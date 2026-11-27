"""Academic Marks Scoring Service (55% overall weight).

Internal category weights:
- Main/Term exams (`exam_id IS NOT NULL` + configurable name match): 60%
- Other exams (`exam_id IS NOT NULL`, not Main/Term): 25%
- Regular internal (`exam_id IS NULL`): 15%
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config.scoring_config import ScoringConfig


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        num = float(value)
        if num != num:  # NaN guard
            return None
        return num
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AcademicComponentResult:
    """Normalized academic score breakdown for a single student."""

    available: bool
    academic_score: Optional[float]
    main_term_score: Optional[float]
    other_exam_score: Optional[float]
    regular_internal_score: Optional[float]
    effective_category_weights: Dict[str, float] = field(default_factory=dict)
    valid_assessment_count: int = 0
    absent_excluded_count: int = 0
    invalid_records_ignored: int = 0
    category_counts: Dict[str, int] = field(default_factory=dict)
    reason: Optional[str] = None


def classify_academic_category(
    exam_id: Optional[int],
    internal_name: Optional[str],
    config: ScoringConfig,
) -> str:
    """Classify an assessment into 'main_term', 'other_exam', or 'regular_internal'."""
    if exam_id is None:
        return "regular_internal"
    if config.is_main_term_assessment(exam_id, internal_name):
        return "main_term"
    return "other_exam"


def calculate_academic_score_from_rows(
    rows: Sequence[Mapping[str, Any]],
    config: ScoringConfig,
) -> AcademicComponentResult:
    """
    Pure deterministic academic score computation for one student's mark rows.

    Aggregation strategy:
      1. Deduplicate by `internal_id` (keep latest mark row by `mark_id` if duplicates exist).
      2. Respect `marks.status` according to `config.absent_mark_policy` ('exclude' or 'zero').
      3. Normalize every valid assessment via `(marks_obtained / max_marks) * 100`.
      4. Average normalized assessment percentages per `(category, subject_id)`, then
         average across subjects within each category so neither differing `max_marks`
         nor unequal assessment counts per subject bias the category score.
      5. Combine available categories using 60% / 25% / 15% weights, proportionally
         renormalizing when any category is unavailable.
    """
    if not rows:
        return AcademicComponentResult(
            available=False,
            academic_score=None,
            main_term_score=None,
            other_exam_score=None,
            regular_internal_score=None,
            reason="no_academic_records",
        )

    # Deduplicate by internal_id (last row wins when ordered by mark_id ASC)
    deduped_by_internal: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        internal_id = row.get("internal_id")
        if internal_id is None:
            continue
        existing = deduped_by_internal.get(int(internal_id))
        if existing is None or int(row.get("mark_id") or 0) >= int(existing.get("mark_id") or 0):
            deduped_by_internal[int(internal_id)] = row

    # category -> subject_key -> list of assessment percentages
    category_subject_scores: Dict[str, Dict[Tuple[str, Any], List[float]]] = {
        "main_term": defaultdict(list),
        "other_exam": defaultdict(list),
        "regular_internal": defaultdict(list),
    }
    category_counts: Dict[str, int] = {
        "main_term": 0,
        "other_exam": 0,
        "regular_internal": 0,
    }

    valid_count = 0
    absent_excluded_count = 0
    invalid_ignored = 0

    for row in deduped_by_internal.values():
        status_str = str(row.get("mark_status") or "").strip().lower()
        maximum = _safe_float(row.get("max_marks"))

        if maximum is None or maximum <= 0:
            invalid_ignored += 1
            continue

        if status_str == "present":
            obtained = _safe_float(row.get("marks_obtained"))
            if obtained is None or obtained < 0 or obtained > maximum:
                invalid_ignored += 1
                continue
        elif status_str == "absent":
            if config.absent_mark_policy == "zero":
                obtained = 0.0
            else:
                absent_excluded_count += 1
                continue
        else:
            # Unknown or blank status: do not silently fabricate zero
            invalid_ignored += 1
            continue

        pct = (obtained / maximum) * 100.0
        category = classify_academic_category(
            row.get("exam_id"),
            row.get("internal_name"),
            config,
        )

        subject_id = row.get("subject_id")
        subject_key = ("subject", int(subject_id)) if subject_id is not None else (
            "unassigned",
            int(row["internal_id"]),
        )
        category_subject_scores[category][subject_key].append(pct)
        category_counts[category] += 1
        valid_count += 1

    category_scores: Dict[str, Optional[float]] = {}
    for category, subject_map in category_subject_scores.items():
        if not subject_map:
            category_scores[category] = None
            continue
        subject_averages = [
            sum(scores) / len(scores) for scores in subject_map.values() if scores
        ]
        category_scores[category] = (
            sum(subject_averages) / len(subject_averages) if subject_averages else None
        )

    available_categories = [
        cat for cat, score in category_scores.items() if score is not None
    ]

    if not available_categories:
        return AcademicComponentResult(
            available=False,
            academic_score=None,
            main_term_score=None,
            other_exam_score=None,
            regular_internal_score=None,
            valid_assessment_count=0,
            absent_excluded_count=absent_excluded_count,
            invalid_records_ignored=invalid_ignored,
            category_counts=category_counts,
            reason="no_valid_academic_marks",
        )

    base_weights = config.academic_category_weights
    total_available_weight = sum(base_weights[cat] for cat in available_categories)
    effective_weights: Dict[str, float] = {
        cat: base_weights[cat] / total_available_weight for cat in available_categories
    }

    academic_score = sum(
        category_scores[cat] * effective_weights[cat]  # type: ignore[operator]
        for cat in available_categories
    )

    return AcademicComponentResult(
        available=True,
        academic_score=round(academic_score, 4),
        main_term_score=(
            round(category_scores["main_term"], 2)
            if category_scores["main_term"] is not None
            else None
        ),
        other_exam_score=(
            round(category_scores["other_exam"], 2)
            if category_scores["other_exam"] is not None
            else None
        ),
        regular_internal_score=(
            round(category_scores["regular_internal"], 2)
            if category_scores["regular_internal"] is not None
            else None
        ),
        effective_category_weights={
            cat: round(weight, 6) for cat, weight in effective_weights.items()
        },
        valid_assessment_count=valid_count,
        absent_excluded_count=absent_excluded_count,
        invalid_records_ignored=invalid_ignored,
        category_counts=category_counts,
    )


def fetch_and_calculate_academic_scores(
    db: Session,
    school_id: int,
    student_ids: Iterable[int],
    period_start: date,
    period_end: date,
    config: ScoringConfig,
) -> Dict[int, AcademicComponentResult]:
    """
    Bulk-fetch academic marks for a batch of students in a single query (no N+1)
    and compute academic component results per student.
    """
    student_id_list = [int(sid) for sid in student_ids]
    if not student_id_list:
        return {}

    query = text(
        """
        SELECT
            m.id AS mark_id,
            m.student_id,
            m.internal_id,
            m.marks_obtained,
            m.status AS mark_status,
            im.subject_id,
            im.internal_name,
            im.max_marks,
            im.exam_id,
            im.date AS assessment_date
        FROM marks m
        INNER JOIN internal_marks im
            ON im.id = m.internal_id
        LEFT JOIN exams e
            ON e.id = im.exam_id
        WHERE m.student_id IN :student_ids
          AND im.school_id = :school_id
          AND (im.trash IS NULL OR im.trash = 0)
          AND im.date >= :period_start
          AND im.date <= :period_end
          AND (
              im.exam_id IS NULL
              OR (e.id IS NOT NULL AND e.school_id = :school_id AND (e.trash IS NULL OR e.trash = 0))
          )
        ORDER BY m.student_id ASC, im.id ASC, m.id ASC
        """
    ).bindparams(bindparam("student_ids", expanding=True))

    rows = db.execute(
        query,
        {
            "student_ids": student_id_list,
            "school_id": school_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    ).mappings().all()

    rows_by_student: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_student[int(row["student_id"])].append(row)

    return {
        sid: calculate_academic_score_from_rows(rows_by_student.get(sid, ()), config)
        for sid in student_id_list
    }
