"""Co-scholastic Scoring Service (5% overall weight).

Rules:
- Co-scholastic assessment is OPTIONAL and only applies to eligible/assessed students.
- Use numeric `score` when it is valid on a known 0–100 scale.
- If only `grade` is present, convert using a school-approved configurable grade map
  (`config.co_scholastic_grade_map`). Never invent an unapproved mapping.
- Deduplicate by `(exam_id, area_id)` and average across areas appropriately.
- If no applicable assessment exists, mark the component unavailable (`None`) — never 0.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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
        if num != num:
            return None
        return num
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CoScholasticComponentResult:
    """Normalized co-scholastic score breakdown for a single student."""

    available: bool
    co_scholastic_score: Optional[float]
    assessed_area_count: int = 0
    valid_assessment_count: int = 0
    unmapped_grade_count: int = 0
    reason: Optional[str] = None


def calculate_co_scholastic_score_from_rows(
    rows: Sequence[Mapping[str, Any]],
    config: ScoringConfig,
) -> CoScholasticComponentResult:
    """
    Pure deterministic co-scholastic score computation for one student.

    Deduplicates `(exam_id, area_id)` pairs, averages multiple exams within each `area_id`,
    and then averages across all assessed areas.
    """
    if not rows:
        return CoScholasticComponentResult(
            available=False,
            co_scholastic_score=None,
            reason="no_co_scholastic_assessments",
        )

    # Deduplicate by (exam_id, area_id) keeping latest assessment id
    deduped: Dict[Tuple[int, int], Mapping[str, Any]] = {}
    for row in rows:
        exam_id = row.get("exam_id")
        area_id = row.get("area_id")
        if exam_id is None or area_id is None:
            continue
        key = (int(exam_id), int(area_id))
        existing = deduped.get(key)
        if existing is None or int(row.get("assessment_id") or 0) >= int(
            existing.get("assessment_id") or 0
        ):
            deduped[key] = row

    area_scores: Dict[int, List[float]] = defaultdict(list)
    valid_count = 0
    unmapped_grade_count = 0

    for (exam_id, area_id), row in deduped.items():
        numeric_score = _safe_float(row.get("score"))
        resolved_score: Optional[float] = None

        if numeric_score is not None and 0.0 <= numeric_score <= 100.0:
            resolved_score = numeric_score
        else:
            grade_raw = row.get("grade")
            if grade_raw is not None and str(grade_raw).strip():
                grade_key = str(grade_raw).strip().upper()
                mapped = config.co_scholastic_grade_map.get(grade_key)
                if mapped is not None and 0.0 <= mapped <= 100.0:
                    resolved_score = float(mapped)
                else:
                    unmapped_grade_count += 1

        if resolved_score is not None:
            area_scores[area_id].append(resolved_score)
            valid_count += 1

    if not area_scores:
        reason = (
            "grade_mapping_not_configured"
            if unmapped_grade_count > 0
            else "no_valid_co_scholastic_scores"
        )
        return CoScholasticComponentResult(
            available=False,
            co_scholastic_score=None,
            assessed_area_count=0,
            valid_assessment_count=0,
            unmapped_grade_count=unmapped_grade_count,
            reason=reason,
        )

    area_averages = [sum(vals) / len(vals) for vals in area_scores.values() if vals]
    final_score = sum(area_averages) / len(area_averages)

    return CoScholasticComponentResult(
        available=True,
        co_scholastic_score=round(final_score, 4),
        assessed_area_count=len(area_averages),
        valid_assessment_count=valid_count,
        unmapped_grade_count=unmapped_grade_count,
    )


def fetch_and_calculate_co_scholastic_scores(
    db: Session,
    school_id: int,
    student_ids: Iterable[int],
    period_start: date,
    period_end: date,
    config: ScoringConfig,
) -> Dict[int, CoScholasticComponentResult]:
    """Bulk-fetch co-scholastic assessments for a batch of students (single SQL query, no N+1)."""
    student_id_list = [int(sid) for sid in student_ids]
    if not student_id_list:
        return {}

    query = text(
        """
        SELECT
            sca.id AS assessment_id,
            sca.student_id,
            sca.exam_id,
            sca.area_id,
            sca.grade,
            sca.score,
            COALESCE(sca.assessed_at, DATE(sca.createdAt)) AS assessment_date
        FROM student_co_scholastic_assessments sca
        INNER JOIN co_scholastic_areas ca
            ON ca.id = sca.area_id
        INNER JOIN exams e
            ON e.id = sca.exam_id
        WHERE sca.student_id IN :student_ids
          AND sca.school_id = :school_id
          AND ca.status = 1
          AND e.school_id = :school_id
          AND (e.trash IS NULL OR e.trash = 0)
          AND COALESCE(sca.assessed_at, DATE(sca.createdAt)) >= :period_start
          AND COALESCE(sca.assessed_at, DATE(sca.createdAt)) <= :period_end
        ORDER BY sca.student_id ASC, sca.exam_id ASC, sca.area_id ASC, sca.id ASC
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
        sid: calculate_co_scholastic_score_from_rows(rows_by_student.get(sid, ()), config)
        for sid in student_id_list
    }
