"""Homework Scoring Service (15% overall weight).

Internal weights:
- Completion percentage: 60%
- Performance/quality percentage: 40%

Uses ONLY the existing Acadobs `homeworks` and `homework_assignments` schema (no table alterations):
- `is_seen` ONLY indicates a guardian/student viewed the homework; it is NEVER treated as
  evidence of submission or completion.
- Completion is determined strictly from real submission/evaluation signals in existing columns:
  `solved_file` (non-empty uploaded submission) OR `points > 0` (teacher-evaluated work;
  `points` defaults to 0 on assignment creation).
- Performance is calculated from `homework_assignments.points` normalized against the
  configurable rubric maximum `config.default_homework_max_points` (default `5.0`, matching
  the 1–5 point rating scale already used in Acadobs `reportController.js`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

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
class HomeworkComponentResult:
    """Normalized homework score breakdown for a single student."""

    available: bool
    homework_score: Optional[float]
    completion_score: Optional[float]
    performance_score: Optional[float]
    total_assigned_due: int = 0
    completed_count: int = 0
    graded_count: int = 0
    effective_metric_weights: Dict[str, float] = field(default_factory=dict)
    reason: Optional[str] = None


def _is_assignment_completed(row: Mapping[str, Any]) -> bool:
    """
    Determine whether a homework assignment was actually submitted/completed using existing columns.
    NEVER inspects or relies upon `is_seen`.
    """
    solved_file = str(row.get("solved_file") or "").strip()
    if solved_file:
        return True

    points = _safe_float(row.get("points"))
    if points is not None and points > 0:
        return True

    return False


def _is_assignment_graded(row: Mapping[str, Any]) -> bool:
    """
    Determine whether teacher-awarded `points` represents an actual evaluation.
    In `homework_assignments`, `points` defaults to 0 on creation, so `points > 0`
    reliably indicates teacher-awarded points.
    """
    points = _safe_float(row.get("points"))
    return points is not None and points > 0


def calculate_homework_score_from_rows(
    rows: Sequence[Mapping[str, Any]],
    config: ScoringConfig,
) -> HomeworkComponentResult:
    """Pure deterministic homework score computation for one student."""
    if not rows:
        return HomeworkComponentResult(
            available=False,
            homework_score=None,
            completion_score=None,
            performance_score=None,
            reason="no_assigned_due_homework",
        )

    # Deduplicate by homework_id (keep latest assignment id if duplicates exist)
    deduped: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        hw_id = row.get("homework_id")
        if hw_id is None:
            continue
        existing = deduped.get(int(hw_id))
        if existing is None or int(row.get("assignment_id") or 0) >= int(existing.get("assignment_id") or 0):
            deduped[int(hw_id)] = row

    total_assigned = len(deduped)
    if total_assigned == 0:
        return HomeworkComponentResult(
            available=False,
            homework_score=None,
            completion_score=None,
            performance_score=None,
            reason="no_assigned_due_homework",
        )

    completed_count = 0
    performance_percentages: List[float] = []
    max_points = config.default_homework_max_points

    for row in deduped.values():
        if _is_assignment_completed(row):
            completed_count += 1

        if _is_assignment_graded(row):
            points = _safe_float(row.get("points"))
            if points is not None and max_points is not None and max_points > 0:
                pct = min((points / max_points) * 100.0, 100.0)
                performance_percentages.append(pct)

    completion_pct = (completed_count / total_assigned) * 100.0
    graded_count = len(performance_percentages)
    performance_pct: Optional[float] = (
        (sum(performance_percentages) / graded_count) if graded_count > 0 else None
    )

    completion_w = config.homework_metric_weights["completion"]
    performance_w = config.homework_metric_weights["performance"]

    if performance_pct is not None:
        total_w = completion_w + performance_w
        eff_comp_w = completion_w / total_w
        eff_perf_w = performance_w / total_w
        hw_score = (completion_pct * eff_comp_w) + (performance_pct * eff_perf_w)
        effective_metric_weights = {
            "completion": round(eff_comp_w, 6),
            "performance": round(eff_perf_w, 6),
        }
    else:
        # No graded point scores available (or rubric max disabled).
        # Never treat raw points as percentages; use completion alone (100% internal weight).
        hw_score = completion_pct
        effective_metric_weights = {"completion": 1.0}

    return HomeworkComponentResult(
        available=True,
        homework_score=round(hw_score, 4),
        completion_score=round(completion_pct, 2),
        performance_score=round(performance_pct, 2) if performance_pct is not None else None,
        total_assigned_due=total_assigned,
        completed_count=completed_count,
        graded_count=graded_count,
        effective_metric_weights=effective_metric_weights,
    )


def fetch_and_calculate_homework_scores(
    db: Session,
    school_id: int,
    student_ids: Iterable[int],
    period_start: date,
    period_end: date,
    config: ScoringConfig,
) -> Dict[int, HomeworkComponentResult]:
    """
    Bulk-fetch due homework assignments for a batch of students (single SQL query, no N+1)
    reading strictly from existing `homeworks` and `homework_assignments` columns.
    """
    student_id_list = [int(sid) for sid in student_ids]
    if not student_id_list:
        return {}

    query = text(
        """
        SELECT
            ha.id AS assignment_id,
            ha.homework_id,
            ha.student_id,
            ha.points,
            ha.solved_file,
            h.type AS homework_type,
            h.due_date
        FROM homework_assignments ha
        INNER JOIN homeworks h
            ON h.id = ha.homework_id
        WHERE ha.student_id IN :student_ids
          AND h.school_id = :school_id
          AND (h.trash IS NULL OR h.trash = 0)
          AND h.due_date IS NOT NULL
          AND h.due_date >= :period_start
          AND h.due_date <= :period_end
        ORDER BY ha.student_id ASC, ha.homework_id ASC, ha.id ASC
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
        sid: calculate_homework_score_from_rows(rows_by_student.get(sid, ()), config)
        for sid in student_id_list
    }
