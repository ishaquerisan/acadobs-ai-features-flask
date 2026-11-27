-- ============================================================================
-- Migration 001: Monthly Student Performance Scoring Tables & Indexes
-- Target RDBMS: MySQL 8.0+ (InnoDB, utf8mb4)
-- Non-destructive: creates new tables only; does not modify existing data.
-- ============================================================================

CREATE TABLE IF NOT EXISTS student_performance_scores (
    id BIGINT NOT NULL AUTO_INCREMENT,
    school_id INT NOT NULL,
    student_id INT NOT NULL,
    class_id INT NULL,
    academic_year VARCHAR(255) NOT NULL,
    score_month DATE NOT NULL COMMENT 'First day of the month represented by the snapshot (e.g., 2025-10-01 for Oct 2025)',
    period_start DATE NULL COMMENT 'Academic year start date used for cumulative calculation',
    period_end DATE NULL COMMENT 'Last day of score_month used as cumulative cutoff date',

    -- Academic component (55% base weight)
    academic_score DECIMAL(6, 2) NULL,
    academic_contribution DECIMAL(6, 2) NULL,
    main_term_score DECIMAL(6, 2) NULL,
    other_exam_score DECIMAL(6, 2) NULL,
    regular_internal_score DECIMAL(6, 2) NULL,

    -- Attendance component (10% base weight)
    attendance_score DECIMAL(6, 2) NULL,
    attendance_contribution DECIMAL(6, 2) NULL,

    -- Homework component (15% base weight)
    homework_score DECIMAL(6, 2) NULL,
    homework_completion_score DECIMAL(6, 2) NULL,
    homework_performance_score DECIMAL(6, 2) NULL,
    homework_contribution DECIMAL(6, 2) NULL,

    -- Achievement component (15% base weight)
    achievement_score DECIMAL(6, 2) NULL,
    achievement_points DECIMAL(8, 2) NULL,
    achievement_count INT NOT NULL DEFAULT 0,
    achievement_contribution DECIMAL(6, 2) NULL,

    -- Co-scholastic component (5% base weight)
    co_scholastic_score DECIMAL(6, 2) NULL,
    co_scholastic_contribution DECIMAL(6, 2) NULL,

    -- Overall result & metadata
    overall_score DECIMAL(6, 2) NULL,
    effective_weights JSON NULL COMMENT 'Effective normalized component and academic category weights applied',
    component_availability JSON NULL COMMENT 'Per-component availability flags, counts, and diagnostic reasons',
    rank_in_class INT NULL COMMENT 'Competition rank (1, 2, 2, 4) within school, class, academic_year, and score_month',
    calculation_status VARCHAR(30) NOT NULL DEFAULT 'completed' COMMENT 'completed | insufficient_data | failed',
    calculation_version VARCHAR(30) NULL,
    calculated_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),

    -- Prevent duplicate snapshots for the same student and month
    UNIQUE KEY uq_sps_school_student_year_month (school_id, student_id, academic_year, score_month),

    -- Query performance indexes
    KEY idx_sps_school_month (school_id, score_month),
    KEY idx_sps_school_class_month (school_id, class_id, score_month),
    KEY idx_sps_student_month (student_id, score_month),
    KEY idx_sps_school_overall (school_id, overall_score),
    KEY idx_sps_school_year_month_status (school_id, academic_year, score_month, calculation_status),

    -- Foreign keys matching existing Acadobs InnoDB INT primary keys
    CONSTRAINT fk_sps_school
        FOREIGN KEY (school_id) REFERENCES schools (id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_sps_student
        FOREIGN KEY (student_id) REFERENCES students (id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_sps_class
        FOREIGN KEY (class_id) REFERENCES classes (id)
        ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- Job execution audit log table for monthly scheduler and manual recalculations
CREATE TABLE IF NOT EXISTS performance_scoring_job_runs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    job_key VARCHAR(120) NOT NULL COMMENT 'Logical lock/run identifier (e.g., monthly:2025-10-01 or school:1:2025-10-01)',
    trigger_type VARCHAR(30) NOT NULL DEFAULT 'scheduled' COMMENT 'scheduled | manual',
    score_month DATE NOT NULL,
    school_id INT NULL,
    triggered_by_user_id INT NULL,
    worker_id VARCHAR(120) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'running' COMMENT 'running | completed | partial_failure | failed | skipped_locked',
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    summary_json JSON NULL,
    error_message TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    KEY idx_psjr_job_key_status (job_key, status),
    KEY idx_psjr_school_month (school_id, score_month),
    KEY idx_psjr_started_at (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
