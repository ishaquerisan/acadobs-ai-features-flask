"""Snapshot Generation and Snapshot Retrieval Services for Monthly Student Performance."""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config.scoring_config import ScoringConfig, load_scoring_config
from app.services.performance_scoring.academic_scoring import (
    fetch_and_calculate_academic_scores,
)
from app.services.performance_scoring.academic_year import (
    AcademicYearResolutionError,
    month_bounds,
    resolve_academic_year_period,
)
from app.services.performance_scoring.achievement_scoring import (
    fetch_and_calculate_achievement_scores,
    school_has_valid_achievements_in_period,
)
from app.services.performance_scoring.attendance_scoring import (
    fetch_and_calculate_attendance_scores,
)
from app.services.performance_scoring.co_scholastic_scoring import (
    fetch_and_calculate_co_scholastic_scores,
)
from app.services.performance_scoring.homework_scoring import (
    fetch_and_calculate_homework_scores,
)
from app.services.performance_scoring.scoring_calculator import (
    ComputedStudentScore,
    assign_class_competition_ranks,
    combine_student_components,
)

logger = logging.getLogger(__name__)


# Strict allowlist mapping API sort_by keys to validated SQL column expressions
_SORT_COLUMN_MAP: Dict[str, str] = {
    "overall_score": "sps.overall_score",
    "academic_score": "sps.academic_score",
    "attendance_score": "sps.attendance_score",
    "homework_score": "sps.homework_score",
    "achievement_score": "sps.achievement_score",
}


class SchoolNotFoundError(LookupError):
    """Raised when the requested school does not exist."""


class SchoolInactiveError(ValueError):
    """Raised when the requested school is inactive or deleted."""


def _chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _decimal_to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_json_column(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_dialect_name(db: Session) -> str:
    bind = db.get_bind()
    return str(bind.dialect.name).lower() if bind is not None else "mysql"


def _build_upsert_sql(dialect_name: str) -> str:
    """Return dialect-appropriate idempotent UPSERT SQL for `student_performance_scores`."""
    columns = """
        school_id, student_id, class_id, academic_year, score_month,
        period_start, period_end,
        academic_score, academic_contribution,
        main_term_score, other_exam_score, regular_internal_score,
        attendance_score, attendance_contribution,
        homework_score, homework_completion_score, homework_performance_score, homework_contribution,
        achievement_score, achievement_points, achievement_count, achievement_contribution,
        co_scholastic_score, co_scholastic_contribution,
        overall_score, effective_weights, component_availability,
        rank_in_class, calculation_status, calculation_version,
        calculated_at, created_at, updated_at
    """
    placeholders = """
        :school_id, :student_id, :class_id, :academic_year, :score_month,
        :period_start, :period_end,
        :academic_score, :academic_contribution,
        :main_term_score, :other_exam_score, :regular_internal_score,
        :attendance_score, :attendance_contribution,
        :homework_score, :homework_completion_score, :homework_performance_score, :homework_contribution,
        :achievement_score, :achievement_points, :achievement_count, :achievement_contribution,
        :co_scholastic_score, :co_scholastic_contribution,
        :overall_score, :effective_weights, :component_availability,
        :rank_in_class, :calculation_status, :calculation_version,
        :calculated_at, :created_at, :updated_at
    """

    if dialect_name == "sqlite":
        return f"""
            INSERT INTO student_performance_scores ({columns})
            VALUES ({placeholders})
            ON CONFLICT (school_id, student_id, academic_year, score_month)
            DO UPDATE SET
                class_id = excluded.class_id,
                period_start = excluded.period_start,
                period_end = excluded.period_end,
                academic_score = excluded.academic_score,
                academic_contribution = excluded.academic_contribution,
                main_term_score = excluded.main_term_score,
                other_exam_score = excluded.other_exam_score,
                regular_internal_score = excluded.regular_internal_score,
                attendance_score = excluded.attendance_score,
                attendance_contribution = excluded.attendance_contribution,
                homework_score = excluded.homework_score,
                homework_completion_score = excluded.homework_completion_score,
                homework_performance_score = excluded.homework_performance_score,
                homework_contribution = excluded.homework_contribution,
                achievement_score = excluded.achievement_score,
                achievement_points = excluded.achievement_points,
                achievement_count = excluded.achievement_count,
                achievement_contribution = excluded.achievement_contribution,
                co_scholastic_score = excluded.co_scholastic_score,
                co_scholastic_contribution = excluded.co_scholastic_contribution,
                overall_score = excluded.overall_score,
                effective_weights = excluded.effective_weights,
                component_availability = excluded.component_availability,
                rank_in_class = excluded.rank_in_class,
                calculation_status = excluded.calculation_status,
                calculation_version = excluded.calculation_version,
                calculated_at = excluded.calculated_at,
                updated_at = excluded.updated_at
        """

    # MySQL 8+ UPSERT
    return f"""
        INSERT INTO student_performance_scores ({columns})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE
            class_id = VALUES(class_id),
            period_start = VALUES(period_start),
            period_end = VALUES(period_end),
            academic_score = VALUES(academic_score),
            academic_contribution = VALUES(academic_contribution),
            main_term_score = VALUES(main_term_score),
            other_exam_score = VALUES(other_exam_score),
            regular_internal_score = VALUES(regular_internal_score),
            attendance_score = VALUES(attendance_score),
            attendance_contribution = VALUES(attendance_contribution),
            homework_score = VALUES(homework_score),
            homework_completion_score = VALUES(homework_completion_score),
            homework_performance_score = VALUES(homework_performance_score),
            homework_contribution = VALUES(homework_contribution),
            achievement_score = VALUES(achievement_score),
            achievement_points = VALUES(achievement_points),
            achievement_count = VALUES(achievement_count),
            achievement_contribution = VALUES(achievement_contribution),
            co_scholastic_score = VALUES(co_scholastic_score),
            co_scholastic_contribution = VALUES(co_scholastic_contribution),
            overall_score = VALUES(overall_score),
            effective_weights = VALUES(effective_weights),
            component_availability = VALUES(component_availability),
            rank_in_class = VALUES(rank_in_class),
            calculation_status = VALUES(calculation_status),
            calculation_version = VALUES(calculation_version),
            calculated_at = VALUES(calculated_at),
            updated_at = VALUES(updated_at)
    """


def _row_params_from_computed(
    item: ComputedStudentScore,
    academic_year: str,
    score_month: date,
    period_start: date,
    period_end: date,
    calculation_version: str,
    calculated_at: datetime,
) -> Dict[str, Any]:
    return {
        "school_id": item.school_id,
        "student_id": item.student_id,
        "class_id": item.class_id,
        "academic_year": academic_year,
        "score_month": score_month,
        "period_start": period_start,
        "period_end": period_end,
        "academic_score": item.academic_score,
        "academic_contribution": item.academic_contribution,
        "main_term_score": item.main_term_score,
        "other_exam_score": item.other_exam_score,
        "regular_internal_score": item.regular_internal_score,
        "attendance_score": item.attendance_score,
        "attendance_contribution": item.attendance_contribution,
        "homework_score": item.homework_score,
        "homework_completion_score": item.homework_completion_score,
        "homework_performance_score": item.homework_performance_score,
        "homework_contribution": item.homework_contribution,
        "achievement_score": item.achievement_score,
        "achievement_points": item.achievement_points,
        "achievement_count": item.achievement_count,
        "achievement_contribution": item.achievement_contribution,
        "co_scholastic_score": item.co_scholastic_score,
        "co_scholastic_contribution": item.co_scholastic_contribution,
        "overall_score": item.overall_score,
        "effective_weights": json.dumps(item.effective_weights, sort_keys=True),
        "component_availability": json.dumps(item.component_availability, sort_keys=True),
        "rank_in_class": item.rank_in_class,
        "calculation_status": item.calculation_status,
        "calculation_version": calculation_version,
        "calculated_at": calculated_at,
        "created_at": calculated_at,
        "updated_at": calculated_at,
    }


def _recompute_and_update_class_ranks_for_month(
    db: Session,
    school_id: int,
    academic_year: str,
    score_month: date,
) -> None:
    """
    Ensure `rank_in_class` is 100% consistent across ALL saved snapshots for
    `(school_id, academic_year, score_month)`, including when `force_recalculate=False`
    only computed a subset of students.
    """
    rows = db.execute(
        text(
            """
            SELECT id, class_id, overall_score, calculation_status
            FROM student_performance_scores
            WHERE school_id = :school_id
              AND academic_year = :academic_year
              AND score_month = :score_month
            ORDER BY class_id ASC, overall_score DESC, id ASC
            """
        ),
        {
            "school_id": school_id,
            "academic_year": academic_year,
            "score_month": score_month,
        },
    ).mappings().all()

    by_class: Dict[int, List[Dict[str, Any]]] = {}
    null_rank_ids: List[int] = []

    for row in rows:
        if (
            row["class_id"] is None
            or row["overall_score"] is None
            or str(row["calculation_status"]) != "completed"
        ):
            null_rank_ids.append(int(row["id"]))
        else:
            cid = int(row["class_id"])
            by_class.setdefault(cid, []).append(
                {
                    "id": int(row["id"]),
                    "overall_score": Decimal(str(row["overall_score"])).quantize(Decimal("0.01")),
                }
            )

    updates: List[Dict[str, Any]] = []
    for row_id in null_rank_ids:
        updates.append({"row_id": row_id, "rank_in_class": None})

    for class_rows in by_class.values():
        class_rows.sort(key=lambda r: (-r["overall_score"], r["id"]))
        prev_score: Optional[Decimal] = None
        rank = 0
        for pos, item in enumerate(class_rows, start=1):
            if prev_score is None or item["overall_score"] != prev_score:
                rank = pos
                prev_score = item["overall_score"]
            updates.append({"row_id": item["id"], "rank_in_class": rank})

    if updates:
        db.execute(
            text(
                """
                UPDATE student_performance_scores
                SET rank_in_class = :rank_in_class
                WHERE id = :row_id
                """
            ),
            updates,
        )


# ---------------------------------------------------------------------------
# Function A: Generate all school student scores
# ---------------------------------------------------------------------------


def generate_school_student_scores(
    db: Session,
    school_id: int,
    score_month: date | str,
    force_recalculate: bool = False,
    config: Optional[ScoringConfig] = None,
) -> Dict[str, Any]:
    """
    Calculate and persist monthly cumulative performance snapshots for all active students
    in a single active school.

    Idempotent UPSERT semantics:
    - Running for the same `(school_id, academic_year, score_month)` updates existing snapshots
      when `force_recalculate=True`, and safely computes only missing/failed students when
      `force_recalculate=False`, never duplicating rows or overwriting previous months.
    """
    started_monotonic = time.perf_counter()
    cfg = config or load_scoring_config()

    # 1. Validate school existence and active status
    school = db.execute(
        text(
            """
            SELECT id, name, status, trash, education_year_start, attendance_count
            FROM schools
            WHERE id = :school_id
            LIMIT 1
            """
        ),
        {"school_id": school_id},
    ).mappings().first()

    if not school:
        raise SchoolNotFoundError(f"School with id={school_id} not found")

    if (school["trash"] is not None and int(school["trash"]) != 0) or (
        str(school["status"] or "").lower() != "active"
    ):
        raise SchoolInactiveError(f"School with id={school_id} is not active")

    # 2. Resolve academic-year period (raises AcademicYearResolutionError if unresolvable)
    period = resolve_academic_year_period(
        db=db,
        school_id=school_id,
        school_education_year_start=school["education_year_start"],
        score_month=score_month,
        config=cfg,
    )

    school_attendance_count = int(school["attendance_count"] or 2)

    # 3. Fetch all active, non-trashed, non-alumni students in this school
    students = db.execute(
        text(
            """
            SELECT id, school_id, class_id
            FROM students
            WHERE school_id = :school_id
              AND status = 'active'
              AND (alumni IS NULL OR alumni = 0)
              AND (trash IS NULL OR trash = 0)
            ORDER BY id ASC
            """
        ),
        {"school_id": school_id},
    ).mappings().all()

    total_students = len(students)

    # Identify already-completed snapshots if force_recalculate is False
    existing_completed_student_ids: Set[int] = set()
    if not force_recalculate and total_students > 0:
        existing_rows = db.execute(
            text(
                """
                SELECT student_id
                FROM student_performance_scores
                WHERE school_id = :school_id
                  AND academic_year = :academic_year
                  AND score_month = :score_month
                  AND calculation_status IN ('completed', 'insufficient_data')
                """
            ),
            {
                "school_id": school_id,
                "academic_year": period.academic_year,
                "score_month": period.score_month,
            },
        ).mappings().all()
        existing_completed_student_ids = {int(r["student_id"]) for r in existing_rows}

    target_students = [
        s for s in students if force_recalculate or int(s["id"]) not in existing_completed_student_ids
    ]
    skipped_students = total_students - len(target_students)

    # Check school-wide achievement activity once for the period
    school_has_ach_activity = (
        school_has_valid_achievements_in_period(
            db, school_id, period.period_start, period.period_end
        )
        if target_students
        else False
    )

    computed_records: List[ComputedStudentScore] = []
    errors: List[Dict[str, Any]] = []
    processed_students = 0
    successful_students = 0
    insufficient_data_students = 0
    failed_students = 0

    # 4. Process in batches using bulk queries per component (zero N+1)
    for batch in _chunked(target_students, cfg.student_batch_size):
        batch_ids = [int(s["id"]) for s in batch]

        academic_map = fetch_and_calculate_academic_scores(
            db, school_id, batch_ids, period.period_start, period.period_end, cfg
        )
        attendance_map = fetch_and_calculate_attendance_scores(
            db,
            school_id,
            batch_ids,
            period.period_start,
            period.period_end,
            cfg,
            school_attendance_count=school_attendance_count,
        )
        homework_map = fetch_and_calculate_homework_scores(
            db, school_id, batch_ids, period.period_start, period.period_end, cfg
        )
        achievement_map = fetch_and_calculate_achievement_scores(
            db,
            school_id,
            batch_ids,
            period.period_start,
            period.period_end,
            cfg,
            school_has_activity=school_has_ach_activity,
        )
        co_scholastic_map = fetch_and_calculate_co_scholastic_scores(
            db, school_id, batch_ids, period.period_start, period.period_end, cfg
        )

        for student_row in batch:
            sid = int(student_row["id"])
            cid = int(student_row["class_id"]) if student_row["class_id"] is not None else None
            processed_students += 1
            try:
                computed = combine_student_components(
                    student_id=sid,
                    school_id=school_id,
                    class_id=cid,
                    academic=academic_map[sid],
                    attendance=attendance_map[sid],
                    homework=homework_map[sid],
                    achievement=achievement_map[sid],
                    co_scholastic=co_scholastic_map[sid],
                    config=cfg,
                )
                computed_records.append(computed)
            except Exception as exc:
                failed_students += 1
                logger.exception(
                    "Failed to calculate performance score for school_id=%s student_id=%s",
                    school_id,
                    sid,
                )
                errors.append({"student_id": sid, "error": str(exc)})

    # 5. Compute initial class ranks among newly computed records (for fast full-school runs)
    if computed_records and skipped_students == 0:
        assign_class_competition_ranks(computed_records)

    # 6. Persist via idempotent UPSERT in batches with per-student fault isolation fallback
    dialect_name = _get_dialect_name(db)
    upsert_stmt = text(_build_upsert_sql(dialect_name))
    calculated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    for batch_computed in _chunked(computed_records, cfg.student_batch_size):
        params_list = [
            _row_params_from_computed(
                item=item,
                academic_year=period.academic_year,
                score_month=period.score_month,
                period_start=period.period_start,
                period_end=period.period_end,
                calculation_version=cfg.calculation_version,
                calculated_at=calculated_at,
            )
            for item in batch_computed
        ]

        try:
            with db.begin_nested():
                db.execute(upsert_stmt, params_list)
            for item in batch_computed:
                if item.calculation_status == "insufficient_data":
                    insufficient_data_students += 1
                else:
                    successful_students += 1
        except Exception as batch_exc:
            logger.warning(
                "Batch UPSERT failed for school_id=%s (%s); falling back to per-student savepoints",
                school_id,
                batch_exc,
            )
            for item, params in zip(batch_computed, params_list):
                try:
                    with db.begin_nested():
                        db.execute(upsert_stmt, params)
                    if item.calculation_status == "insufficient_data":
                        insufficient_data_students += 1
                    else:
                        successful_students += 1
                except Exception as student_exc:
                    failed_students += 1
                    logger.exception(
                        "Failed to persist performance score for school_id=%s student_id=%s",
                        school_id,
                        item.student_id,
                    )
                    errors.append({"student_id": item.student_id, "error": str(student_exc)})

    # 7. If some students were skipped (partial retry) or failed, reconcile exact class ranks across DB
    if computed_records and (skipped_students > 0 or failed_students > 0):
        _recompute_and_update_class_ranks_for_month(
            db, school_id, period.academic_year, period.score_month
        )

    db.commit()

    elapsed = round(time.perf_counter() - started_monotonic, 4)
    summary = {
        "school_id": school_id,
        "academic_year": period.academic_year,
        "score_month": period.score_month,
        "period_start": period.period_start,
        "period_end": period.period_end,
        "total_students": total_students,
        "processed_students": processed_students,
        "successful_students": successful_students,
        "insufficient_data_students": insufficient_data_students,
        "skipped_students": skipped_students,
        "failed_students": failed_students,
        "execution_time": elapsed,
        "calculation_version": cfg.calculation_version,
        "errors": errors,
    }

    logger.info(
        "Generated monthly performance scores: school_id=%s score_month=%s total=%s "
        "processed=%s successful=%s insufficient=%s skipped=%s failed=%s time=%.4fs",
        school_id,
        period.score_month.isoformat(),
        total_students,
        processed_students,
        successful_students,
        insufficient_data_students,
        skipped_students,
        failed_students,
        elapsed,
    )
    return summary


# ---------------------------------------------------------------------------
# Function B: Get all student scores for a school (Paginated Read Only)
# ---------------------------------------------------------------------------


def get_school_student_scores(
    db: Session,
    school_id: int,
    score_month: Optional[date | str] = None,
    academic_year: Optional[str] = None,
    class_id: Optional[int] = None,
    allowed_class_ids: Optional[Set[int]] = None,
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    sort_by: str = "overall_score",
    sort_order: str = "desc",
    calculation_status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve saved monthly student performance snapshots for a school with pagination,
    filtering, and validated sorting. Never recalculates scores on read.
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")

    if sort_by not in _SORT_COLUMN_MAP:
        raise ValueError(
            f"Invalid sort_by '{sort_by}'. Allowed values: {', '.join(_SORT_COLUMN_MAP.keys())}"
        )

    order_dir = str(sort_order or "desc").strip().lower()
    if order_dir not in ("asc", "desc"):
        raise ValueError("sort_order must be 'asc' or 'desc'")

    resolved_month: Optional[date] = None
    if score_month is not None:
        resolved_month, _ = month_bounds(score_month)
    else:
        # Default to latest available snapshot month for this school
        latest_clauses = ["school_id = :school_id"]
        latest_params: Dict[str, Any] = {"school_id": school_id}
        if academic_year:
            latest_clauses.append("academic_year = :academic_year")
            latest_params["academic_year"] = academic_year.strip()
        if class_id is not None:
            latest_clauses.append("class_id = :class_id")
            latest_params["class_id"] = class_id

        latest_sql = (
            "SELECT MAX(score_month) AS latest_month "
            "FROM student_performance_scores "
            f"WHERE {' AND '.join(latest_clauses)}"
        )
        latest_row = db.execute(text(latest_sql), latest_params).mappings().first()
        resolved_month = _to_date(latest_row["latest_month"]) if latest_row else None

    if resolved_month is None:
        return {
            "success": True,
            "school_id": school_id,
            "score_month": None,
            "academic_year": academic_year,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": 0,
                "total_pages": 0,
                "has_next": False,
                "has_previous": page > 1,
            },
            "total_records": 0,
            "items": [],
        }

    where_clauses = [
        "sps.school_id = :school_id",
        "s.school_id = :school_id",
        "(s.trash IS NULL OR s.trash = 0)",
        "sps.score_month = :score_month",
    ]
    params: Dict[str, Any] = {
        "school_id": school_id,
        "score_month": resolved_month,
    }
    expanding_params: List[Any] = []

    if academic_year:
        where_clauses.append("sps.academic_year = :academic_year")
        params["academic_year"] = academic_year.strip()

    if class_id is not None:
        where_clauses.append("sps.class_id = :class_id")
        params["class_id"] = class_id
    elif allowed_class_ids is not None:
        if not allowed_class_ids:
            return {
                "success": True,
                "school_id": school_id,
                "score_month": resolved_month,
                "academic_year": academic_year,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_records": 0,
                    "total_pages": 0,
                    "has_next": False,
                    "has_previous": page > 1,
                },
                "total_records": 0,
                "items": [],
            }
        where_clauses.append("sps.class_id IN :allowed_class_ids")
        params["allowed_class_ids"] = sorted(allowed_class_ids)
        expanding_params.append(bindparam("allowed_class_ids", expanding=True))

    if calculation_status:
        where_clauses.append("sps.calculation_status = :calculation_status")
        params["calculation_status"] = calculation_status.strip().lower()

    if search and search.strip():
        search_like = f"%{search.strip().lower()}%"
        where_clauses.append(
            "(LOWER(s.full_name) LIKE :search_like "
            "OR LOWER(COALESCE(s.reg_no, '')) LIKE :search_like "
            "OR CAST(COALESCE(s.roll_number, '') AS CHAR) LIKE :search_like)"
        )
        params["search_like"] = search_like

    where_sql = " AND ".join(where_clauses)

    count_query = text(
        f"""
        SELECT COUNT(*) AS total
        FROM student_performance_scores sps
        INNER JOIN students s
            ON s.id = sps.student_id
        WHERE {where_sql}
        """
    )
    if expanding_params:
        count_query = count_query.bindparams(*expanding_params)

    total_records = int(db.execute(count_query, params).scalar() or 0)
    total_pages = math.ceil(total_records / page_size) if total_records > 0 else 0
    offset = (page - 1) * page_size

    sort_col = _SORT_COLUMN_MAP[sort_by]
    order_sql = (
        f"CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END ASC, "
        f"{sort_col} {order_dir.upper()}, "
        "sps.student_id ASC"
    )

    data_query = text(
        f"""
        SELECT
            sps.student_id,
            s.full_name AS student_name,
            s.reg_no,
            s.roll_number,
            sps.class_id,
            c.classname,
            c.year AS class_year,
            c.division AS class_division,
            sps.academic_year,
            sps.score_month,
            sps.overall_score,
            sps.rank_in_class,
            sps.academic_score,
            sps.attendance_score,
            sps.homework_score,
            sps.achievement_score,
            sps.co_scholastic_score,
            sps.calculation_status,
            sps.calculation_version,
            sps.calculated_at
        FROM student_performance_scores sps
        INNER JOIN students s
            ON s.id = sps.student_id
        LEFT JOIN classes c
            ON c.id = sps.class_id
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT :limit OFFSET :offset
        """
    )
    if expanding_params:
        data_query = data_query.bindparams(*expanding_params)

    data_params = dict(params)
    data_params["limit"] = page_size
    data_params["offset"] = offset

    rows = db.execute(data_query, data_params).mappings().all()

    items: List[Dict[str, Any]] = []
    resolved_academic_year = academic_year
    for row in rows:
        if resolved_academic_year is None:
            resolved_academic_year = row["academic_year"]
        class_details = (
            {
                "id": int(row["class_id"]),
                "classname": row["classname"],
                "year": int(row["class_year"]) if row["class_year"] is not None else None,
                "division": row["class_division"],
            }
            if row["class_id"] is not None
            else None
        )
        items.append(
            {
                "student_id": int(row["student_id"]),
                "student_name": row["student_name"],
                "reg_no": row["reg_no"],
                "roll_number": int(row["roll_number"]) if row["roll_number"] is not None else None,
                "class_details": class_details,
                "academic_year": row["academic_year"],
                "score_month": _to_date(row["score_month"]),
                "overall_score": _decimal_to_float(row["overall_score"]),
                "rank_in_class": int(row["rank_in_class"]) if row["rank_in_class"] is not None else None,
                "component_scores": {
                    "academic_score": _decimal_to_float(row["academic_score"]),
                    "attendance_score": _decimal_to_float(row["attendance_score"]),
                    "homework_score": _decimal_to_float(row["homework_score"]),
                    "achievement_score": _decimal_to_float(row["achievement_score"]),
                    "co_scholastic_score": _decimal_to_float(row["co_scholastic_score"]),
                },
                "calculation_status": row["calculation_status"],
                "calculation_version": row["calculation_version"],
                "calculated_at": _to_datetime(row["calculated_at"]),
            }
        )

    return {
        "success": True,
        "school_id": school_id,
        "score_month": resolved_month,
        "academic_year": resolved_academic_year,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_records": total_records,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
        },
        "total_records": total_records,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Function C: Get individual student score (Read Only)
# ---------------------------------------------------------------------------


def get_student_performance_score(
    db: Session,
    student_id: int,
    score_month: Optional[date | str] = None,
    academic_year: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve the saved monthly performance snapshot and complete breakdown for an
    individual student. Defaults to the latest available snapshot if `score_month`
    is omitted. Never recalculates on read.
    """
    where_clauses = [
        "sps.student_id = :student_id",
        "s.id = :student_id",
        "sps.school_id = s.school_id",
        "(s.trash IS NULL OR s.trash = 0)",
    ]
    params: Dict[str, Any] = {"student_id": student_id}

    if score_month is not None:
        normalized_month, _ = month_bounds(score_month)
        where_clauses.append("sps.score_month = :score_month")
        params["score_month"] = normalized_month

    if academic_year:
        where_clauses.append("sps.academic_year = :academic_year")
        params["academic_year"] = academic_year.strip()

    query = text(
        f"""
        SELECT
            sps.*,
            s.full_name AS student_name,
            s.reg_no,
            s.roll_number,
            sc.name AS school_name,
            c.classname,
            c.year AS class_year,
            c.division AS class_division
        FROM student_performance_scores sps
        INNER JOIN students s
            ON s.id = sps.student_id
        INNER JOIN schools sc
            ON sc.id = sps.school_id
        LEFT JOIN classes c
            ON c.id = sps.class_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY sps.score_month DESC, sps.calculated_at DESC
        LIMIT 1
        """
    )

    row = db.execute(query, params).mappings().first()
    if not row:
        raise LookupError(
            f"No performance score snapshot found for student_id={student_id}"
            + (f" and score_month={params.get('score_month')}" if "score_month" in params else "")
        )

    class_details = (
        {
            "id": int(row["class_id"]),
            "classname": row["classname"],
            "year": int(row["class_year"]) if row["class_year"] is not None else None,
            "division": row["class_division"],
        }
        if row["class_id"] is not None
        else None
    )

    return {
        "student": {
            "id": int(row["student_id"]),
            "full_name": row["student_name"],
            "reg_no": row["reg_no"],
            "roll_number": int(row["roll_number"]) if row["roll_number"] is not None else None,
        },
        "school": {
            "id": int(row["school_id"]),
            "name": row["school_name"],
        },
        "class_details": class_details,
        "academic_year": row["academic_year"],
        "score_month": _to_date(row["score_month"]),
        "period_start": _to_date(row["period_start"]),
        "period_end": _to_date(row["period_end"]),
        "overall_score": _decimal_to_float(row["overall_score"]),
        "rank_in_class": int(row["rank_in_class"]) if row["rank_in_class"] is not None else None,
        "academic": {
            "academic_score": _decimal_to_float(row["academic_score"]),
            "academic_contribution": _decimal_to_float(row["academic_contribution"]),
            "main_term_score": _decimal_to_float(row["main_term_score"]),
            "other_exam_score": _decimal_to_float(row["other_exam_score"]),
            "regular_internal_score": _decimal_to_float(row["regular_internal_score"]),
        },
        "attendance": {
            "attendance_score": _decimal_to_float(row["attendance_score"]),
            "attendance_contribution": _decimal_to_float(row["attendance_contribution"]),
        },
        "homework": {
            "homework_score": _decimal_to_float(row["homework_score"]),
            "homework_completion_score": _decimal_to_float(row["homework_completion_score"]),
            "homework_performance_score": _decimal_to_float(row["homework_performance_score"]),
            "homework_contribution": _decimal_to_float(row["homework_contribution"]),
        },
        "achievement": {
            "achievement_score": _decimal_to_float(row["achievement_score"]),
            "achievement_points": _decimal_to_float(row["achievement_points"]),
            "achievement_count": int(row["achievement_count"] or 0),
            "achievement_contribution": _decimal_to_float(row["achievement_contribution"]),
        },
        "co_scholastic": {
            "co_scholastic_score": _decimal_to_float(row["co_scholastic_score"]),
            "co_scholastic_contribution": _decimal_to_float(row["co_scholastic_contribution"]),
        },
        "effective_weights": _parse_json_column(row["effective_weights"]),
        "component_availability": _parse_json_column(row["component_availability"]),
        "calculation_status": row["calculation_status"],
        "calculation_version": row["calculation_version"],
        "calculated_at": _to_datetime(row["calculated_at"]),
    }


# ---------------------------------------------------------------------------
# Multi-school orchestrator used by the automatic monthly scheduler
# ---------------------------------------------------------------------------


def generate_all_active_schools_scores(
    session_factory: Callable[[], Session],
    score_month: date | str,
    force_recalculate: bool = False,
    config: Optional[ScoringConfig] = None,
) -> Dict[str, Any]:
    """
    Calculate monthly performance snapshots across all active, non-trashed schools.

    Fault-isolated:
    - Each school runs in its own session and transaction.
    - Retries transient errors up to `config.scheduler_max_retries`.
    - A failed school (e.g., missing `education_year_start`) is logged and reported
      without stopping other schools.
    """
    cfg = config or load_scoring_config()
    normalized_month, _ = month_bounds(score_month)
    started = time.perf_counter()

    db = session_factory()
    try:
        active_schools = db.execute(
            text(
                """
                SELECT id, name
                FROM schools
                WHERE status = 'active'
                  AND (trash IS NULL OR trash = 0)
                ORDER BY id ASC
                """
            )
        ).mappings().all()
    finally:
        db.close()

    school_results: List[Dict[str, Any]] = []
    failed_schools: List[Dict[str, Any]] = []

    for school in active_schools:
        sid = int(school["id"])
        attempts = 0
        max_attempts = 1 + max(0, cfg.scheduler_max_retries)
        last_error: Optional[str] = None

        while attempts < max_attempts:
            attempts += 1
            school_db = session_factory()
            try:
                summary = generate_school_student_scores(
                    db=school_db,
                    school_id=sid,
                    score_month=normalized_month,
                    force_recalculate=force_recalculate,
                    config=cfg,
                )
                school_results.append(summary)
                last_error = None
                break
            except AcademicYearResolutionError as exc:
                # Configuration error: do not waste retries
                school_db.rollback()
                last_error = str(exc)
                logger.error(
                    "Academic year configuration error for school_id=%s: %s",
                    sid,
                    last_error,
                )
                break
            except Exception as exc:
                school_db.rollback()
                last_error = str(exc)
                logger.exception(
                    "Monthly scoring failed for school_id=%s (attempt %s/%s)",
                    sid,
                    attempts,
                    max_attempts,
                )
                if attempts < max_attempts and cfg.scheduler_retry_delay_seconds > 0:
                    time.sleep(min(cfg.scheduler_retry_delay_seconds, 5))
            finally:
                school_db.close()

        if last_error is not None:
            failed_schools.append(
                {
                    "school_id": sid,
                    "school_name": school["name"],
                    "error": last_error,
                }
            )

    return {
        "score_month": normalized_month.isoformat(),
        "total_active_schools": len(active_schools),
        "succeeded_schools": len(school_results),
        "failed_schools_count": len(failed_schools),
        "execution_time": round(time.perf_counter() - started, 4),
        "school_summaries": [
            {
                **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in s.items()},
            }
            for s in school_results
        ],
        "failed_schools": failed_schools,
    }
