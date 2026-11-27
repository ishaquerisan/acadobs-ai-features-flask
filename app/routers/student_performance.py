"""REST API endpoints for the Monthly Student Performance Scoring System."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.config.scoring_config import load_scoring_config
from app.database.connection import SessionLocal, get_db
from app.dependencies.auth import (
    AuthenticatedUser,
    authorize_school_performance_read,
    authorize_school_recalculation,
    authorize_student_performance_read,
    get_current_user,
)
from app.schemas.student_performance import (
    RecalculateSchoolScoresRequest,
    RecalculateSchoolScoresResponse,
    SchoolScoreGenerationSummary,
    SchoolStudentPerformanceListResponse,
    SortByOption,
    SortOrderOption,
    StudentPerformanceDetailData,
    StudentPerformanceDetailResponse,
    normalize_score_month,
)
from app.services.performance_scoring.academic_year import AcademicYearResolutionError
from app.services.performance_scoring.scheduler import (
    _create_job_run_record,
    _finalize_job_run_record,
    distributed_job_lock,
)
from app.services.performance_scoring.snapshot_service import (
    SchoolInactiveError,
    SchoolNotFoundError,
    generate_school_student_scores,
    get_school_student_scores,
    get_student_performance_score,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["Student Performance Scoring"],
)


@router.get(
    "/schools/{school_id}/student-performance",
    response_model=SchoolStudentPerformanceListResponse,
    summary="List paginated student performance scores for a school",
)
def list_school_student_performance(
    school_id: int = Path(..., gt=0, description="Target school ID"),
    score_month: Optional[str] = Query(
        default=None,
        description="Snapshot month (YYYY-MM or YYYY-MM-DD). Defaults to latest available snapshot.",
    ),
    academic_year: Optional[str] = Query(
        default=None,
        max_length=255,
        description="Optional academic year filter (e.g., 2025-2026).",
    ),
    class_id: Optional[int] = Query(
        default=None,
        gt=0,
        description="Optional class ID filter.",
    ),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)."),
    page_size: int = Query(default=20, ge=1, le=100, description="Records per page (max 100)."),
    search: Optional[str] = Query(
        default=None,
        max_length=120,
        description="Optional search by student full_name, reg_no, or roll_number.",
    ),
    sort_by: SortByOption = Query(
        default="overall_score",
        description="Validated sort column.",
    ),
    sort_order: SortOrderOption = Query(
        default="desc",
        description="Sort direction: asc or desc.",
    ),
    calculation_status: Optional[str] = Query(
        default=None,
        max_length=30,
        description="Optional status filter (completed, insufficient_data, failed).",
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SchoolStudentPerformanceListResponse:
    allowed_class_ids = authorize_school_performance_read(
        db=db,
        user=current_user,
        school_id=school_id,
        requested_class_id=class_id,
    )

    parsed_month = None
    if score_month is not None and score_month.strip():
        try:
            parsed_month = normalize_score_month(score_month)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid score_month: {exc}",
            )

    try:
        payload = get_school_student_scores(
            db=db,
            school_id=school_id,
            score_month=parsed_month,
            academic_year=academic_year,
            class_id=class_id,
            allowed_class_ids=allowed_class_ids,
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
            calculation_status=calculation_status,
        )
        return SchoolStudentPerformanceListResponse(**payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to fetch school student performance scores (school_id=%s)", school_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve school student performance scores",
        )


@router.get(
    "/students/{student_id}/performance-score",
    response_model=StudentPerformanceDetailResponse,
    summary="Retrieve saved monthly performance score and breakdown for one student",
)
def get_individual_student_performance_score(
    student_id: int = Path(..., gt=0, description="Canonical student ID (`students.id`)"),
    score_month: Optional[str] = Query(
        default=None,
        description="Snapshot month (YYYY-MM or YYYY-MM-DD). Defaults to latest available snapshot.",
    ),
    academic_year: Optional[str] = Query(
        default=None,
        max_length=255,
        description="Optional academic year filter (e.g., 2025-2026).",
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudentPerformanceDetailResponse:
    # Verifies parent-child ownership, school isolation, and teacher/staff permissions
    authorize_student_performance_read(
        db=db,
        user=current_user,
        student_id=student_id,
    )

    parsed_month = None
    if score_month is not None and score_month.strip():
        try:
            parsed_month = normalize_score_month(score_month)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid score_month: {exc}",
            )

    try:
        detail = get_student_performance_score(
            db=db,
            student_id=student_id,
            score_month=parsed_month,
            academic_year=academic_year,
        )
        return StudentPerformanceDetailResponse(
            success=True,
            data=StudentPerformanceDetailData(**detail),
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to fetch student performance score (student_id=%s)", student_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve student performance score",
        )


@router.post(
    "/schools/{school_id}/student-performance/recalculate",
    response_model=RecalculateSchoolScoresResponse,
    summary="Manually calculate or recalculate monthly performance scores for a school",
)
def recalculate_school_student_performance(
    payload: RecalculateSchoolScoresRequest,
    school_id: int = Path(..., gt=0, description="Target school ID"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RecalculateSchoolScoresResponse:
    authorize_school_recalculation(user=current_user, school_id=school_id)

    cfg = load_scoring_config()
    lock_key = f"acadobs:school_perf:{school_id}:{payload.score_month.isoformat()}"

    # Bind session factory to the same engine as the injected request session (important for tests too)
    bind = db.get_bind()

    def _lock_session_factory() -> Session:
        if bind is not None and bind is not SessionLocal.kw.get("bind"):
            return Session(bind=bind)
        return SessionLocal()

    with distributed_job_lock(
        session_factory=_lock_session_factory,
        lock_name=lock_key,
        timeout_seconds=cfg.scheduler_lock_timeout_seconds,
    ) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A performance score calculation for school_id={school_id} and "
                    f"score_month={payload.score_month.isoformat()} is already running."
                ),
            )

        run_id = _create_job_run_record(
            session_factory=_lock_session_factory,
            job_key=lock_key,
            score_month=payload.score_month,
            trigger_type="manual",
            school_id=school_id,
            triggered_by_user_id=current_user.user_id,
            status="running",
        )

        try:
            summary = generate_school_student_scores(
                db=db,
                school_id=school_id,
                score_month=payload.score_month,
                force_recalculate=payload.force_recalculate,
                config=cfg,
            )
            final_status = "partial_failure" if summary["failed_students"] > 0 else "completed"
            _finalize_job_run_record(
                session_factory=_lock_session_factory,
                run_id=run_id,
                status=final_status,
                summary=summary,
            )
            return RecalculateSchoolScoresResponse(
                success=True,
                message="Monthly student performance scores generated successfully",
                summary=SchoolScoreGenerationSummary(**summary),
            )
        except SchoolNotFoundError as exc:
            db.rollback()
            _finalize_job_run_record(
                session_factory=_lock_session_factory,
                run_id=run_id,
                status="failed",
                error_message=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            )
        except SchoolInactiveError as exc:
            db.rollback()
            _finalize_job_run_record(
                session_factory=_lock_session_factory,
                run_id=run_id,
                status="failed",
                error_message=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        except AcademicYearResolutionError as exc:
            db.rollback()
            _finalize_job_run_record(
                session_factory=_lock_session_factory,
                run_id=run_id,
                status="failed",
                error_message=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.exception(
                "Manual recalculation failed for school_id=%s score_month=%s",
                school_id,
                payload.score_month,
            )
            _finalize_job_run_record(
                session_factory=_lock_session_factory,
                run_id=run_id,
                status="failed",
                error_message=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to recalculate school student performance scores",
            )
