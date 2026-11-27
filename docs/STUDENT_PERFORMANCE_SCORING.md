# Monthly Student Performance Scoring System — Documentation

## 1. Overview

This feature adds a production-ready **Monthly Student Performance Scoring System** to the existing `Acadobs AI Engine` FastAPI service, sharing its MySQL connection pool (`app/database/connection.py`) and aligning directly with the existing Acadobs MySQL schema and JWT authentication contract.

Key properties:
- **Permanent monthly cumulative snapshots** stored in `student_performance_scores` (`period_start` = academic-year start through `period_end` = last day of `score_month`).
- **Zero recalculation on normal `GET` requests** — parent, teacher, and administrator reads serve directly from saved snapshots.
- **Idempotent UPSERT generation** — running a month multiple times updates the same snapshot row via `UNIQUE (school_id, student_id, academic_year, score_month)` without duplicating records or overwriting previous months.
- **Fair missing-data handling** — unavailable/ineligible components store `NULL` (not `0`), and their weights are proportionally redistributed across available components so `effective_weights.components` always sums to `1.00` (100%).

---

## 2. Database Migration Steps

Apply the SQL migration against the existing Acadobs MySQL database. **No existing tables are altered.**

```bash
# Creates only the new tables: student_performance_scores and performance_scoring_job_runs
mysql -u "$DB_USER" -p "$DB_NAME" < migrations/001_create_student_performance_scores.sql
```

### Existing Schema Notes (Read-Only Usage)
1. **Homework (`homeworks`, `homework_assignments`)**: No columns are added or modified on these tables. The service reads `homework_assignments.points` and `homework_assignments.solved_file` directly (strictly ignoring `is_seen`) and normalizes teacher-awarded `points` against the configurable `SCORING_HOMEWORK_DEFAULT_MAX_POINTS` rubric (default `5.0`, matching the 1–5 point scale used in Acadobs `reportController.js`).
2. **Co-scholastic (`student_co_scholastic_assessments`)**: Numeric `score` (0–100) is used directly when present. Grade-only rows are **not** converted via invented mappings; configure a school-approved map in `SCORING_CO_SCHOLASTIC_GRADE_MAP` when needed.

---

## 3. Scoring Rules & Weightage

| Component | Base Weight | Internal Rules |
|---|---:|---|
| **Academic Marks** | **55%** | Main/Term `60%` (`exam_id IS NOT NULL` + configurable keyword regex), Other Exams `25%` (`exam_id IS NOT NULL`), Regular Internal `15%` (`exam_id IS NULL`). Normalized via `(marks_obtained / max_marks) * 100`, averaged per subject then per category. |
| **Attendance** | **10%** | `(present + late * late_credit) / (present + late + unexcused_absent) * 100`. `leave` rows and `absent` rows covered by approved student `leave_requests` (full or half-day) are excluded from the denominator. Missing rows are never treated as absences. |
| **Homework** | **15%** | Completion `60%` + Performance `40%`. Only non-trashed homeworks with `due_date` in `[period_start, period_end]`. `is_seen` is strictly ignored. |
| **Achievements** | **15%** | `SUM(level_base_points * prize_multiplier)`, capped at `100`. Base points: Class 10, School 20, District 35, State 50, National 70, International 100. Multipliers: 1st 1.00, 2nd 0.80, 3rd 0.60, participant 0.20, other 0.10. |
| **Co-scholastic** | **5%** | Optional; active `co_scholastic_areas` (`status = 1`), deduplicated per `(exam_id, area_id)`, averaged per area then across areas. Unavailable if not assessed (no penalty). |

### Ranking Rules
- `rank_in_class` is calculated per `(school_id, class_id, academic_year, score_month)`.
- Uses **standard competition ranking** (`1, 2, 2, 4`) ordered by `overall_score DESC`.
- Students with `calculation_status = 'insufficient_data'` or `class_id IS NULL` receive `rank_in_class = NULL`.

---

## 4. Authentication & Authorization

All `/api/v1/*` endpoints require a valid Acadobs Bearer JWT signed with `JWT_SECRET` (HS256):
```
Authorization: Bearer <jwt_token>
```
If `sessionId` is present in the token claims, it is validated against the `sessions` table.

| Role | `GET /schools/{school_id}/student-performance` | `GET /students/{student_id}/performance-score` | `POST /schools/{school_id}/student-performance/recalculate` |
|---|---|---|---|
| `superadmin` | All schools | All students | All schools |
| `admin` | Own school only | Own school students only | Own school only |
| `teacher` | Own school (assigned homeroom/timetable classes, or all classes if granted `staff_permissions` for `students`/`reports`/`aiAnalytics`) | Assigned class students (or school-wide with `staff_permissions`) | Forbidden (`403`) |
| `staff` | Own school only if granted `staff_permissions` (`students`/`reports`/`aiAnalytics`) | Own school only with `staff_permissions` | Forbidden (`403`) |
| `guardian` | Forbidden (`403`) | Only linked children (`students.guardian_id == user.id`) | Forbidden (`403`) |
| `driver` | Forbidden (`403`) | Forbidden (`403`) | Forbidden (`403`) |

---

## 5. REST API Reference & Sample Requests/Responses

### 5.1 School-Wide Paginated Scores

`GET /api/v1/schools/{school_id}/student-performance`

**Query Parameters:**
- `score_month` *(optional, `YYYY-MM` or `YYYY-MM-DD`)* — Defaults to latest available snapshot for the school.
- `academic_year` *(optional)* — e.g., `2025-2026`.
- `class_id` *(optional, int)* — Filter to a specific class.
- `page` *(default `1`, min `1`)*
- `page_size` *(default `20`, range `1..100`)*
- `search` *(optional)* — Matches student `full_name`, `reg_no`, or `roll_number`.
- `sort_by` *(default `overall_score`)* — One of `overall_score`, `academic_score`, `attendance_score`, `homework_score`, `achievement_score`.
- `sort_order` *(default `desc`)* — `asc` or `desc`.
- `calculation_status` *(optional)* — `completed`, `insufficient_data`, `failed`.

**Sample Request:**
```bash
curl -X GET \
  "http://localhost:8000/api/v1/schools/1/student-performance?score_month=2025-10-01&class_id=10&page=1&page_size=20&sort_by=overall_score&sort_order=desc" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Sample Response (`200 OK`):**
```json
{
  "success": true,
  "school_id": 1,
  "score_month": "2025-10-01",
  "academic_year": "2025-2026",
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_records": 32,
    "total_pages": 2,
    "has_next": true,
    "has_previous": false
  },
  "total_records": 32,
  "items": [
    {
      "student_id": 101,
      "student_name": "Aarav Nair",
      "reg_no": "REG101",
      "roll_number": 1,
      "class_details": {
        "id": 10,
        "classname": "10 A",
        "year": 10,
        "division": "A"
      },
      "academic_year": "2025-2026",
      "score_month": "2025-10-01",
      "overall_score": 88.45,
      "rank_in_class": 1,
      "component_scores": {
        "academic_score": 91.2,
        "attendance_score": 96.0,
        "homework_score": 84.0,
        "achievement_score": 75.0,
        "co_scholastic_score": null
      },
      "calculation_status": "completed",
      "calculation_version": "1.0.0",
      "calculated_at": "2025-11-01T00:10:04"
    }
  ]
}
```

---

### 5.2 Individual Student Detailed Breakdown

`GET /api/v1/students/{student_id}/performance-score`

**Query Parameters:**
- `score_month` *(optional)* — Defaults to latest available snapshot for the student.
- `academic_year` *(optional)*

**Sample Request:**
```bash
curl -X GET \
  "http://localhost:8000/api/v1/students/101/performance-score?score_month=2025-10-01" \
  -H "Authorization: Bearer $GUARDIAN_TOKEN"
```

**Sample Response (`200 OK`):**
```json
{
  "success": true,
  "data": {
    "student": {
      "id": 101,
      "full_name": "Aarav Nair",
      "reg_no": "REG101",
      "roll_number": 1
    },
    "school": {
      "id": 1,
      "name": "Alpha High"
    },
    "class_details": {
      "id": 10,
      "classname": "10 A",
      "year": 10,
      "division": "A"
    },
    "academic_year": "2025-2026",
    "score_month": "2025-10-01",
    "period_start": "2025-06-01",
    "period_end": "2025-10-31",
    "overall_score": 88.45,
    "rank_in_class": 1,
    "academic": {
      "academic_score": 91.2,
      "academic_contribution": 52.8,
      "main_term_score": 90.0,
      "other_exam_score": 92.0,
      "regular_internal_score": 94.67
    },
    "attendance": {
      "attendance_score": 96.0,
      "attendance_contribution": 10.11
    },
    "homework": {
      "homework_score": 84.0,
      "homework_completion_score": 90.0,
      "homework_performance_score": 75.0,
      "homework_contribution": 13.26
    },
    "achievement": {
      "achievement_score": 75.0,
      "achievement_points": 75.0,
      "achievement_count": 2,
      "achievement_contribution": 11.84
    },
    "co_scholastic": {
      "co_scholastic_score": null,
      "co_scholastic_contribution": null
    },
    "effective_weights": {
      "components": {
        "academic": 0.578947,
        "attendance": 0.105263,
        "homework": 0.157895,
        "achievement": 0.157895
      },
      "academic_categories": {
        "main_term": 0.6,
        "other_exam": 0.25,
        "regular_internal": 0.15
      }
    },
    "component_availability": {
      "academic": { "available": true, "valid_assessment_count": 8 },
      "attendance": { "available": true, "eligible_sessions": 50 },
      "homework": { "available": true, "total_assigned_due": 10, "completed_count": 9 },
      "achievement": { "available": true, "achievement_count": 2 },
      "co_scholastic": { "available": false, "reason": "no_co_scholastic_assessments" }
    },
    "calculation_status": "completed",
    "calculation_version": "1.0.0",
    "calculated_at": "2025-11-01T00:10:04"
  }
}
```

---

### 5.3 Manual School Recalculation

`POST /api/v1/schools/{school_id}/student-performance/recalculate`

**Request Body:**
```json
{
  "score_month": "2025-10-01",
  "force_recalculate": true
}
```

**Sample Request:**
```bash
curl -X POST \
  "http://localhost:8000/api/v1/schools/1/student-performance/recalculate" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"score_month": "2025-10-01", "force_recalculate": true}'
```

**Sample Response (`200 OK`):**
```json
{
  "success": true,
  "message": "Monthly student performance scores generated successfully",
  "summary": {
    "school_id": 1,
    "academic_year": "2025-2026",
    "score_month": "2025-10-01",
    "period_start": "2025-06-01",
    "period_end": "2025-10-31",
    "total_students": 420,
    "processed_students": 420,
    "successful_students": 412,
    "insufficient_data_students": 8,
    "skipped_students": 0,
    "failed_students": 0,
    "execution_time": 1.3842,
    "calculation_version": "1.0.0",
    "errors": []
  }
}
```

---

## 6. Configuring and Running the Automatic Monthly Scheduler

### Default Schedule
- Trigger: **Day 1 of every month at `00:10`** in `PERFORMANCE_SCHEDULER_TIMEZONE` (default `Asia/Kolkata`).
- Snapshot month calculated: **previous calendar month** (e.g., on `2025-11-01 00:10 IST`, calculates `score_month = 2025-10-01` covering `education_year_start` through `2025-10-31`).

### Environment Variables
See `.env.example` for all configurable options:
```env
JWT_SECRET=<same_secret_as_acadobs_backend>
PERFORMANCE_SCHEDULER_ENABLED=true
PERFORMANCE_SCHEDULER_TIMEZONE=Asia/Kolkata
PERFORMANCE_SCHEDULER_DAY=1
PERFORMANCE_SCHEDULER_HOUR=0
PERFORMANCE_SCHEDULER_MINUTE=10
PERFORMANCE_SCHEDULER_LOCK_TIMEOUT=5
PERFORMANCE_SCHEDULER_MAX_RETRIES=2
PERFORMANCE_SCHEDULER_RETRY_DELAY_SECONDS=30
```

### Multi-Worker / Multi-Replica Safety
- The scheduler starts automatically via FastAPI `lifespan` in `app/main.py`.
- When running with multiple workers (`uvicorn app.main:app --workers 4`) or multiple Kubernetes pods, **only one worker executes the job** because `distributed_job_lock` acquires a MySQL session-scoped advisory lock (`GET_LOCK('acadobs:monthly_perf:YYYY-MM-01', timeout)`).
- Every other worker sees the lock held and logs `skipped_locked`. If the active worker crashes, MySQL automatically releases the advisory lock when the connection drops.
- All runs are logged to `performance_scoring_job_runs` with `status`, `worker_id`, `summary_json`, and `error_message`.
- If you prefer running the scheduler as a dedicated single-instance sidecar/cron instead of inside web workers, set `PERFORMANCE_SCHEDULER_ENABLED=false` on web pods and invoke:
  ```bash
  python -c "from app.services.performance_scoring.scheduler import run_monthly_scoring_job; print(run_monthly_scoring_job())"
  ```

---

## 7. Running Tests

```bash
pip install -r requirements.txt pytest httpx
pytest -v tests/test_student_performance_scoring.py
```
