"""Automatic Monthly Performance Scoring Scheduler with Distributed MySQL Locking.

Schedule:
- Runs on Day 1 of every month at 00:10 local time (`Asia/Kolkata` default, configurable).
- Calculates snapshots for the previous month (academic-year start through last day of previous month).
- Safe under multi-worker FastAPI deployments (uvicorn --workers N / gunicorn / multiple replicas):
  uses MySQL `GET_LOCK(lock_name, timeout)` held on a dedicated session plus an execution audit row
  in `performance_scoring_job_runs` so concurrent workers never run duplicate jobs.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterator, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.scoring_config import ScoringConfig, load_scoring_config
from app.database.connection import SessionLocal
from app.services.performance_scoring.academic_year import previous_month_first_day
from app.services.performance_scoring.snapshot_service import (
    generate_all_active_schools_scores,
)

try:
    from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
    from apscheduler.triggers.cron import CronTrigger  # type: ignore
except ImportError:  # pragma: no cover - optional in bare stdlib environments
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore

logger = logging.getLogger(__name__)

_scheduler_instance: Any = None
_MONTHLY_JOB_ID = "monthly_student_performance_scoring"


def _worker_identifier() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _dialect_name(db: Session) -> str:
    bind = db.get_bind()
    return str(bind.dialect.name).lower() if bind is not None else "mysql"


@contextmanager
def distributed_job_lock(
    session_factory: Callable[[], Session],
    lock_name: str,
    timeout_seconds: int = 5,
) -> Iterator[bool]:
    """
    Acquire a cross-worker distributed lock using MySQL advisory locks (`GET_LOCK`).

    On MySQL, the lock is automatically released when the holding connection closes (even if a
    worker process crashes unexpectedly), preventing permanent deadlocks.
    On SQLite/other dialects (e.g., local test suites), checks `performance_scoring_job_runs`.
    """
    lock_session = session_factory()
    acquired = False
    dialect = _dialect_name(lock_session)

    try:
        if dialect == "mysql":
            res = lock_session.execute(
                text("SELECT GET_LOCK(:lock_name, :timeout) AS acquired"),
                {"lock_name": lock_name[:64], "timeout": timeout_seconds},
            ).scalar()
            acquired = int(res or 0) == 1
        else:
            # Fallback for non-MySQL test environments
            try:
                existing = lock_session.execute(
                    text(
                        """
                        SELECT id FROM performance_scoring_job_runs
                        WHERE job_key = :job_key AND status = 'running'
                        LIMIT 1
                        """
                    ),
                    {"job_key": lock_name},
                ).first()
                acquired = existing is None
            except Exception:
                acquired = True

        yield acquired
    finally:
        try:
            if acquired and dialect == "mysql":
                lock_session.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name[:64]},
                )
        except Exception:
            logger.exception("Error releasing MySQL advisory lock %s", lock_name)
        finally:
            lock_session.close()


def _create_job_run_record(
    session_factory: Callable[[], Session],
    job_key: str,
    score_month: date,
    trigger_type: str = "scheduled",
    school_id: Optional[int] = None,
    triggered_by_user_id: Optional[int] = None,
    status: str = "running",
) -> Optional[int]:
    """Insert a job audit record into `performance_scoring_job_runs` if the table exists."""
    db = session_factory()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        result = db.execute(
            text(
                """
                INSERT INTO performance_scoring_job_runs (
                    job_key, trigger_type, score_month, school_id,
                    triggered_by_user_id, worker_id, status,
                    started_at, created_at, updated_at
                )
                VALUES (
                    :job_key, :trigger_type, :score_month, :school_id,
                    :triggered_by_user_id, :worker_id, :status,
                    :started_at, :created_at, :updated_at
                )
                """
            ),
            {
                "job_key": job_key,
                "trigger_type": trigger_type,
                "score_month": score_month,
                "school_id": school_id,
                "triggered_by_user_id": triggered_by_user_id,
                "worker_id": _worker_identifier(),
                "status": status,
                "started_at": now_utc,
                "created_at": now_utc,
                "updated_at": now_utc,
            },
        )
        db.commit()
        return int(result.lastrowid) if result.lastrowid is not None else None
    except Exception as exc:
        db.rollback()
        logger.warning("Could not write performance_scoring_job_runs start log: %s", exc)
        return None
    finally:
        db.close()


def _finalize_job_run_record(
    session_factory: Callable[[], Session],
    run_id: Optional[int],
    status: str,
    summary: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    if run_id is None:
        return
    db = session_factory()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.execute(
            text(
                """
                UPDATE performance_scoring_job_runs
                SET status = :status,
                    finished_at = :finished_at,
                    summary_json = :summary_json,
                    error_message = :error_message,
                    updated_at = :updated_at
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "finished_at": now_utc,
                "summary_json": json.dumps(summary, default=str) if summary is not None else None,
                "error_message": error_message,
                "updated_at": now_utc,
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Could not update performance_scoring_job_runs id=%s: %s", run_id, exc)
    finally:
        db.close()


def run_monthly_scoring_job(
    session_factory: Callable[[], Session] = SessionLocal,
    score_month: Optional[date] = None,
    force_recalculate: bool = False,
    config: Optional[ScoringConfig] = None,
) -> Dict[str, Any]:
    """
    Execute the monthly student performance scoring job across all active schools.

    When `score_month` is None, automatically determines the previous calendar month
    in the configured scheduler timezone (e.g., on Nov 1 00:10 IST -> 2025-10-01).
    """
    cfg = config or load_scoring_config()
    tz = ZoneInfo(cfg.scheduler_timezone)

    if score_month is None:
        local_today = datetime.now(tz).date()
        target_month = previous_month_first_day(local_today)
    else:
        target_month = score_month.replace(day=1)

    job_key = f"acadobs:monthly_perf:{target_month.isoformat()}"
    logger.info(
        "Monthly performance scoring triggered by worker=%s for score_month=%s",
        _worker_identifier(),
        target_month.isoformat(),
    )

    with distributed_job_lock(
        session_factory=session_factory,
        lock_name=job_key,
        timeout_seconds=cfg.scheduler_lock_timeout_seconds,
    ) as acquired:
        if not acquired:
            logger.info(
                "Skipping monthly scoring job for %s: another worker holds lock %s",
                target_month.isoformat(),
                job_key,
            )
            return {
                "status": "skipped_locked",
                "job_key": job_key,
                "score_month": target_month.isoformat(),
                "worker_id": _worker_identifier(),
            }

        run_id = _create_job_run_record(
            session_factory=session_factory,
            job_key=job_key,
            score_month=target_month,
            trigger_type="scheduled",
            status="running",
        )

        try:
            summary = generate_all_active_schools_scores(
                session_factory=session_factory,
                score_month=target_month,
                force_recalculate=force_recalculate,
                config=cfg,
            )
            final_status = (
                "partial_failure" if summary["failed_schools_count"] > 0 else "completed"
            )
            _finalize_job_run_record(
                session_factory=session_factory,
                run_id=run_id,
                status=final_status,
                summary=summary,
            )
            logger.info(
                "Monthly scoring job finished (%s) for %s: succeeded=%s failed=%s",
                final_status,
                target_month.isoformat(),
                summary["succeeded_schools"],
                summary["failed_schools_count"],
            )
            return {"status": final_status, "job_key": job_key, **summary}
        except Exception as exc:
            logger.exception("Unhandled failure during monthly scoring job for %s", target_month)
            _finalize_job_run_record(
                session_factory=session_factory,
                run_id=run_id,
                status="failed",
                error_message=str(exc),
            )
            raise


def start_performance_scheduler(config: Optional[ScoringConfig] = None) -> Any:
    """Start the APScheduler background cron scheduler if enabled."""
    global _scheduler_instance
    cfg = config or load_scoring_config()

    if not cfg.scheduler_enabled:
        logger.info("Monthly performance scoring scheduler is disabled by configuration")
        return None

    if BackgroundScheduler is None or CronTrigger is None:
        logger.warning(
            "APScheduler is not installed; automatic monthly scoring scheduler will not start. "
            "Install APScheduler>=3.10.4 to enable scheduled execution."
        )
        return None

    if _scheduler_instance is not None and getattr(_scheduler_instance, "running", False):
        return _scheduler_instance

    tz = ZoneInfo(cfg.scheduler_timezone)
    scheduler = BackgroundScheduler(timezone=tz)
    trigger = CronTrigger(
        day=cfg.scheduler_day_of_month,
        hour=cfg.scheduler_hour,
        minute=cfg.scheduler_minute,
        timezone=tz,
    )
    scheduler.add_job(
        run_monthly_scoring_job,
        trigger=trigger,
        id=_MONTHLY_JOB_ID,
        name="Monthly Student Performance Scoring Snapshot Job",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler_instance = scheduler

    job = scheduler.get_job(_MONTHLY_JOB_ID)
    logger.info(
        "Started monthly performance scoring scheduler (day=%s %02d:%02d %s, next_run=%s)",
        cfg.scheduler_day_of_month,
        cfg.scheduler_hour,
        cfg.scheduler_minute,
        cfg.scheduler_timezone,
        getattr(job, "next_run_time", None),
    )
    return scheduler


def shutdown_performance_scheduler() -> None:
    """Gracefully shut down the background scheduler on FastAPI exit."""
    global _scheduler_instance
    if _scheduler_instance is not None:
        try:
            if getattr(_scheduler_instance, "running", False):
                _scheduler_instance.shutdown(wait=False)
                logger.info("Monthly performance scoring scheduler stopped")
        finally:
            _scheduler_instance = None


def get_scheduler_status() -> Dict[str, Any]:
    """Return diagnostic status of the scheduler for health checks / ops."""
    if _scheduler_instance is None or not getattr(_scheduler_instance, "running", False):
        return {"running": False, "next_run_time": None}
    job = _scheduler_instance.get_job(_MONTHLY_JOB_ID)
    return {
        "running": True,
        "job_id": _MONTHLY_JOB_ID,
        "next_run_time": str(getattr(job, "next_run_time", None)) if job else None,
    }
