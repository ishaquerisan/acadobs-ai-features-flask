
from sqlalchemy import text
from statistics import pstdev
from collections import defaultdict


def safe_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def percentage(obtained, maximum):
    if maximum <= 0:
        return None
    return round((obtained / maximum) * 100, 2)


def get_performance_category(score):
    if score >= 90:
        return "Excellent"
    elif score >= 75:
        return "Good"
    elif score >= 50:
        return "Average"
    return "Needs Improvement"


def get_academic_performance(db, student_id):
    # --------------------------------------------------
    # 1. Validate and fetch student
    # --------------------------------------------------

    student_query = text("""
        SELECT
            id,
            school_id,
            class_id,
            full_name
        FROM students
        WHERE id = :student_id
          AND (trash IS NULL OR trash = 0)
        LIMIT 1
    """)

    student = db.execute(
        student_query,
        {"student_id": student_id}
    ).mappings().first()

    if not student:
        raise LookupError("Student not found")

    school_id = student["school_id"]

    # --------------------------------------------------
    # 2. Fetch academic records
    # --------------------------------------------------

    academic_query = text("""
        SELECT
            m.id AS mark_id,
            m.student_id,
            m.marks_obtained,
            m.status AS mark_status,

            im.id AS internal_id,
            im.internal_name,
            im.max_marks,
            im.date AS assessment_date,
            im.subject_id,
            im.exam_id,

            s.subject_name,
            e.exam_name

        FROM marks m

        INNER JOIN internal_marks im
            ON im.id = m.internal_id

        LEFT JOIN subjects s
            ON s.id = im.subject_id

        LEFT JOIN exams e
            ON e.id = im.exam_id

        WHERE m.student_id = :student_id
          AND im.school_id = :school_id
          AND (im.trash IS NULL OR im.trash = 0)
          AND (s.trash IS NULL OR s.trash = 0)

        ORDER BY im.date ASC, im.id ASC
    """)

    records = db.execute(
        academic_query,
        {
            "student_id": student_id,
            "school_id": school_id
        }
    ).mappings().all()

    # --------------------------------------------------
    # 3. Prepare valid assessment records
    # --------------------------------------------------

    valid_records = []

    for row in records:

        # Exclude absent assessments.
        if str(row["mark_status"] or "").lower() != "present":
            continue

        obtained = safe_float(row["marks_obtained"])
        maximum = safe_float(row["max_marks"])

        # A missing maximum is not assumed to be 100.
        if obtained is None or maximum is None:
            continue

        if maximum <= 0 or obtained < 0:
            continue

        score = percentage(obtained, maximum)

        valid_records.append({
            "mark_id": row["mark_id"],
            "subject_id": row["subject_id"],
            "subject_name": row["subject_name"] or "Unknown Subject",
            "exam_id": row["exam_id"],
            "exam_name": row["exam_name"] or "Unassigned Exam",
            "internal_name": row["internal_name"],
            "assessment_date": row["assessment_date"],
            "obtained_marks": obtained,
            "maximum_marks": maximum,
            "percentage": score
        })

    if not valid_records:
        return {
            "student": {
                "student_id": student["id"],
                "student_name": student["full_name"],
                "school_id": school_id,
                "class_id": student["class_id"]
            },
            "message": "No valid academic assessment records found.",
            "academic_summary": None,
            "subject_performance": [],
            "exam_progress": [],
            "improvement_analysis": None,
            "performance_consistency": None,
            "strengths": [],
            "improvement_areas": []
        }

    # --------------------------------------------------
    # 4. Overall academic performance
    # --------------------------------------------------

    total_obtained = sum(
        item["obtained_marks"] for item in valid_records
    )

    total_maximum = sum(
        item["maximum_marks"] for item in valid_records
    )

    overall_percentage = percentage(
        total_obtained,
        total_maximum
    )

    # --------------------------------------------------
    # 5. Subject-wise performance
    # --------------------------------------------------

    subject_map = {}

    for item in valid_records:

        subject_id = item["subject_id"]

        if subject_id not in subject_map:
            subject_map[subject_id] = {
                "subject_id": subject_id,
                "subject_name": item["subject_name"],
                "total_obtained": 0,
                "total_maximum": 0,
                "assessment_count": 0,
                "assessment_scores": []
            }

        subject = subject_map[subject_id]

        subject["total_obtained"] += item["obtained_marks"]
        subject["total_maximum"] += item["maximum_marks"]
        subject["assessment_count"] += 1
        subject["assessment_scores"].append(item["percentage"])

    subject_performance = []

    for subject in subject_map.values():

        score = percentage(
            subject["total_obtained"],
            subject["total_maximum"]
        )

        subject_performance.append({
            "subject_id": subject["subject_id"],
            "subject_name": subject["subject_name"],
            "obtained_marks": round(subject["total_obtained"], 2),
            "maximum_marks": round(subject["total_maximum"], 2),
            "percentage": score,
            "assessment_count": subject["assessment_count"],
            "performance": get_performance_category(score)
        })

    subject_performance.sort(
        key=lambda item: item["percentage"],
        reverse=True
    )

    # --------------------------------------------------
    # 6. Exam-wise progress
    # --------------------------------------------------

    exam_map = {}

    for item in valid_records:

        # Keep records without an exam ID separate by
        # their internal assessment name.
        exam_key = (
            ("exam", item["exam_id"])
            if item["exam_id"] is not None
            else ("internal", item["internal_name"])
        )

        if exam_key not in exam_map:
            exam_map[exam_key] = {
                "exam_id": item["exam_id"],
                "exam_name": item["exam_name"],
                "total_obtained": 0,
                "total_maximum": 0,
                "assessment_count": 0
            }

        exam = exam_map[exam_key]

        exam["total_obtained"] += item["obtained_marks"]
        exam["total_maximum"] += item["maximum_marks"]
        exam["assessment_count"] += 1

    exam_progress = []

    for exam in exam_map.values():

        score = percentage(
            exam["total_obtained"],
            exam["total_maximum"]
        )

        exam_progress.append({
            "exam_id": exam["exam_id"],
            "exam_name": exam["exam_name"],
            "obtained_marks": round(exam["total_obtained"], 2),
            "maximum_marks": round(exam["total_maximum"], 2),
            "percentage": score,
            "assessment_count": exam["assessment_count"]
        })

    # --------------------------------------------------
    # 7. Improvement analysis
    # --------------------------------------------------

    chronological_scores = [
        item["percentage"] for item in valid_records
    ]

    improvement_analysis = None

    if len(chronological_scores) >= 4:

        previous_scores = chronological_scores[:-3]
        recent_scores = chronological_scores[-3:]

        previous_average = sum(previous_scores) / len(previous_scores)
        recent_average = sum(recent_scores) / len(recent_scores)

        change = round(recent_average - previous_average, 2)

        if change > 1:
            trend = "Improving"
        elif change < -1:
            trend = "Declining"
        else:
            trend = "Stable"

        improvement_analysis = {
            "previous_average": round(previous_average, 2),
            "recent_average": round(recent_average, 2),
            "change_percentage_points": change,
            "trend": trend,
            "comparison_method": (
                "Earlier assessments compared with the latest 3 assessments"
            )
        }

    elif len(chronological_scores) >= 2:

        improvement_analysis = {
            "message": (
                "More assessment records are required for a reliable "
                "recent-versus-earlier comparison."
            )
        }

    # --------------------------------------------------
    # 8. Performance consistency
    # --------------------------------------------------

    consistency = None

    if len(chronological_scores) >= 2:

        standard_deviation = round(
            pstdev(chronological_scores),
            2
        )

        if standard_deviation <= 5:
            consistency_label = "Very Consistent"
        elif standard_deviation <= 10:
            consistency_label = "Consistent"
        elif standard_deviation <= 20:
            consistency_label = "Moderately Variable"
        else:
            consistency_label = "Highly Variable"

        consistency = {
            "standard_deviation": standard_deviation,
            "label": consistency_label,
            "assessment_count": len(chronological_scores)
        }

    # --------------------------------------------------
    # 9. Strengths and improvement areas
    # --------------------------------------------------

    strengths = [
        subject for subject in subject_performance
        if subject["percentage"] >= 75
    ]

    improvement_areas = [
        subject for subject in subject_performance
        if subject["percentage"] < 50
    ]

    # --------------------------------------------------
    # 10. Final response
    # --------------------------------------------------

    return {
        "student": {
            "student_id": student["id"],
            "student_name": student["full_name"],
            "school_id": school_id,
            "class_id": student["class_id"]
        },

        "academic_summary": {
            "overall_percentage": overall_percentage,
            "performance": get_performance_category(
                overall_percentage
            ),
            "total_obtained_marks": round(total_obtained, 2),
            "total_maximum_marks": round(total_maximum, 2),
            "total_assessments": len(valid_records),
            "total_subjects": len(subject_performance)
        },

        "subject_performance": subject_performance,

        "exam_progress": exam_progress,

        "improvement_analysis": improvement_analysis,

        "performance_consistency": consistency,

        "strengths": strengths,

        "improvement_areas": improvement_areas
    }

# Higher weight means greater influence on the subject score.
ASSESSMENT_WEIGHTS = {
    "term_exam": 3.0,
    "main_exam": 2.0,
    "local_exam": 1.0,
}


def to_float(value):
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_assessment(exam_id, internal_name):
    """
    Acadobs rules:
    1. exam_id exists + internal_name contains 'term'
       => Term exam
    2. exam_id exists
       => Other main exam
    3. exam_id is NULL
       => Local exam
    """

    if exam_id is None:
        return "local_exam"

    name = (internal_name or "").strip().lower()

    if "term" in name:
        return "term_exam"

    return "main_exam"


def classify_subject_strength(score):
    if score >= 75:
        return "Strong"

    if score < 50:
        return "Weak"

    return "Average"


def get_confidence(assessment_count):
    if assessment_count >= 6:
        return "High"

    if assessment_count >= 3:
        return "Medium"

    if assessment_count >= 1:
        return "Low"

    return "Insufficient Data"


def get_recommendations(score, homework_upload_rate):
    recommendations = []

    if score is None:
        recommendations.append(
            "There are not enough valid exam marks to assess this subject."
        )

    elif score < 50:
        recommendations.append(
            "Needs focused revision and teacher-guided support."
        )

    elif score < 75:
        recommendations.append(
            "Improve consistency through regular practice and revision."
        )

    else:
        recommendations.append(
            "Maintain the current performance and continue advanced practice."
        )

    if homework_upload_rate is not None and homework_upload_rate < 50:
        recommendations.append(
            "Few homework submissions have an uploaded file. "
            "Verify offline homework completion before drawing conclusions."
        )

    return recommendations


def get_subject_strength_weakness(db, student_id):

    # --------------------------------------------------
    # 1. Get student and school information
    # --------------------------------------------------

    student_query = text("""
        SELECT
            id,
            school_id,
            class_id,
            full_name
        FROM students
        WHERE id = :student_id
          AND trash = 0
        LIMIT 1
    """)

    student = db.execute(
        student_query,
        {"student_id": student_id}
    ).mappings().first()

    if not student:
        raise LookupError("Student not found")

    school_id = student["school_id"]

    # --------------------------------------------------
    # 2. Get exam marks subject-wise
    # --------------------------------------------------

    marks_query = text("""
        SELECT
            m.id AS mark_id,
            m.student_id,
            m.marks_obtained,
            m.status,

            im.id AS internal_id,
            im.subject_id,
            im.internal_name,
            im.max_marks,
            im.date AS assessment_date,
            im.exam_id,

            s.subject_name

        FROM marks m

        INNER JOIN internal_marks im
            ON im.id = m.internal_id

        INNER JOIN subjects s
            ON s.id = im.subject_id

        WHERE m.student_id = :student_id
          AND im.school_id = :school_id
          AND s.school_id = :school_id
          AND (im.trash IS NULL OR im.trash = 0)
          AND (s.trash IS NULL OR s.trash = 0)

        ORDER BY im.date ASC, im.id ASC
    """)

    mark_rows = db.execute(
        marks_query,
        {
            "student_id": student_id,
            "school_id": school_id
        }
    ).mappings().all()

    # --------------------------------------------------
    # 3. Get homework information subject-wise
    # --------------------------------------------------

    homework_query = text("""
        SELECT
            ha.id AS assignment_id,
            ha.homework_id,
            ha.student_id,
            ha.points,
            ha.remarks,
            ha.solved_file,
            ha.is_seen,

            h.subject_id,
            h.title AS homework_title,
            h.due_date,

            s.subject_name

        FROM homework_assignments ha

        INNER JOIN homeworks h
            ON h.id = ha.homework_id

        INNER JOIN subjects s
            ON s.id = h.subject_id

        WHERE ha.student_id = :student_id
          AND h.school_id = :school_id
          AND s.school_id = :school_id
          AND (h.trash IS NULL OR h.trash = 0)
          AND (s.trash IS NULL OR s.trash = 0)
          AND h.subject_id IS NOT NULL

        ORDER BY h.due_date ASC, ha.id ASC
    """)

    homework_rows = db.execute(
        homework_query,
        {
            "student_id": student_id,
            "school_id": school_id
        }
    ).mappings().all()

    # --------------------------------------------------
    # 4. Prepare subject-wise data
    # --------------------------------------------------

    subject_data = {}

    def get_subject(subject_id, subject_name):
        if subject_id not in subject_data:
            subject_data[subject_id] = {
                "subject_id": subject_id,
                "subject_name": subject_name,

                "assessments": [],
                "weighted_score_sum": 0.0,
                "total_assessment_weight": 0.0,

                "exam_categories": {
                    "term_exam": {
                        "count": 0,
                        "obtained": 0.0,
                        "maximum": 0.0
                    },
                    "main_exam": {
                        "count": 0,
                        "obtained": 0.0,
                        "maximum": 0.0
                    },
                    "local_exam": {
                        "count": 0,
                        "obtained": 0.0,
                        "maximum": 0.0
                    }
                },

                "absent_count": 0,

                "homework": {
                    "assigned_count": 0,
                    "uploaded_submission_count": 0,
                    "seen_count": 0,
                    "total_points": 0,
                    "feedback_count": 0
                }
            }

        return subject_data[subject_id]

    # --------------------------------------------------
    # 5. Analyse exam marks
    # --------------------------------------------------

    for row in mark_rows:

        subject = get_subject(
            row["subject_id"],
            row["subject_name"]
        )

        status = (row["status"] or "").lower()

        # Absent marks are counted separately.
        # They are not converted into zero marks.
        if status != "present":
            subject["absent_count"] += 1
            continue

        obtained = to_float(row["marks_obtained"])
        maximum = to_float(row["max_marks"])

        if obtained is None or maximum is None:
            continue

        if maximum <= 0 or obtained < 0:
            continue

        score = (obtained / maximum) * 100

        assessment_type = classify_assessment(
            row["exam_id"],
            row["internal_name"]
        )

        weight = ASSESSMENT_WEIGHTS[assessment_type]

        subject["assessments"].append({
            "internal_id": row["internal_id"],
            "internal_name": row["internal_name"],
            "exam_id": row["exam_id"],
            "assessment_type": assessment_type,
            "assessment_date": row["assessment_date"],
            "obtained_marks": round(obtained, 2),
            "maximum_marks": round(maximum, 2),
            "percentage": round(score, 2),
            "weight": weight
        })

        subject["weighted_score_sum"] += score * weight
        subject["total_assessment_weight"] += weight

        category = subject["exam_categories"][assessment_type]

        category["count"] += 1
        category["obtained"] += obtained
        category["maximum"] += maximum

    # --------------------------------------------------
    # 6. Analyse homework activity
    # --------------------------------------------------

    for row in homework_rows:

        subject = get_subject(
            row["subject_id"],
            row["subject_name"]
        )

        homework = subject["homework"]

        homework["assigned_count"] += 1

        if row["solved_file"]:
            homework["uploaded_submission_count"] += 1

        if row["is_seen"]:
            homework["seen_count"] += 1

        homework["total_points"] += (
            to_float(row["points"]) or 0
        )

        if row["remarks"] and row["remarks"].strip():
            homework["feedback_count"] += 1

    # --------------------------------------------------
    # 7. Calculate final subject-wise strength/weakness
    # --------------------------------------------------

    subject_results = []

    for subject in subject_data.values():

        assessment_count = len(subject["assessments"])

        if subject["total_assessment_weight"] > 0:

            weighted_score = (
                subject["weighted_score_sum"]
                / subject["total_assessment_weight"]
            )

            weighted_score = round(weighted_score, 2)

            strength_category = classify_subject_strength(
                weighted_score
            )

        else:
            weighted_score = None
            strength_category = "Insufficient Data"

        # Calculate exam category percentages.
        exam_breakdown = {}

        for category_name, category in subject["exam_categories"].items():

            if category["maximum"] > 0:
                category_percentage = round(
                    (
                        category["obtained"]
                        / category["maximum"]
                    ) * 100,
                    2
                )
            else:
                category_percentage = None

            exam_breakdown[category_name] = {
                "assessment_count": category["count"],
                "obtained_marks": round(category["obtained"], 2),
                "maximum_marks": round(category["maximum"], 2),
                "percentage": category_percentage
            }

        homework = subject["homework"]

        assigned_count = homework["assigned_count"]
        uploaded_count = homework["uploaded_submission_count"]

        # This is specifically an uploaded-file rate,
        # not a confirmed homework completion percentage.
        if assigned_count > 0:
            upload_rate = round(
                (uploaded_count / assigned_count) * 100,
                2
            )
        else:
            upload_rate = None

        homework_analysis = {
            "assigned_count": assigned_count,
            "uploaded_submission_count": uploaded_count,
            "uploaded_submission_rate": upload_rate,
            "seen_count": homework["seen_count"],
            "total_points_awarded": homework["total_points"],
            "feedback_count": homework["feedback_count"]
        }

        recommendations = get_recommendations(
            weighted_score,
            upload_rate
        )

        subject_results.append({
            "subject_id": subject["subject_id"],
            "subject_name": subject["subject_name"],

            "strength_weakness": strength_category,

            "academic_score": {
                "weighted_percentage": weighted_score,
                "assessment_count": assessment_count,
                "absent_count": subject["absent_count"],
                "confidence": get_confidence(assessment_count)
            },

            "exam_breakdown": exam_breakdown,

            "homework_analysis": homework_analysis,

            "recommendations": recommendations
        })

    # Strongest subjects first.
    subject_results.sort(
        key=lambda item: (
            item["academic_score"]["weighted_percentage"]
            if item["academic_score"]["weighted_percentage"] is not None
            else -1
        ),
        reverse=True
    )

    # --------------------------------------------------
    # 8. Separate strengths and weaknesses
    # --------------------------------------------------

    strengths = [
        item for item in subject_results
        if item["strength_weakness"] == "Strong"
    ]

    weaknesses = [
        item for item in subject_results
        if item["strength_weakness"] == "Weak"
    ]

    insufficient_data = [
        item for item in subject_results
        if item["strength_weakness"] == "Insufficient Data"
    ]

    return {
        "student": {
            "student_id": student["id"],
            "student_name": student["full_name"],
            "school_id": student["school_id"],
            "class_id": student["class_id"]
        },

        "analysis": {
            "module": "Subject-wise Strength and Weakness",
            "assessment_priority": {
                "term_exam": 3.0,
                "main_exam": 2.0,
                "local_exam": 1.0
            },
            "strength_threshold": 75,
            "weakness_threshold": 50,
            "homework_is_separate_supporting_evidence": True
        },

        "summary": {
            "total_subjects_analysed": len(subject_results),
            "strong_subjects": len(strengths),
            "weak_subjects": len(weaknesses),
            "insufficient_data_subjects": len(insufficient_data)
        },

        "strengths": strengths,
        "weaknesses": weaknesses,
        "all_subjects": subject_results
    }