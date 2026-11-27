"""SQLAlchemy ORM models for the Monthly Student Performance Scoring System."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarative class for new ORM models in Acadobs AI Engine."""


class StudentPerformanceScore(Base):
    """
    Permanent monthly cumulative performance snapshot per student.

    One row per (school_id, student_id, academic_year, score_month).
    Component scores store NULL when data is unavailable/ineligible rather than 0.
    """

    __tablename__ = "student_performance_scores"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "student_id",
            "academic_year",
            "score_month",
            name="uq_sps_school_student_year_month",
        ),
        Index("idx_sps_school_month", "school_id", "score_month"),
        Index("idx_sps_school_class_month", "school_id", "class_id", "score_month"),
        Index("idx_sps_student_month", "student_id", "score_month"),
        Index("idx_sps_school_overall", "school_id", "overall_score"),
        Index(
            "idx_sps_school_year_month_status",
            "school_id",
            "academic_year",
            "score_month",
            "calculation_status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    school_id: Mapped[int] = mapped_column(Integer, nullable=False)
    student_id: Mapped[int] = mapped_column(Integer, nullable=False)
    class_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    academic_year: Mapped[str] = mapped_column(String(255), nullable=False)
    score_month: Mapped[date] = mapped_column(Date, nullable=False)
    period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    period_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Academic (55% base weight)
    academic_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    academic_contribution: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    main_term_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    other_exam_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    regular_internal_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)

    # Attendance (10% base weight)
    attendance_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    attendance_contribution: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)

    # Homework (15% base weight)
    homework_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    homework_completion_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    homework_performance_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    homework_contribution: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)

    # Achievements (15% base weight)
    achievement_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    achievement_points: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    achievement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    achievement_contribution: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)

    # Co-scholastic (5% base weight)
    co_scholastic_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    co_scholastic_contribution: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)

    # Overall & transparency metadata
    overall_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    effective_weights: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    component_availability: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    rank_in_class: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    calculation_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="completed"
    )
    calculation_version: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PerformanceScoringJobRun(Base):
    """Audit and distributed-run tracking table for scheduled and manual scoring jobs."""

    __tablename__ = "performance_scoring_job_runs"
    __table_args__ = (
        Index("idx_psjr_job_key_status", "job_key", "status"),
        Index("idx_psjr_school_month", "school_id", "score_month"),
        Index("idx_psjr_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    job_key: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False, default="scheduled")
    score_month: Mapped[date] = mapped_column(Date, nullable=False)
    school_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    triggered_by_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    summary_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
