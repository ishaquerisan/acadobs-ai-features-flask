"""
Comprehensive Unit & Integration Test Suite for the Monthly Student Performance Scoring System.

Covers all 21 mandatory verification scenarios:
 1. Main/Term receives 60% of academic weight.
 2. Other exams receive 25%.
 3. Regular internal receives 15%.
 4. Marks are normalized using max_marks.
 5. Missing academic categories are handled through weight normalization.
 6. Attendance leave is excluded from the denominator.
 7. Late is handled consistently with the configured policy.
 8. Missing attendance rows are not treated as absences.
 9. Homework is_seen is never interpreted as submission completion.
10. Achievement level points are correct.
11. Achievement prize multipliers are correct.
12. Achievement score is capped at 100.
13. Missing co-scholastic data does not penalize ineligible students.
14. Missing data is different from an actual zero score.
15. Effective weights add up to 100%.
16. Running the same monthly job twice does not create duplicate rows.
17. School filters prevent cross-school data leakage.
18. Class pagination and sorting work correctly.
19. Parents cannot access another student's score.
20. Class ranks correctly handle ties (competition ranking: 1, 2, 2, 4).
21. Historical monthly snapshots remain available after a new month is calculated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.scoring_config import ScoringConfig
from app.database.connection import get_db
from app.models.student_performance import Base
from app.routers.student_performance import router as performance_router
from app.services.performance_scoring.academic_scoring import (
    AcademicComponentResult,
    calculate_academic_score_from_rows,
)
from app.services.performance_scoring.academic_year import (
    AcademicYearResolutionError,
    previous_month_first_day,
    resolve_academic_year_period,
)
from app.services.performance_scoring.achievement_scoring import (
    AchievementComponentResult,
    calculate_achievement_points,
    calculate_achievement_score_from_rows,
)
from app.services.performance_scoring.attendance_scoring import (
    AttendanceComponentResult,
    calculate_attendance_score_from_rows,
)
from app.services.performance_scoring.co_scholastic_scoring import (
    CoScholasticComponentResult,
    calculate_co_scholastic_score_from_rows,
)
from app.services.performance_scoring.homework_scoring import (
    HomeworkComponentResult,
    calculate_homework_score_from_rows,
)
from app.services.performance_scoring.scoring_calculator import (
    ComputedStudentScore,
    assign_class_competition_ranks,
    combine_student_components,
)
from app.services.performance_scoring.snapshot_service import (
    generate_school_student_scores,
    get_school_student_scores,
    get_student_performance_score,
)


TEST_JWT_SECRET = "unit-test-acadobs-jwt-secret-key"


def _make_hs256_jwt(payload: dict, secret: str = TEST_JWT_SECRET) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    def _b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    h = _b64(header)
    p = _b64(payload)
    sig = hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{h}.{p}.{s}"


def _create_acadobs_schema(engine) -> None:
    """Create minimal existing Acadobs MySQL tables in SQLite for integration testing."""
    ddl_statements = [
        """
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(255) NOT NULL,
            education_year_start DATE NULL,
            attendance_count INTEGER DEFAULT 2,
            status VARCHAR(20) DEFAULT 'active',
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(255) NOT NULL,
            email VARCHAR(255),
            phone VARCHAR(50),
            password VARCHAR(255),
            school_id INTEGER NOT NULL,
            role VARCHAR(30) NOT NULL,
            status VARCHAR(20) DEFAULT 'active',
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            refresh_token VARCHAR(500) NOT NULL,
            expires_at DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            division VARCHAR(20) NOT NULL,
            classname VARCHAR(255) NOT NULL,
            school_id INTEGER NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE staffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role VARCHAR(20) NOT NULL,
            class_id INTEGER NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE staff_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            students BOOLEAN DEFAULT 0,
            reports BOOLEAN DEFAULT 0,
            aiAnalytics BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE timetables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            class_id INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            period_number INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            staff_id INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            guardian_id INTEGER NOT NULL,
            reg_no VARCHAR(100),
            roll_number INTEGER,
            full_name VARCHAR(255) NOT NULL,
            class_id INTEGER,
            status VARCHAR(20) DEFAULT 'active',
            alumni BOOLEAN DEFAULT 0,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            exam_name VARCHAR(255) NOT NULL,
            education_year VARCHAR(255) NOT NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE internal_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            class_id INTEGER,
            subject_id INTEGER,
            internal_name VARCHAR(255),
            max_marks DECIMAL(5,2),
            date DATE,
            exam_id INTEGER NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            internal_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            marks_obtained DECIMAL(5,2),
            status VARCHAR(20) DEFAULT 'present'
        )
        """,
        """
        CREATE TABLE attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            class_id INTEGER NOT NULL,
            period INTEGER NOT NULL,
            date DATE NOT NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE attendance_marked (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attendance_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL
        )
        """,
        """
        CREATE TABLE leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            student_id INTEGER NULL,
            role VARCHAR(20) DEFAULT 'student',
            reason TEXT,
            leave_duration VARCHAR(20) DEFAULT 'full',
            half_section VARCHAR(20) NULL,
            from_date DATE NOT NULL,
            to_date DATE NOT NULL,
            status VARCHAR(20) DEFAULT 'pending',
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE homeworks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            teacher_id INTEGER,
            class_id INTEGER,
            subject_id INTEGER,
            title VARCHAR(255),
            due_date DATE,
            type VARCHAR(20),
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE homework_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            homework_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            remarks VARCHAR(255),
            points INTEGER DEFAULT 0,
            solved_file VARCHAR(255),
            is_seen BOOLEAN DEFAULT 0,
            createdAt DATETIME,
            updatedAt DATETIME
        )
        """,
        """
        CREATE TABLE achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            category VARCHAR(50),
            level VARCHAR(50),
            date DATE NOT NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE student_achievement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            achievement_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            status VARCHAR(50) NOT NULL,
            trash BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE co_scholastic_areas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER,
            name VARCHAR(255) NOT NULL,
            status BOOLEAN DEFAULT 1
        )
        """,
        """
        CREATE TABLE student_co_scholastic_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            exam_id INTEGER NOT NULL,
            area_id INTEGER NOT NULL,
            grade VARCHAR(10),
            score DECIMAL(5,2),
            assessed_at DATE,
            createdAt DATETIME
        )
        """,
    ]
    with engine.begin() as conn:
        for ddl in ddl_statements:
            conn.execute(text(ddl))
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _create_acadobs_schema(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    test_app = FastAPI()
    test_app.include_router(performance_router)

    def _override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    with TestClient(test_app) as client:
        yield client


# ===========================================================================
# UNIT TESTS: ACADEMIC SCORING (Requirements 1, 2, 3, 4, 5)
# ===========================================================================


def test_academic_weights_60_25_15_and_max_marks_normalization():
    """Tests 1, 2, 3, 4: Main/Term 60%, Other 25%, Regular Internal 15%, normalized by max_marks."""
    cfg = ScoringConfig()
    rows = [
        # Main/Term: 40/50 = 80%
        {
            "mark_id": 1,
            "internal_id": 101,
            "subject_id": 1,
            "exam_id": 10,
            "internal_name": "Term 1 Exam",
            "max_marks": 50,
            "marks_obtained": 40,
            "mark_status": "present",
        },
        # Other exam: 18/20 = 90%
        {
            "mark_id": 2,
            "internal_id": 102,
            "subject_id": 1,
            "exam_id": 11,
            "internal_name": "Unit Test 1",
            "max_marks": 20,
            "marks_obtained": 18,
            "mark_status": "present",
        },
        # Regular internal: 10/10 = 100%
        {
            "mark_id": 3,
            "internal_id": 103,
            "subject_id": 1,
            "exam_id": None,
            "internal_name": "Weekly Quiz",
            "max_marks": 10,
            "marks_obtained": 10,
            "mark_status": "present",
        },
    ]

    result = calculate_academic_score_from_rows(rows, cfg)
    assert result.available is True
    assert result.main_term_score == pytest.approx(80.0, abs=0.01)
    assert result.other_exam_score == pytest.approx(90.0, abs=0.01)
    assert result.regular_internal_score == pytest.approx(100.0, abs=0.01)
    # 80*0.60 + 90*0.25 + 100*0.15 = 48 + 22.5 + 15 = 85.5
    assert result.academic_score == pytest.approx(85.5, abs=0.01)
    assert result.effective_category_weights == {
        "main_term": pytest.approx(0.60, abs=1e-6),
        "other_exam": pytest.approx(0.25, abs=1e-6),
        "regular_internal": pytest.approx(0.15, abs=1e-6),
    }


def test_academic_missing_category_weight_normalization():
    """Test 5: Missing other_exam proportionally normalizes main_term (0.60/0.75=0.80) and regular (0.15/0.75=0.20)."""
    cfg = ScoringConfig()
    rows = [
        {
            "mark_id": 1,
            "internal_id": 201,
            "subject_id": 1,
            "exam_id": 1,
            "internal_name": "Main Exam",
            "max_marks": 100,
            "marks_obtained": 70,
            "mark_status": "present",
        },
        {
            "mark_id": 2,
            "internal_id": 202,
            "subject_id": 1,
            "exam_id": None,
            "internal_name": "Class Test",
            "max_marks": 25,
            "marks_obtained": 25,
            "mark_status": "present",
        },
    ]
    result = calculate_academic_score_from_rows(rows, cfg)
    assert result.available is True
    assert result.other_exam_score is None
    assert sum(result.effective_category_weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert result.effective_category_weights["main_term"] == pytest.approx(0.80, abs=1e-6)
    assert result.effective_category_weights["regular_internal"] == pytest.approx(0.20, abs=1e-6)
    # 70 * 0.80 + 100 * 0.20 = 56 + 20 = 76.0
    assert result.academic_score == pytest.approx(76.0, abs=0.01)


def test_academic_absent_and_invalid_max_marks_safety():
    """Absent marks are excluded by default (Acadobs policy) and zero/null max_marks are safely ignored."""
    cfg = ScoringConfig(absent_mark_policy="exclude")
    rows = [
        {
            "mark_id": 1,
            "internal_id": 301,
            "subject_id": 1,
            "exam_id": 1,
            "internal_name": "Term 1",
            "max_marks": 100,
            "marks_obtained": 80,
            "mark_status": "present",
        },
        {
            "mark_id": 2,
            "internal_id": 302,
            "subject_id": 2,
            "exam_id": 1,
            "internal_name": "Term 1",
            "max_marks": 100,
            "marks_obtained": 0,
            "mark_status": "absent",
        },
        {
            "mark_id": 3,
            "internal_id": 303,
            "subject_id": 3,
            "exam_id": 1,
            "internal_name": "Term 1",
            "max_marks": 0,
            "marks_obtained": 10,
            "mark_status": "present",
        },
    ]
    result = calculate_academic_score_from_rows(rows, cfg)
    assert result.available is True
    assert result.academic_score == pytest.approx(80.0, abs=0.01)
    assert result.absent_excluded_count == 1
    assert result.invalid_records_ignored == 1


# ===========================================================================
# UNIT TESTS: ATTENDANCE SCORING (Requirements 6, 7, 8)
# ===========================================================================


def test_attendance_leave_excluded_late_policy_and_approved_leave():
    """Tests 6, 7: Leave excluded from denominator; late obeys configured policy; approved leave excuses absent."""
    rows = [
        {"marked_id": 1, "attendance_id": 1, "status": "present", "session_date": date(2025, 10, 1), "period": 1},
        {"marked_id": 2, "attendance_id": 2, "status": "late", "session_date": date(2025, 10, 2), "period": 1},
        {"marked_id": 3, "attendance_id": 3, "status": "absent", "session_date": date(2025, 10, 3), "period": 1},
        {"marked_id": 4, "attendance_id": 4, "status": "leave", "session_date": date(2025, 10, 4), "period": 1},
        # Absent on Oct 5 but covered by approved full-day leave request -> excused as leave
        {"marked_id": 5, "attendance_id": 5, "status": "absent", "session_date": date(2025, 10, 5), "period": 1},
    ]
    approved_leaves = [
        {
            "from_date": date(2025, 10, 5),
            "to_date": date(2025, 10, 5),
            "leave_duration": "full",
            "half_section": None,
        }
    ]

    # Default policy: late counts as present (1.0)
    # Eligible sessions = present(1) + late(1) + unexcused_absent(1) = 3; attended = 2 -> 66.6667%
    cfg_default = ScoringConfig(late_attendance_credit=1.0)
    res_default = calculate_attendance_score_from_rows(rows, approved_leaves, cfg_default)
    assert res_default.available is True
    assert res_default.eligible_sessions == 3
    assert res_default.leave_excluded_count == 2
    assert res_default.approved_leave_excused_count == 1
    assert res_default.attendance_score == pytest.approx(66.6667, abs=0.01)

    # Custom policy: late counts as 0.5
    cfg_half = ScoringConfig(late_attendance_credit=0.5)
    res_half = calculate_attendance_score_from_rows(rows, approved_leaves, cfg_half)
    assert res_half.attendance_score == pytest.approx(50.0, abs=0.01)


def test_attendance_missing_rows_not_treated_as_absent():
    """Test 8: No recorded attendance rows (or only leave rows) -> unavailable, NOT 0%."""
    cfg = ScoringConfig()
    res_empty = calculate_attendance_score_from_rows([], [], cfg)
    assert res_empty.available is False
    assert res_empty.attendance_score is None

    res_only_leave = calculate_attendance_score_from_rows(
        [{"marked_id": 1, "attendance_id": 1, "status": "leave", "session_date": date(2025, 10, 1), "period": 1}],
        [],
        cfg,
    )
    assert res_only_leave.available is False
    assert res_only_leave.attendance_score is None


# ===========================================================================
# UNIT TESTS: HOMEWORK SCORING (Requirement 9)
# ===========================================================================


def test_homework_is_seen_never_counts_as_completion():
    """Test 9: `is_seen=True` with no `solved_file` and `points=0` is NOT completed."""
    cfg = ScoringConfig(default_homework_max_points=5.0)
    rows = [
        {
            "assignment_id": 1,
            "homework_id": 10,
            "points": 0,
            "solved_file": None,
            "is_seen": True,
            "homework_type": "online",
        },
        {
            "assignment_id": 2,
            "homework_id": 11,
            "points": 4,
            "solved_file": "uploads/hw11.pdf",
            "is_seen": True,
            "homework_type": "online",
        },
    ]
    res = calculate_homework_score_from_rows(rows, cfg)
    assert res.available is True
    assert res.total_assigned_due == 2
    assert res.completed_count == 1  # hw10 was only seen, NOT completed!
    assert res.completion_score == pytest.approx(50.0, abs=0.01)
    # Performance on graded hw11: 4 / 5 * 100 = 80.0
    assert res.performance_score == pytest.approx(80.0, abs=0.01)
    # 50 * 0.60 + 80 * 0.40 = 30 + 32 = 62.0
    assert res.homework_score == pytest.approx(62.0, abs=0.01)


# ===========================================================================
# UNIT TESTS: ACHIEVEMENT SCORING (Requirements 10, 11, 12)
# ===========================================================================


def test_achievement_level_points_multipliers_and_cap_at_100():
    """Tests 10, 11, 12: Base points, prize multipliers, and cap at 100."""
    cfg = ScoringConfig()
    # Prompt examples:
    assert calculate_achievement_points("District", "1st prize", cfg) == pytest.approx(35.0)
    assert calculate_achievement_points("State", "2nd prize", cfg) == pytest.approx(40.0)
    assert calculate_achievement_points("National", "participant", cfg) == pytest.approx(14.0)
    assert calculate_achievement_points("Class", "3rd prize", cfg) == pytest.approx(6.0)
    assert calculate_achievement_points("School", "other", cfg) == pytest.approx(2.0)
    assert calculate_achievement_points("International", "1st prize", cfg) == pytest.approx(100.0)
    # Invalid level ignored
    assert calculate_achievement_points("galactic", "1st prize", cfg) is None

    # Sum = 35 + 40 + 70 = 145 -> capped at 100
    rows = [
        {"student_achievement_id": 1, "achievement_id": 1, "level": "district", "status": "1st prize"},
        {"student_achievement_id": 2, "achievement_id": 2, "level": "state", "status": "2nd prize"},
        {"student_achievement_id": 3, "achievement_id": 3, "level": "national", "status": "1st prize"},
    ]
    res = calculate_achievement_score_from_rows(rows, cfg, school_has_achievement_activity=True)
    assert res.available is True
    assert res.achievement_count == 3
    assert res.achievement_points == pytest.approx(145.0, abs=0.01)
    assert res.achievement_score == pytest.approx(100.0, abs=0.01)


# ===========================================================================
# UNIT TESTS: CO-SCHOLASTIC, MISSING VS ZERO, EFFECTIVE WEIGHTS (Req 13, 14, 15)
# ===========================================================================


def test_missing_co_scholastic_does_not_penalize_and_weights_sum_to_100():
    """Tests 13, 15: Ineligible student without co-scholastic gets 100% effective weights redistributed."""
    cfg = ScoringConfig()
    co_missing = calculate_co_scholastic_score_from_rows([], cfg)
    assert co_missing.available is False
    assert co_missing.co_scholastic_score is None

    # Student scores 100 across all 4 available components; co-scholastic is unavailable
    combined = combine_student_components(
        student_id=1,
        school_id=1,
        class_id=10,
        academic=AcademicComponentResult(available=True, academic_score=100.0, main_term_score=100.0, other_exam_score=None, regular_internal_score=None, effective_category_weights={"main_term": 1.0}),
        attendance=AttendanceComponentResult(available=True, attendance_score=100.0, eligible_sessions=10, present_count=10),
        homework=HomeworkComponentResult(available=True, homework_score=100.0, completion_score=100.0, performance_score=100.0, total_assigned_due=2, completed_count=2, graded_count=2),
        achievement=AchievementComponentResult(available=True, achievement_score=100.0, achievement_points=100.0, achievement_count=1),
        co_scholastic=co_missing,
        config=cfg,
    )
    assert combined.calculation_status == "completed"
    # Not penalized! Overall is 100.00, NOT 95.00
    assert combined.overall_score == Decimal("100.00")
    assert combined.co_scholastic_score is None
    assert sum(combined.effective_weights["components"].values()) == pytest.approx(1.0, abs=1e-6)


def test_missing_data_vs_actual_zero_score():
    """Test 14: Student with zero data -> insufficient_data (overall=None); student with 0/100 marks -> completed (0.00)."""
    cfg = ScoringConfig()
    no_data = combine_student_components(
        student_id=1,
        school_id=1,
        class_id=10,
        academic=AcademicComponentResult(available=False, academic_score=None, main_term_score=None, other_exam_score=None, regular_internal_score=None),
        attendance=AttendanceComponentResult(available=False, attendance_score=None),
        homework=HomeworkComponentResult(available=False, homework_score=None, completion_score=None, performance_score=None),
        achievement=AchievementComponentResult(available=True, achievement_score=0.0, achievement_points=0.0, achievement_count=0),
        co_scholastic=CoScholasticComponentResult(available=False, co_scholastic_score=None),
        config=cfg,
    )
    assert no_data.calculation_status == "insufficient_data"
    assert no_data.overall_score is None

    real_zero = combine_student_components(
        student_id=2,
        school_id=1,
        class_id=10,
        academic=AcademicComponentResult(available=True, academic_score=0.0, main_term_score=0.0, other_exam_score=None, regular_internal_score=None, effective_category_weights={"main_term": 1.0}, valid_assessment_count=1),
        attendance=AttendanceComponentResult(available=True, attendance_score=0.0, eligible_sessions=5, absent_count=5),
        homework=HomeworkComponentResult(available=False, homework_score=None, completion_score=None, performance_score=None),
        achievement=AchievementComponentResult(available=False, achievement_score=None, achievement_points=None),
        co_scholastic=CoScholasticComponentResult(available=False, co_scholastic_score=None),
        config=cfg,
    )
    assert real_zero.calculation_status == "completed"
    assert real_zero.overall_score == Decimal("0.00")
    assert sum(real_zero.effective_weights["components"].values()) == pytest.approx(1.0, abs=1e-6)


def test_class_competition_ranking_handles_ties():
    """Test 20: Ties receive competition ranking 1, 2, 2, 4; insufficient_data receives rank None."""
    def _stub(sid: int, score: str | None, status: str = "completed") -> ComputedStudentScore:
        return ComputedStudentScore(
            student_id=sid,
            school_id=1,
            class_id=10,
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
            overall_score=Decimal(score) if score is not None else None,
            effective_weights={},
            component_availability={},
            calculation_status=status,
        )

    items = [
        _stub(1, "95.00"),
        _stub(2, "88.50"),
        _stub(3, "88.50"),
        _stub(4, "72.00"),
        _stub(5, None, status="insufficient_data"),
    ]
    assign_class_competition_ranks(items)
    ranks = {item.student_id: item.rank_in_class for item in items}
    assert ranks == {1: 1, 2: 2, 3: 2, 4: 4, 5: None}


def test_academic_year_resolution_reports_missing_start_date(db_session):
    """Academic year resolution raises explicit error when no reliable start date exists."""
    db_session.execute(
        text("INSERT INTO schools (id, name, education_year_start, status, trash) VALUES (99, 'NoDate School', NULL, 'active', 0)")
    )
    db_session.commit()
    cfg = ScoringConfig(fallback_education_year_start=None)
    with pytest.raises(AcademicYearResolutionError):
        resolve_academic_year_period(db_session, 99, None, date(2025, 10, 1), cfg)

    assert previous_month_first_day(date(2025, 11, 1)) == date(2025, 10, 1)
    assert previous_month_first_day(date(2026, 1, 1)) == date(2025, 12, 1)


# ===========================================================================
# INTEGRATION TESTS: DB UPSERT, HISTORICAL SNAPSHOTS, API SECURITY (Req 16-21)
# ===========================================================================


def _seed_multi_school_dataset(db_session):
    """Seed two schools, classes, users (admin, teacher, guardians), students, and scoring data."""
    db_session.execute(
        text("""
            INSERT INTO schools (id, name, education_year_start, attendance_count, status, trash) VALUES
            (1, 'Alpha High', '2025-06-01', 2, 'active', 0),
            (2, 'Beta Academy', '2025-06-01', 2, 'active', 0)
        """)
    )
    db_session.execute(
        text("""
            INSERT INTO classes (id, year, division, classname, school_id, trash) VALUES
            (10, 10, 'A', '10 A', 1, 0),
            (11, 10, 'B', '10 B', 1, 0),
            (20, 10, 'A', '10 A', 2, 0)
        """)
    )
    db_session.execute(
        text("""
            INSERT INTO users (id, name, email, phone, password, school_id, role, status, trash) VALUES
            (1, 'Super Admin', 'sa@test.com', '100', 'x', 1, 'superadmin', 'active', 0),
            (2, 'Alpha Admin', 'a1@test.com', '101', 'x', 1, 'admin', 'active', 0),
            (3, 'Beta Admin', 'a2@test.com', '102', 'x', 2, 'admin', 'active', 0),
            (4, 'Guardian One', 'g1@test.com', '103', 'x', 1, 'guardian', 'active', 0),
            (5, 'Guardian Two', 'g2@test.com', '104', 'x', 1, 'guardian', 'active', 0),
            (6, 'Teacher 10A', 't1@test.com', '105', 'x', 1, 'teacher', 'active', 0)
        """)
    )
    db_session.execute(
        text("INSERT INTO staffs (id, school_id, user_id, role, class_id, trash) VALUES (1, 1, 6, 'teacher', 10, 0)")
    )
    db_session.execute(
        text("""
            INSERT INTO students (id, school_id, guardian_id, reg_no, roll_number, full_name, class_id, status, alumni, trash) VALUES
            (101, 1, 4, 'REG101', 1, 'Aarav Nair', 10, 'active', 0, 0),
            (102, 1, 5, 'REG102', 2, 'Diya Menon', 10, 'active', 0, 0),
            (103, 1, 5, 'REG103', 3, 'Kabir Verma', 10, 'active', 0, 0),
            (104, 1, 4, 'REG104', 1, 'Meera Pillai', 11, 'active', 0, 0),
            (201, 2, 3, 'BREG201', 1, 'Beta Student', 20, 'active', 0, 0)
        """)
    )
    db_session.execute(
        text("""
            INSERT INTO exams (id, school_id, exam_name, education_year, trash) VALUES
            (1, 1, 'First Term', '2025-2026', 0),
            (2, 2, 'First Term', '2025-2026', 0)
        """)
    )
    # October marks for School 1 & School 2
    db_session.execute(
        text("""
            INSERT INTO internal_marks (id, school_id, class_id, subject_id, internal_name, max_marks, date, exam_id, trash) VALUES
            (1, 1, 10, 1, 'Term 1 Exam', 100, '2025-10-15', 1, 0),
            (2, 1, 11, 1, 'Term 1 Exam', 100, '2025-10-15', 1, 0),
            (3, 2, 20, 1, 'Term 1 Exam', 100, '2025-10-15', 2, 0),
            (4, 1, 10, 1, 'Unit Quiz Nov', 50, '2025-11-15', NULL, 0)
        """)
    )
    db_session.execute(
        text("""
            INSERT INTO marks (id, internal_id, student_id, marks_obtained, status) VALUES
            (1, 1, 101, 90, 'present'),
            (2, 1, 102, 90, 'present'),
            (3, 1, 103, 70, 'present'),
            (4, 2, 104, 85, 'present'),
            (5, 3, 201, 99, 'present'),
            (6, 4, 101, 50, 'present')
        """)
    )
    db_session.commit()


def test_idempotent_upsert_no_duplicates_and_historical_snapshots_preserved(db_session):
    """Tests 16 & 21: Running same month twice does not duplicate rows; new month preserves previous month."""
    _seed_multi_school_dataset(db_session)
    cfg = ScoringConfig()

    # Run October snapshot twice (including with force_recalculate=True)
    summary_oct_1 = generate_school_student_scores(db_session, 1, date(2025, 10, 1), force_recalculate=False, config=cfg)
    assert summary_oct_1["successful_students"] == 4
    assert summary_oct_1["failed_students"] == 0

    summary_oct_2 = generate_school_student_scores(db_session, 1, date(2025, 10, 1), force_recalculate=True, config=cfg)
    assert summary_oct_2["successful_students"] == 4

    oct_count = db_session.execute(
        text("SELECT COUNT(*) FROM student_performance_scores WHERE school_id = 1 AND score_month = '2025-10-01'")
    ).scalar()
    assert oct_count == 4

    # Now calculate November snapshot (which includes the Nov quiz for student 101)
    generate_school_student_scores(db_session, 1, date(2025, 11, 1), force_recalculate=False, config=cfg)

    oct_101 = get_student_performance_score(db_session, 101, score_month=date(2025, 10, 1))
    nov_101 = get_student_performance_score(db_session, 101, score_month=date(2025, 11, 1))
    latest_101 = get_student_performance_score(db_session, 101)

    # October snapshot remains untouched at 90.0 (only Term 1 exam in Oct)
    assert oct_101["overall_score"] == pytest.approx(90.0, abs=0.01)
    assert oct_101["rank_in_class"] == 1
    # Student 102 tied with 101 at 90.0 -> rank 1; Student 103 at 70.0 -> rank 3 (1, 1, 3)
    oct_102 = get_student_performance_score(db_session, 102, score_month=date(2025, 10, 1))
    oct_103 = get_student_performance_score(db_session, 103, score_month=date(2025, 10, 1))
    assert oct_102["rank_in_class"] == 1
    assert oct_103["rank_in_class"] == 3

    # November snapshot includes regular internal 100% (weight 0.15/0.75=0.20) + main_term 90% (0.80) = 92.0
    assert nov_101["overall_score"] == pytest.approx(92.0, abs=0.01)
    # Default query without score_month returns latest snapshot (November)
    assert latest_101["score_month"] == date(2025, 11, 1)


def test_api_school_isolation_pagination_sorting_and_parent_access(db_session, api_client):
    """Tests 17, 18, 19: Cross-school isolation, class pagination & sorting, parent authorization."""
    _seed_multi_school_dataset(db_session)
    cfg = ScoringConfig()
    generate_school_student_scores(db_session, 1, date(2025, 10, 1), force_recalculate=True, config=cfg)
    generate_school_student_scores(db_session, 2, date(2025, 10, 1), force_recalculate=True, config=cfg)

    alpha_admin_token = _make_hs256_jwt({"user_id": 2, "role": "admin", "school_id": 1})
    beta_admin_token = _make_hs256_jwt({"user_id": 3, "role": "admin", "school_id": 2})
    guardian1_token = _make_hs256_jwt({"user_id": 4, "role": "guardian", "school_id": 1})
    teacher10a_token = _make_hs256_jwt({"user_id": 6, "role": "teacher", "school_id": 1})

    # Test 17: School isolation - Alpha Admin cannot view School 2
    resp_cross = api_client.get(
        "/api/v1/schools/2/student-performance",
        headers={"Authorization": f"Bearer {alpha_admin_token}"},
    )
    assert resp_cross.status_code == 403

    # Alpha Admin viewing School 1 never sees School 2's students
    resp_s1 = api_client.get(
        "/api/v1/schools/1/student-performance?score_month=2025-10-01",
        headers={"Authorization": f"Bearer {alpha_admin_token}"},
    )
    assert resp_s1.status_code == 200
    body_s1 = resp_s1.json()
    assert body_s1["total_records"] == 4
    assert all(item["student_id"] in {101, 102, 103, 104} for item in body_s1["items"])

    # Test 18: Class filter, pagination, and sorting (class 10 has 3 students: 101=90, 102=90, 103=70)
    resp_page1 = api_client.get(
        "/api/v1/schools/1/student-performance?score_month=2025-10-01&class_id=10&page=1&page_size=2&sort_by=overall_score&sort_order=asc",
        headers={"Authorization": f"Bearer {alpha_admin_token}"},
    )
    assert resp_page1.status_code == 200
    p1 = resp_page1.json()
    assert p1["total_records"] == 3
    assert p1["pagination"]["total_pages"] == 2
    assert p1["pagination"]["has_next"] is True
    assert [item["student_id"] for item in p1["items"]] == [103, 101]

    # Test 19: Guardian 1 (owns 101 & 104) can read 101, CANNOT read 102 or school-wide list
    resp_own = api_client.get(
        "/api/v1/students/101/performance-score",
        headers={"Authorization": f"Bearer {guardian1_token}"},
    )
    assert resp_own.status_code == 200
    assert resp_own.json()["data"]["student"]["id"] == 101

    resp_other = api_client.get(
        "/api/v1/students/102/performance-score",
        headers={"Authorization": f"Bearer {guardian1_token}"},
    )
    assert resp_other.status_code == 403

    resp_guardian_school = api_client.get(
        "/api/v1/schools/1/student-performance",
        headers={"Authorization": f"Bearer {guardian1_token}"},
    )
    assert resp_guardian_school.status_code == 403

    # Teacher 10A can access class 10 student (101) but NOT class 11 student (104)
    assert (
        api_client.get(
            "/api/v1/students/101/performance-score",
            headers={"Authorization": f"Bearer {teacher10a_token}"},
        ).status_code
        == 200
    )
    assert (
        api_client.get(
            "/api/v1/students/104/performance-score",
            headers={"Authorization": f"Bearer {teacher10a_token}"},
        ).status_code
        == 403
    )

    # Manual recalculation: Teacher & Guardian forbidden, Alpha Admin allowed for School 1 only
    assert (
        api_client.post(
            "/api/v1/schools/1/student-performance/recalculate",
            json={"score_month": "2025-10-01", "force_recalculate": True},
            headers={"Authorization": f"Bearer {guardian1_token}"},
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            "/api/v1/schools/1/student-performance/recalculate",
            json={"score_month": "2025-10-01", "force_recalculate": True},
            headers={"Authorization": f"Bearer {beta_admin_token}"},
        ).status_code
        == 403
    )
    resp_recalc = api_client.post(
        "/api/v1/schools/1/student-performance/recalculate",
        json={"score_month": "2025-10-01", "force_recalculate": True},
        headers={"Authorization": f"Bearer {alpha_admin_token}"},
    )
    assert resp_recalc.status_code == 200
    assert resp_recalc.json()["summary"]["successful_students"] == 4
