"""Achievement Scoring Service (15% overall weight).

Level Base Points (configurable):
- class: 10, school: 20, district: 35, state: 50, national: 70, international: 100

Prize Multipliers (configurable):
- 1st prize: 1.00, 2nd prize: 0.80, 3rd prize: 0.60, participant: 0.20, other: 0.10

Formula:
- Achievement Points = Level Base Points * Prize Multiplier
- Total Achievement Points = Sum of valid achievement points
- Achievement Score = MIN(Total Achievement Points, 100)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config.scoring_config import ScoringConfig


@dataclass(frozen=True)
class AchievementComponentResult:
    """Normalized achievement score breakdown for a single student."""

    available: bool
    achievement_score: Optional[float]
    achievement_points: Optional[float]
    achievement_count: int = 0
    invalid_records_ignored: int = 0
    reason: Optional[str] = None


def calculate_achievement_points(
    level: Optional[str],
    status: Optional[str],
    config: ScoringConfig,
) -> Optional[float]:
    """
    Compute points for a single achievement record.
    Returns None if `level` or `status` is missing or invalid (never silently guesses).
    """
    norm_level = str(level or "").strip().lower()
    norm_status = str(status or "").strip().lower()

    if norm_level not in config.achievement_level_points:
        return None
    if norm_status not in config.achievement_prize_multipliers:
        return None

    base_points = config.achievement_level_points[norm_level]
    multiplier = config.achievement_prize_multipliers[norm_status]
    return base_points * multiplier


def calculate_achievement_score_from_rows(
    rows: Sequence[Mapping[str, Any]],
    config: ScoringConfig,
    school_has_achievement_activity: bool = True,
) -> AchievementComponentResult:
    """
    Pure deterministic achievement calculation for one student.

    Distinguishes between:
    - Real zero performance (`school_has_achievement_activity=True` and student has 0 valid achievements -> score 0.0)
    - Missing/unavailable component (`school_has_achievement_activity=False` and student has 0 achievements -> unavailable/None)
    """
    # Deduplicate by achievement_id to prevent duplicate counting
    deduped: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        ach_id = row.get("achievement_id")
        if ach_id is None:
            continue
        existing = deduped.get(int(ach_id))
        if existing is None or int(row.get("student_achievement_id") or 0) >= int(
            existing.get("student_achievement_id") or 0
        ):
            deduped[int(ach_id)] = row

    total_points = 0.0
    valid_count = 0
    invalid_count = 0

    for row in deduped.values():
        pts = calculate_achievement_points(row.get("level"), row.get("status"), config)
        if pts is None:
            invalid_count += 1
            continue
        total_points += pts
        valid_count += 1

    if valid_count == 0:
        if config.achievement_require_school_activity_for_zero and not school_has_achievement_activity:
            return AchievementComponentResult(
                available=False,
                achievement_score=None,
                achievement_points=None,
                achievement_count=0,
                invalid_records_ignored=invalid_count,
                reason="no_school_achievement_activity",
            )
        return AchievementComponentResult(
            available=True,
            achievement_score=0.0,
            achievement_points=0.0,
            achievement_count=0,
            invalid_records_ignored=invalid_count,
            reason="zero_student_achievements",
        )

    capped_score = min(total_points, config.achievement_score_cap)
    return AchievementComponentResult(
        available=True,
        achievement_score=round(capped_score, 4),
        achievement_points=round(total_points, 2),
        achievement_count=valid_count,
        invalid_records_ignored=invalid_count,
    )


def school_has_valid_achievements_in_period(
    db: Session,
    school_id: int,
    period_start: date,
    period_end: date,
) -> bool:
    """Check once per school whether any non-trashed achievement records exist in the snapshot period."""
    row = db.execute(
        text(
            """
            SELECT 1 AS found
            FROM student_achievement sa
            INNER JOIN achievements a
                ON a.id = sa.achievement_id
            WHERE a.school_id = :school_id
              AND (a.trash IS NULL OR a.trash = 0)
              AND (sa.trash IS NULL OR sa.trash = 0)
              AND a.date >= :period_start
              AND a.date <= :period_end
              AND a.level IS NOT NULL
              AND sa.status IS NOT NULL
            LIMIT 1
            """
        ),
        {
            "school_id": school_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    ).first()
    return row is not None


def fetch_and_calculate_achievement_scores(
    db: Session,
    school_id: int,
    student_ids: Iterable[int],
    period_start: date,
    period_end: date,
    config: ScoringConfig,
    school_has_activity: Optional[bool] = None,
) -> Dict[int, AchievementComponentResult]:
    """Bulk-fetch student achievements for a batch of students (single SQL query, no N+1)."""
    student_id_list = [int(sid) for sid in student_ids]
    if not student_id_list:
        return {}

    if school_has_activity is None:
        school_has_activity = school_has_valid_achievements_in_period(
            db, school_id, period_start, period_end
        )

    query = text(
        """
        SELECT
            sa.id AS student_achievement_id,
            sa.achievement_id,
            sa.student_id,
            sa.status,
            a.level,
            a.date AS achievement_date
        FROM student_achievement sa
        INNER JOIN achievements a
            ON a.id = sa.achievement_id
        WHERE sa.student_id IN :student_ids
          AND a.school_id = :school_id
          AND (a.trash IS NULL OR a.trash = 0)
          AND (sa.trash IS NULL OR sa.trash = 0)
          AND a.date >= :period_start
          AND a.date <= :period_end
        ORDER BY sa.student_id ASC, sa.achievement_id ASC, sa.id ASC
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
        sid: calculate_achievement_score_from_rows(
            rows_by_student.get(sid, ()),
            config,
            school_has_achievement_activity=school_has_activity,
        )
        for sid in student_id_list
    }
