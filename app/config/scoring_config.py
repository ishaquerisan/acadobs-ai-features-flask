"""Centralized, configurable scoring rules for the Monthly Student Performance Scoring System."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


CALCULATION_VERSION = "1.0.0"

DEFAULT_COMPONENT_WEIGHTS: Dict[str, float] = {
    "academic": 0.55,
    "attendance": 0.10,
    "homework": 0.15,
    "achievement": 0.15,
    "co_scholastic": 0.05,
}

DEFAULT_ACADEMIC_CATEGORY_WEIGHTS: Dict[str, float] = {
    "main_term": 0.60,
    "other_exam": 0.25,
    "regular_internal": 0.15,
}

DEFAULT_HOMEWORK_METRIC_WEIGHTS: Dict[str, float] = {
    "completion": 0.60,
    "performance": 0.40,
}

DEFAULT_MAIN_TERM_KEYWORDS: Tuple[str, ...] = (
    "main",
    "term",
    "main exam",
    "term exam",
    "term 1",
    "term 2",
    "term 3",
    "midterm",
    "mid-term",
    "mid term",
    "final term",
)

DEFAULT_ACHIEVEMENT_LEVEL_POINTS: Dict[str, float] = {
    "class": 10.0,
    "school": 20.0,
    "district": 35.0,
    "state": 50.0,
    "national": 70.0,
    "international": 100.0,
}

DEFAULT_ACHIEVEMENT_PRIZE_MULTIPLIERS: Dict[str, float] = {
    "1st prize": 1.00,
    "2nd prize": 0.80,
    "3rd prize": 0.60,
    "participant": 0.20,
    "other": 0.10,
}


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _parse_json_dict_env(name: str, default: Dict[str, float]) -> Dict[str, float]:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return dict(default)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            result: Dict[str, float] = {}
            for key, value in parsed.items():
                result[str(key).strip().lower()] = float(value)
            return result
    except (ValueError, TypeError):
        pass
    return dict(default)


def _parse_grade_map_env(name: str) -> Dict[str, float]:
    """
    Parse optional school-approved co-scholastic grade-to-score mapping from env JSON.
    Example: SCORING_CO_SCHOLASTIC_GRADE_MAP='{"A+": 95, "A": 85, "B": 70, "C": 55}'
    Defaults to empty dict so unapproved grade mappings are never invented silently.
    """
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            result: Dict[str, float] = {}
            for key, value in parsed.items():
                score = float(value)
                if 0.0 <= score <= 100.0:
                    result[str(key).strip().upper()] = score
            return result
    except (ValueError, TypeError):
        pass
    return {}


def _parse_keywords_env(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    items = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return items or default


@dataclass(frozen=True)
class ScoringConfig:
    """Immutable configuration object governing all scoring calculations and scheduling."""

    calculation_version: str = CALCULATION_VERSION

    # Overall component weights (must sum to 1.0)
    component_weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_COMPONENT_WEIGHTS)
    )

    # Academic internal category weights (must sum to 1.0)
    academic_category_weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_ACADEMIC_CATEGORY_WEIGHTS)
    )

    # Case-insensitive keywords matched with word boundaries on internal_marks.internal_name
    main_term_keywords: Tuple[str, ...] = DEFAULT_MAIN_TERM_KEYWORDS

    # Examination absence policy for marks.status != 'present':
    # - 'exclude': matches existing Acadobs policy (absent marks are not converted to 0)
    # - 'zero': count recorded absent marks as 0 obtained marks
    absent_mark_policy: str = "exclude"

    # Attendance policy
    # Late counts as present (1.0) by default; configurable to 0.0..1.0
    late_attendance_credit: float = 1.0
    # Whether approved student leave_requests excuse recorded 'absent' attendance rows
    apply_approved_leave_requests: bool = True

    # Homework internal weights (must sum to 1.0)
    homework_metric_weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_HOMEWORK_METRIC_WEIGHTS)
    )
    # Rubric max points for homework_assignments.points (no alteration to existing homeworks table).
    # Acadobs reportController.js uses a 1-5 point rating scale for homework_assignments.points.
    default_homework_max_points: Optional[float] = 5.0

    # Achievement scoring parameters
    achievement_level_points: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_ACHIEVEMENT_LEVEL_POINTS)
    )
    achievement_prize_multipliers: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_ACHIEVEMENT_PRIZE_MULTIPLIERS)
    )
    achievement_score_cap: float = 100.0
    # If True: when a school has recorded achievement activity in the period,
    # students with 0 achievements receive 0.0 (real zero); if the school has NO
    # recorded achievements at all in the period, achievement is marked unavailable.
    achievement_require_school_activity_for_zero: bool = True

    # Co-scholastic school-approved grade-to-score map (empty by default: do not invent a mapping)
    co_scholastic_grade_map: Dict[str, float] = field(default_factory=dict)

    # Fallback academic year start from environment (only used if schools.education_year_start is NULL)
    fallback_education_year_start: Optional[str] = None

    # Batch processing & scheduler configuration
    student_batch_size: int = 250
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Kolkata"
    scheduler_day_of_month: int = 1
    scheduler_hour: int = 0
    scheduler_minute: int = 10
    scheduler_lock_timeout_seconds: int = 5
    scheduler_max_retries: int = 2
    scheduler_retry_delay_seconds: int = 30

    def compile_main_term_patterns(self) -> List[re.Pattern[str]]:
        """Compile word-boundary case-insensitive regex patterns for Main/Term classification."""
        patterns: List[re.Pattern[str]] = []
        for kw in self.main_term_keywords:
            cleaned = kw.strip()
            if not cleaned:
                continue
            escaped = re.escape(cleaned).replace(r"\ ", r"[\s\-_]+")
            patterns.append(re.compile(rf"(?i)\b{escaped}\b"))
        return patterns

    def is_main_term_assessment(self, exam_id: Optional[int], internal_name: Optional[str]) -> bool:
        """Return True iff exam_id is present and internal_name matches configured Main/Term rules."""
        if exam_id is None:
            return False
        name = (internal_name or "").strip()
        if not name:
            return False
        return any(pattern.search(name) is not None for pattern in self.compile_main_term_patterns())


def load_scoring_config() -> ScoringConfig:
    """Build ScoringConfig from environment variables with safe defaults."""
    absent_policy = os.getenv("SCORING_ABSENT_MARK_POLICY", "exclude").strip().lower()
    if absent_policy not in ("exclude", "zero"):
        absent_policy = "exclude"

    hw_max_raw = os.getenv("SCORING_HOMEWORK_DEFAULT_MAX_POINTS", "5.0").strip()
    default_hw_max: Optional[float]
    if hw_max_raw.lower() in ("none", "null", ""):
        default_hw_max = None
    else:
        try:
            parsed_hw_max = float(hw_max_raw)
            default_hw_max = parsed_hw_max if parsed_hw_max > 0 else None
        except ValueError:
            default_hw_max = 5.0

    fallback_edu_start = os.getenv("EDUCATION_YEAR_START")
    if fallback_edu_start is not None:
        fallback_edu_start = fallback_edu_start.strip() or None

    late_credit = _parse_float_env("SCORING_LATE_ATTENDANCE_CREDIT", 1.0)
    late_credit = max(0.0, min(1.0, late_credit))

    return ScoringConfig(
        calculation_version=os.getenv("SCORING_CALCULATION_VERSION", CALCULATION_VERSION).strip()
        or CALCULATION_VERSION,
        component_weights=dict(DEFAULT_COMPONENT_WEIGHTS),
        academic_category_weights=dict(DEFAULT_ACADEMIC_CATEGORY_WEIGHTS),
        main_term_keywords=_parse_keywords_env(
            "SCORING_MAIN_TERM_KEYWORDS", DEFAULT_MAIN_TERM_KEYWORDS
        ),
        absent_mark_policy=absent_policy,
        late_attendance_credit=late_credit,
        apply_approved_leave_requests=_parse_bool_env(
            "SCORING_APPLY_APPROVED_LEAVE_REQUESTS", True
        ),
        homework_metric_weights=dict(DEFAULT_HOMEWORK_METRIC_WEIGHTS),
        default_homework_max_points=default_hw_max,
        achievement_level_points=_parse_json_dict_env(
            "SCORING_ACHIEVEMENT_LEVEL_POINTS", DEFAULT_ACHIEVEMENT_LEVEL_POINTS
        ),
        achievement_prize_multipliers=_parse_json_dict_env(
            "SCORING_ACHIEVEMENT_PRIZE_MULTIPLIERS", DEFAULT_ACHIEVEMENT_PRIZE_MULTIPLIERS
        ),
        achievement_score_cap=_parse_float_env("SCORING_ACHIEVEMENT_SCORE_CAP", 100.0),
        achievement_require_school_activity_for_zero=_parse_bool_env(
            "SCORING_ACHIEVEMENT_REQUIRE_SCHOOL_ACTIVITY", True
        ),
        co_scholastic_grade_map=_parse_grade_map_env("SCORING_CO_SCHOLASTIC_GRADE_MAP"),
        fallback_education_year_start=fallback_edu_start,
        student_batch_size=max(10, _parse_int_env("SCORING_STUDENT_BATCH_SIZE", 250)),
        scheduler_enabled=_parse_bool_env("PERFORMANCE_SCHEDULER_ENABLED", True),
        scheduler_timezone=os.getenv("PERFORMANCE_SCHEDULER_TIMEZONE", "Asia/Kolkata").strip()
        or "Asia/Kolkata",
        scheduler_day_of_month=max(1, min(28, _parse_int_env("PERFORMANCE_SCHEDULER_DAY", 1))),
        scheduler_hour=max(0, min(23, _parse_int_env("PERFORMANCE_SCHEDULER_HOUR", 0))),
        scheduler_minute=max(0, min(59, _parse_int_env("PERFORMANCE_SCHEDULER_MINUTE", 10))),
        scheduler_lock_timeout_seconds=max(
            1, _parse_int_env("PERFORMANCE_SCHEDULER_LOCK_TIMEOUT", 5)
        ),
        scheduler_max_retries=max(0, _parse_int_env("PERFORMANCE_SCHEDULER_MAX_RETRIES", 2)),
        scheduler_retry_delay_seconds=max(
            1, _parse_int_env("PERFORMANCE_SCHEDULER_RETRY_DELAY_SECONDS", 30)
        ),
    )
