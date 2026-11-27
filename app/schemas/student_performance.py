"""Pydantic request and response models for the Monthly Student Performance Scoring API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


ALLOWED_SORT_FIELDS = (
    "overall_score",
    "academic_score",
    "attendance_score",
    "homework_score",
    "achievement_score",
)


def normalize_score_month(value: date | str) -> date:
    """
    Parse and normalize `score_month` to the first day of the target month (YYYY-MM-01).
    Accepts `date`, `YYYY-MM-DD`, or `YYYY-MM`.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.replace(day=1)

    if isinstance(value, str):
        cleaned = value.strip()
        if len(cleaned) == 7 and cleaned[4] == "-":
            cleaned = f"{cleaned}-01"
        parsed = date.fromisoformat(cleaned)
        return parsed.replace(day=1)

    raise ValueError("score_month must be a valid date in YYYY-MM or YYYY-MM-DD format")


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class RecalculateSchoolScoresRequest(BaseModel):
    """Request body for POST /api/v1/schools/{school_id}/student-performance/recalculate."""

    model_config = ConfigDict(extra="forbid")

    score_month: date = Field(
        ...,
        description="Snapshot month represented as YYYY-MM-01 (or YYYY-MM; normalized to first day of month).",
        examples=["2025-10-01"],
    )
    force_recalculate: bool = Field(
        default=False,
        description="When true, recalculates and updates existing snapshots even if already computed for the month.",
    )

    @field_validator("score_month", mode="before")
    @classmethod
    def _validate_score_month(cls, value: Any) -> date:
        return normalize_score_month(value)


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class ClassSummary(BaseModel):
    id: Optional[int] = None
    classname: Optional[str] = None
    year: Optional[int] = None
    division: Optional[str] = None


class SchoolSummary(BaseModel):
    id: int
    name: Optional[str] = None


class StudentIdentity(BaseModel):
    id: int
    full_name: str
    reg_no: Optional[str] = None
    roll_number: Optional[int] = None


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total_records: int
    total_pages: int
    has_next: bool
    has_previous: bool


# ---------------------------------------------------------------------------
# School-wide paginated response schemas
# ---------------------------------------------------------------------------


class ComponentScoresSummary(BaseModel):
    academic_score: Optional[float] = None
    attendance_score: Optional[float] = None
    homework_score: Optional[float] = None
    achievement_score: Optional[float] = None
    co_scholastic_score: Optional[float] = None


class SchoolStudentScoreItem(BaseModel):
    student_id: int
    student_name: str
    reg_no: Optional[str] = None
    roll_number: Optional[int] = None
    class_details: Optional[ClassSummary] = None
    academic_year: str
    score_month: date
    overall_score: Optional[float] = None
    rank_in_class: Optional[int] = None
    component_scores: ComponentScoresSummary
    calculation_status: str
    calculation_version: Optional[str] = None
    calculated_at: datetime


class SchoolStudentPerformanceListResponse(BaseModel):
    success: bool = True
    school_id: int
    score_month: Optional[date] = None
    academic_year: Optional[str] = None
    pagination: PaginationMeta
    total_records: int
    items: List[SchoolStudentScoreItem]


# ---------------------------------------------------------------------------
# Individual student detailed breakdown response schemas
# ---------------------------------------------------------------------------


class AcademicBreakdown(BaseModel):
    academic_score: Optional[float] = None
    academic_contribution: Optional[float] = None
    main_term_score: Optional[float] = None
    other_exam_score: Optional[float] = None
    regular_internal_score: Optional[float] = None


class AttendanceBreakdown(BaseModel):
    attendance_score: Optional[float] = None
    attendance_contribution: Optional[float] = None


class HomeworkBreakdown(BaseModel):
    homework_score: Optional[float] = None
    homework_completion_score: Optional[float] = None
    homework_performance_score: Optional[float] = None
    homework_contribution: Optional[float] = None


class AchievementBreakdown(BaseModel):
    achievement_score: Optional[float] = None
    achievement_points: Optional[float] = None
    achievement_count: int = 0
    achievement_contribution: Optional[float] = None


class CoScholasticBreakdown(BaseModel):
    co_scholastic_score: Optional[float] = None
    co_scholastic_contribution: Optional[float] = None


class StudentPerformanceDetailData(BaseModel):
    student: StudentIdentity
    school: SchoolSummary
    class_details: Optional[ClassSummary] = None
    academic_year: str
    score_month: date
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    overall_score: Optional[float] = None
    rank_in_class: Optional[int] = None
    academic: AcademicBreakdown
    attendance: AttendanceBreakdown
    homework: HomeworkBreakdown
    achievement: AchievementBreakdown
    co_scholastic: CoScholasticBreakdown
    effective_weights: Optional[Dict[str, Any]] = None
    component_availability: Optional[Dict[str, Any]] = None
    calculation_status: str
    calculation_version: Optional[str] = None
    calculated_at: datetime


class StudentPerformanceDetailResponse(BaseModel):
    success: bool = True
    data: StudentPerformanceDetailData


# ---------------------------------------------------------------------------
# Snapshot generation / manual recalculation response schemas
# ---------------------------------------------------------------------------


class SchoolScoreGenerationSummary(BaseModel):
    school_id: int
    academic_year: str
    score_month: date
    period_start: date
    period_end: date
    total_students: int
    processed_students: int
    successful_students: int
    insufficient_data_students: int
    skipped_students: int = 0
    failed_students: int
    execution_time: float = Field(..., description="Wall-clock execution time in seconds.")
    calculation_version: str
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class RecalculateSchoolScoresResponse(BaseModel):
    success: bool = True
    message: str
    summary: SchoolScoreGenerationSummary


SortByOption = Literal[
    "overall_score",
    "academic_score",
    "attendance_score",
    "homework_score",
    "achievement_score",
]
SortOrderOption = Literal["asc", "desc"]
