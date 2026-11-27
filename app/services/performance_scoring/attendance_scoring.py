"""Attendance Scoring Service (10% overall weight).

Rules:
- `present` counts as present.
- `late` counts as present (credit 1.0) by default; configurable via `late_attendance_credit`.
- `absent` counts as absent UNLESS excused by an approved student leave request covering
  that date (and half-day section if applicable), in which case it is treated as `leave`.
- `leave` is excluded from the eligible session denominator.
- Missing attendance rows are NEVER treated as absences.
- If there are no eligible recorded sessions, mark attendance as unavailable (`None`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config.scoring_config import ScoringConfig


def _to_date(value: Any) -> Optional[date]:
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


@dataclass(frozen=True)
class AttendanceComponentResult:
    """Normalized attendance score and session counts for a single student."""

    available: bool
    attendance_score: Optional[float]
    eligible_sessions: int = 0
    present_count: int = 0
    late_count: int = 0
    absent_count: int = 0
    leave_excluded_count: int = 0
    approved_leave_excused_count: int = 0
    reason: Optional[str] = None


def _is_covered_by_approved_leave(
    session_date: Optional[date],
    session_period: Optional[int],
    approved_leaves: Sequence[Mapping[str, Any]],
    school_attendance_count: int,
) -> bool:
    """Return True if an approved student leave request covers this date and period."""
    if session_date is None or not approved_leaves:
        return False

    periods_per_day = max(1, int(school_attendance_count or 2))
    forenoon_cutoff = math.ceil(periods_per_day / 2)

    for leave in approved_leaves:
        from_dt = _to_date(leave.get("from_date"))
        to_dt = _to_date(leave.get("to_date"))
        if from_dt is None or to_dt is None:
            continue
        if not (from_dt <= session_date <= to_dt):
            continue

        duration = str(leave.get("leave_duration") or "full").strip().lower()
        if duration != "half":
            return True

        half_section = str(leave.get("half_section") or "").strip().lower()
        if session_period is None or half_section not in ("forenoon", "afternoon"):
            return True

        period_num = int(session_period)
        if half_section == "forenoon" and period_num <= forenoon_cutoff:
            return True
        if half_section == "afternoon" and period_num > forenoon_cutoff:
            return True

    return False


def calculate_attendance_score_from_rows(
    attendance_rows: Sequence[Mapping[str, Any]],
    approved_leaves: Sequence[Mapping[str, Any]],
    config: ScoringConfig,
    school_attendance_count: int = 2,
) -> AttendanceComponentResult:
    """Pure deterministic attendance calculation for one student."""
    if not attendance_rows:
        return AttendanceComponentResult(
            available=False,
            attendance_score=None,
            reason="no_attendance_sessions",
        )

    # Deduplicate by attendance_id (keep latest attendance_marked.id if duplicates exist)
    deduped: Dict[int, Mapping[str, Any]] = {}
    for row in attendance_rows:
        att_id = row.get("attendance_id")
        if att_id is None:
            continue
        existing = deduped.get(int(att_id))
        if existing is None or int(row.get("marked_id") or 0) >= int(existing.get("marked_id") or 0):
            deduped[int(att_id)] = row

    present_count = 0
    late_count = 0
    absent_count = 0
    leave_excluded_count = 0
    approved_leave_excused_count = 0

    for row in deduped.values():
        status_str = str(row.get("status") or "").strip().lower()
        if status_str == "present":
            present_count += 1
        elif status_str == "late":
            late_count += 1
        elif status_str == "leave":
            leave_excluded_count += 1
        elif status_str == "absent":
            if config.apply_approved_leave_requests and _is_covered_by_approved_leave(
                _to_date(row.get("session_date")),
                row.get("period"),
                approved_leaves,
                school_attendance_count,
            ):
                leave_excluded_count += 1
                approved_leave_excused_count += 1
            else:
                absent_count += 1

    eligible_sessions = present_count + late_count + absent_count
    if eligible_sessions == 0:
        return AttendanceComponentResult(
            available=False,
            attendance_score=None,
            eligible_sessions=0,
            present_count=0,
            late_count=0,
            absent_count=0,
            leave_excluded_count=leave_excluded_count,
            approved_leave_excused_count=approved_leave_excused_count,
            reason="no_eligible_attendance_sessions",
        )

    present_credit = float(present_count) + (float(late_count) * config.late_attendance_credit)
    percentage = (present_credit / float(eligible_sessions)) * 100.0

    return AttendanceComponentResult(
        available=True,
        attendance_score=round(percentage, 4),
        eligible_sessions=eligible_sessions,
        present_count=present_count,
        late_count=late_count,
        absent_count=absent_count,
        leave_excluded_count=leave_excluded_count,
        approved_leave_excused_count=approved_leave_excused_count,
    )


def fetch_and_calculate_attendance_scores(
    db: Session,
    school_id: int,
    student_ids: Iterable[int],
    period_start: date,
    period_end: date,
    config: ScoringConfig,
    school_attendance_count: int = 2,
) -> Dict[int, AttendanceComponentResult]:
    """
    Bulk-fetch attendance records and approved student leave requests for a batch
    of students (2 bulk queries total, zero N+1) and compute scores per student.
    """
    student_id_list = [int(sid) for sid in student_ids]
    if not student_id_list:
        return {}

    att_query = text(
        """
        SELECT
            am.id AS marked_id,
            am.attendance_id,
            am.student_id,
            am.status,
            a.date AS session_date,
            a.period
        FROM attendance_marked am
        INNER JOIN attendance a
            ON a.id = am.attendance_id
        WHERE am.student_id IN :student_ids
          AND a.school_id = :school_id
          AND (a.trash IS NULL OR a.trash = 0)
          AND a.date >= :period_start
          AND a.date <= :period_end
        ORDER BY am.student_id ASC, am.attendance_id ASC, am.id ASC
        """
    ).bindparams(bindparam("student_ids", expanding=True))

    att_rows = db.execute(
        att_query,
        {
            "student_ids": student_id_list,
            "school_id": school_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    ).mappings().all()

    att_by_student: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for row in att_rows:
        att_by_student[int(row["student_id"])].append(row)

    leaves_by_student: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    if config.apply_approved_leave_requests:
        leave_query = text(
            """
            SELECT
                student_id,
                from_date,
                to_date,
                leave_duration,
                half_section
            FROM leave_requests
            WHERE student_id IN :student_ids
              AND school_id = :school_id
              AND role = 'student'
              AND status = 'approved'
              AND (trash IS NULL OR trash = 0)
              AND from_date <= :period_end
              AND to_date >= :period_start
            """
        ).bindparams(bindparam("student_ids", expanding=True))

        leave_rows = db.execute(
            leave_query,
            {
                "student_ids": student_id_list,
                "school_id": school_id,
                "period_start": period_start,
                "period_end": period_end,
            },
        ).mappings().all()

        for row in leave_rows:
            if row["student_id"] is not None:
                leaves_by_student[int(row["student_id"])].append(row)

    return {
        sid: calculate_attendance_score_from_rows(
            att_by_student.get(sid, ()),
            leaves_by_student.get(sid, ()),
            config,
            school_attendance_count=school_attendance_count,
        )
        for sid in student_id_list
    }
