"""Academic-year boundary resolution for monthly cumulative snapshots."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.scoring_config import ScoringConfig


class AcademicYearResolutionError(ValueError):
    """Raised when a reliable academic-year start date cannot be determined for a school."""


@dataclass(frozen=True)
class AcademicYearPeriod:
    """Resolved cumulative calculation window for a school and snapshot month."""

    school_id: int
    academic_year: str
    score_month: date
    period_start: date
    period_end: date
    source: str  # 'schools.education_year_start' | 'env.EDUCATION_YEAR_START'


def parse_date_value(value: object) -> Optional[date]:
    """Safely convert DATE/DATETIME/ISO string into a `date` object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return date.fromisoformat(cleaned[:10])
        except ValueError:
            return None
    return None


def month_bounds(score_month: date | str) -> tuple[date, date]:
    """Return (first_day_of_month, last_day_of_month) for a snapshot month."""
    if isinstance(score_month, str):
        cleaned = score_month.strip()
        if len(cleaned) == 7 and cleaned[4] == "-":
            cleaned = f"{cleaned}-01"
        parsed = date.fromisoformat(cleaned)
    elif isinstance(score_month, datetime):
        parsed = score_month.date()
    elif isinstance(score_month, date):
        parsed = score_month
    else:
        raise ValueError("Invalid score_month value")

    first_day = parsed.replace(day=1)
    last_day_num = calendar.monthrange(first_day.year, first_day.month)[1]
    last_day = first_day.replace(day=last_day_num)
    return first_day, last_day


def previous_month_first_day(reference_date: date) -> date:
    """
    Given a run date (e.g., 2025-11-01), return the first day of the previous month (2025-10-01).
    """
    if reference_date.month == 1:
        return date(reference_date.year - 1, 12, 1)
    return date(reference_date.year, reference_date.month - 1, 1)


def derive_academic_year_label(period_start: date) -> str:
    """Derive a canonical academic year string when no `exams.education_year` row matches."""
    if period_start.month == 1:
        return str(period_start.year)
    return f"{period_start.year}-{period_start.year + 1}"


def _find_exam_academic_year(
    db: Session,
    school_id: int,
    period_start: date,
    period_end: date,
) -> Optional[str]:
    """
    Inspect non-trashed `exams.education_year` linked to `internal_marks` inside the period.
    Returns the most frequent non-empty `education_year` string, or None if none exist.
    """
    row = db.execute(
        text(
            """
            SELECT e.education_year, COUNT(*) AS cnt
            FROM exams e
            INNER JOIN internal_marks im
                ON im.exam_id = e.id
            WHERE e.school_id = :school_id
              AND (e.trash IS NULL OR e.trash = 0)
              AND (im.trash IS NULL OR im.trash = 0)
              AND im.date >= :period_start
              AND im.date <= :period_end
              AND e.education_year IS NOT NULL
              AND TRIM(e.education_year) <> ''
            GROUP BY e.education_year
            ORDER BY cnt DESC, e.education_year DESC
            LIMIT 1
            """
        ),
        {
            "school_id": school_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    ).mappings().first()

    if row and row["education_year"]:
        return str(row["education_year"]).strip()
    return None


def resolve_academic_year_period(
    db: Session,
    school_id: int,
    school_education_year_start: object,
    score_month: date | str,
    config: ScoringConfig,
) -> AcademicYearPeriod:
    """
    Resolve cumulative snapshot boundaries `[period_start, period_end]` and `academic_year`.

    Priority:
      1. `schools.education_year_start` (primary source in Acadobs schema)
      2. `EDUCATION_YEAR_START` environment fallback (matches Acadobs backend convention)

    Never silently guesses a default start date if neither source is configured or if the
    resolved start date falls after `period_end`.
    """
    first_day, period_end = month_bounds(score_month)

    period_start = parse_date_value(school_education_year_start)
    source = "schools.education_year_start"

    if period_start is None and config.fallback_education_year_start:
        period_start = parse_date_value(config.fallback_education_year_start)
        source = "env.EDUCATION_YEAR_START"

    if period_start is None:
        raise AcademicYearResolutionError(
            f"Cannot determine academic-year start date for school_id={school_id}. "
            "Set `schools.education_year_start` (or configure `EDUCATION_YEAR_START`)."
        )

    if period_start > period_end:
        raise AcademicYearResolutionError(
            f"Resolved academic-year start date ({period_start.isoformat()}) for school_id={school_id} "
            f"is after snapshot period_end ({period_end.isoformat()}) for score_month={first_day.isoformat()}."
        )

    academic_year = _find_exam_academic_year(db, school_id, period_start, period_end)
    if not academic_year:
        academic_year = derive_academic_year_label(period_start)

    return AcademicYearPeriod(
        school_id=school_id,
        academic_year=academic_year,
        score_month=first_day,
        period_start=period_start,
        period_end=period_end,
        source=source,
    )
