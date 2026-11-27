"""Overall Score Calculator, Dynamic Weight Redistribution, and Class Competition Ranking."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence

from app.config.scoring_config import ScoringConfig
from app.services.performance_scoring.academic_scoring import AcademicComponentResult
from app.services.performance_scoring.achievement_scoring import AchievementComponentResult
from app.services.performance_scoring.attendance_scoring import AttendanceComponentResult
from app.services.performance_scoring.co_scholastic_scoring import CoScholasticComponentResult
from app.services.performance_scoring.homework_scoring import HomeworkComponentResult


def round2(value: Optional[float]) -> Optional[Decimal]:
    """Deterministic HALF_UP rounding to 2 decimal places for financial/score precision."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ComputedStudentScore:
    """Fully computed snapshot payload for one student before database persistence."""

    student_id: int
    school_id: int
    class_id: Optional[int]

    academic_score: Optional[Decimal]
    academic_contribution: Optional[Decimal]
    main_term_score: Optional[Decimal]
    other_exam_score: Optional[Decimal]
    regular_internal_score: Optional[Decimal]

    attendance_score: Optional[Decimal]
    attendance_contribution: Optional[Decimal]

    homework_score: Optional[Decimal]
    homework_completion_score: Optional[Decimal]
    homework_performance_score: Optional[Decimal]
    homework_contribution: Optional[Decimal]

    achievement_score: Optional[Decimal]
    achievement_points: Optional[Decimal]
    achievement_count: int
    achievement_contribution: Optional[Decimal]

    co_scholastic_score: Optional[Decimal]
    co_scholastic_contribution: Optional[Decimal]

    overall_score: Optional[Decimal]
    effective_weights: Dict[str, Any]
    component_availability: Dict[str, Any]
    calculation_status: str  # 'completed' | 'insufficient_data'
    rank_in_class: Optional[int] = None


def combine_student_components(
    student_id: int,
    school_id: int,
    class_id: Optional[int],
    academic: AcademicComponentResult,
    attendance: AttendanceComponentResult,
    homework: HomeworkComponentResult,
    achievement: AchievementComponentResult,
    co_scholastic: CoScholasticComponentResult,
    config: ScoringConfig,
) -> ComputedStudentScore:
    """
    Combine the five component results into an overall score out of 100.

    Missing-data rules:
    - Unavailable components are excluded and their base weights are proportionally
      redistributed across available components so effective weights sum to 1.0 (100%).
    - A student with NO meaningful personal data across academic, attendance, homework,
      co-scholastic, AND zero achievements is saved as `insufficient_data` with
      `overall_score = None` rather than manufacturing an artificial 0.0 score.
    """
    component_availability: Dict[str, Any] = {
        "academic": {
            "available": academic.available,
            "reason": academic.reason,
            "valid_assessment_count": academic.valid_assessment_count,
            "absent_excluded_count": academic.absent_excluded_count,
            "invalid_records_ignored": academic.invalid_records_ignored,
            "category_counts": academic.category_counts,
        },
        "attendance": {
            "available": attendance.available,
            "reason": attendance.reason,
            "eligible_sessions": attendance.eligible_sessions,
            "present_count": attendance.present_count,
            "late_count": attendance.late_count,
            "absent_count": attendance.absent_count,
            "leave_excluded_count": attendance.leave_excluded_count,
            "approved_leave_excused_count": attendance.approved_leave_excused_count,
        },
        "homework": {
            "available": homework.available,
            "reason": homework.reason,
            "total_assigned_due": homework.total_assigned_due,
            "completed_count": homework.completed_count,
            "graded_count": homework.graded_count,
            "effective_metric_weights": homework.effective_metric_weights,
        },
        "achievement": {
            "available": achievement.available,
            "reason": achievement.reason,
            "achievement_count": achievement.achievement_count,
            "invalid_records_ignored": achievement.invalid_records_ignored,
        },
        "co_scholastic": {
            "available": co_scholastic.available,
            "reason": co_scholastic.reason,
            "assessed_area_count": co_scholastic.assessed_area_count,
            "valid_assessment_count": co_scholastic.valid_assessment_count,
            "unmapped_grade_count": co_scholastic.unmapped_grade_count,
        },
    }

    has_meaningful_personal_activity = (
        academic.available
        or attendance.available
        or homework.available
        or co_scholastic.available
        or (achievement.available and achievement.achievement_count > 0)
    )

    if not has_meaningful_personal_activity:
        return ComputedStudentScore(
            student_id=student_id,
            school_id=school_id,
            class_id=class_id,
            academic_score=None,
            academic_contribution=None,
            main_term_score=None,
            other_exam_score=None,
            regular_internal_score=None,
            attendance_score=None,
            attendance_contribution=None,
            homework_score=None,
            homework_completion_score=None,
            homework_performance_score=None,
            homework_contribution=None,
            achievement_score=None,
            achievement_points=None,
            achievement_count=0,
            achievement_contribution=None,
            co_scholastic_score=None,
            co_scholastic_contribution=None,
            overall_score=None,
            effective_weights={
                "components": {},
                "academic_categories": {},
            },
            component_availability=component_availability,
            calculation_status="insufficient_data",
            rank_in_class=None,
        )

    raw_scores: Dict[str, Optional[float]] = {
        "academic": academic.academic_score if academic.available else None,
        "attendance": attendance.attendance_score if attendance.available else None,
        "homework": homework.homework_score if homework.available else None,
        "achievement": achievement.achievement_score if achievement.available else None,
        "co_scholastic": co_scholastic.co_scholastic_score if co_scholastic.available else None,
    }

    available_keys = [k for k, v in raw_scores.items() if v is not None]
    base_weights = config.component_weights
    total_available_base_weight = sum(base_weights[k] for k in available_keys)

    effective_component_weights: Dict[str, float] = {
        k: base_weights[k] / total_available_base_weight for k in available_keys
    }

    contributions: Dict[str, Optional[float]] = {}
    overall_unrounded = 0.0
    for key in ("academic", "attendance", "homework", "achievement", "co_scholastic"):
        score_val = raw_scores[key]
        if score_val is None:
            contributions[key] = None
        else:
            contrib = score_val * effective_component_weights[key]
            contributions[key] = contrib
            overall_unrounded += contrib

    # Store clean 6-decimal normalized weights that sum to 1.0
    rounded_weights: Dict[str, float] = {
        k: round(w, 6) for k, w in effective_component_weights.items()
    }
    if rounded_weights:
        drift = round(1.0 - sum(rounded_weights.values()), 6)
        if drift != 0.0:
            # Absorb floating-point rounding drift into the highest-weight available component
            largest_key = max(rounded_weights, key=lambda k: rounded_weights[k])
            rounded_weights[largest_key] = round(rounded_weights[largest_key] + drift, 6)

    effective_weights_payload: Dict[str, Any] = {
        "components": rounded_weights,
        "academic_categories": academic.effective_category_weights if academic.available else {},
    }

    return ComputedStudentScore(
        student_id=student_id,
        school_id=school_id,
        class_id=class_id,
        academic_score=round2(raw_scores["academic"]),
        academic_contribution=round2(contributions["academic"]),
        main_term_score=round2(academic.main_term_score),
        other_exam_score=round2(academic.other_exam_score),
        regular_internal_score=round2(academic.regular_internal_score),
        attendance_score=round2(raw_scores["attendance"]),
        attendance_contribution=round2(contributions["attendance"]),
        homework_score=round2(raw_scores["homework"]),
        homework_completion_score=round2(homework.completion_score),
        homework_performance_score=round2(homework.performance_score),
        homework_contribution=round2(contributions["homework"]),
        achievement_score=round2(raw_scores["achievement"]),
        achievement_points=round2(achievement.achievement_points),
        achievement_count=achievement.achievement_count,
        achievement_contribution=round2(contributions["achievement"]),
        co_scholastic_score=round2(raw_scores["co_scholastic"]),
        co_scholastic_contribution=round2(contributions["co_scholastic"]),
        overall_score=round2(overall_unrounded),
        effective_weights=effective_weights_payload,
        component_availability=component_availability,
        calculation_status="completed",
        rank_in_class=None,
    )


def assign_class_competition_ranks(scores: Sequence[ComputedStudentScore]) -> None:
    """
    Assign `rank_in_class` in-place using standard competition ranking (1, 2, 2, 4).

    Rules:
    - Partitioned strictly by `(school_id, class_id)`.
    - Students with `class_id is None`, `calculation_status != 'completed'`, or
      `overall_score is None` receive `rank_in_class = None`.
    - Ordered by `overall_score` descending. Exact ties receive identical rank;
      no hidden tie-breakers are applied.
    """
    by_class: Dict[tuple[int, int], List[ComputedStudentScore]] = defaultdict(list)

    for item in scores:
        item.rank_in_class = None
        if (
            item.class_id is not None
            and item.calculation_status == "completed"
            and item.overall_score is not None
        ):
            by_class[(item.school_id, item.class_id)].append(item)

    for group in by_class.values():
        group.sort(key=lambda s: (-s.overall_score, s.student_id))  # type: ignore[operator]
        previous_score: Optional[Decimal] = None
        current_rank = 0
        for position, item in enumerate(group, start=1):
            if previous_score is None or item.overall_score != previous_score:
                current_rank = position
                previous_score = item.overall_score
            item.rank_in_class = current_rank
