from datetime import datetime, timezone

from collections import defaultdict

from sqlalchemy import text

import re

SKILL_CONFIG = {

          # Main/public exam gets highest priority

          "main_exam_weight": 1.0,

          "other_exam_weight": 0.70,

          "normal_internal_weight": 0.40,

          "grade_map": {

                    "A+": 95, "A": 90, "A-": 85,

                    "B+": 80, "B": 75, "B-": 70,

                    "C+": 65, "C": 60, "C-": 55,

                    "D": 45, "E": 35

          },

          # Contribution of scored evidence types.
          # Weights are normalized across sources available for each skill.
          "source_weights": {
                    "academic_marks": 0.35,
                    "achievements": 0.50,
                    "co_scholastic": 0.15
          },

          # Achievement level importance
          "achievement_level_weights": {
                    "international": 1.00,
                    "national": 0.90,
                    "state": 0.80,
                    "district": 0.65,
                    "school": 0.50,
                    "class": 0.35
          },

          # Achievement position score (0-100)
          "achievement_position_scores": {
                    "1st prize": 100,
                    "2nd prize": 85,
                    "3rd prize": 70,
                    "participant": 40,
                    "other": 25
          }

}

# =========================================================

# HELPERS

# =========================================================

def safe_float(value):

          try:

                    number = float(value)

                    if number != number:

                              return None

                    return number

          except (ValueError, TypeError):

                    return None

def round_value(value):

          return round(value, 2) if value is not None else None

def average(values):

          valid = [safe_float(v) for v in values]

          valid = [v for v in valid if v is not None]

          return sum(valid) / len(valid) if valid else None

def performance_level(score):

          if score is None:

                    return "insufficient_evidence"

          if score >= 85:

                    return "excellent"

          if score >= 70:

                    return "strong"

          if score >= 50:

                    return "developing"

          return "needs_improvement"

def confidence(count):

          if count >= 8:

                    return "high"

          if count >= 4:

                    return "medium"

          if count >= 1:

                    return "low"

          return "insufficient_evidence"

def json_safe(value):

          if hasattr(value, "isoformat"):

                    return value.isoformat()

          return value

def parse_grade(value):

          if value is None:

                    return None

          value = str(value).strip().upper()

          if value in SKILL_CONFIG["grade_map"]:

                    return SKILL_CONFIG["grade_map"][value]

          return safe_float(value)

def assessment_type(exam_id, internal_name):

          """

          Main priority:

          exam_id exists AND name contains term or main.

          Other exam:

          exam_id exists but is not a main/term exam.

          Normal internal:

          exam_id is NULL.

          """

          if exam_id is None:

                    return "normal_internal"

          name = str(internal_name or "").strip().lower()

          if re.search(r"\b(term|main)\b", name):

                    return "main_public_exam"

          return "other_exam_linked"

def get_assessment_weight(category):

          return {

                    "main_public_exam": SKILL_CONFIG["main_exam_weight"],

                    "other_exam_linked": SKILL_CONFIG["other_exam_weight"],

                    "normal_internal": SKILL_CONFIG["normal_internal_weight"]

          }.get(category, 0.4)

def calculate_trend(records):

          """

          Compare first half with second half chronologically.

          Requires at least two valid observations.

          """

          valid = sorted(

                    [

                              r for r in records

                              if r.get("score") is not None

                              and r.get("date") is not None

                    ],

                    key=lambda r: str(r["date"])

          )

          if len(valid) < 2:

                    return "insufficient_history"

          values = [r["score"] for r in valid]

          middle = len(values) // 2

          if middle == 0:

                    return "insufficient_history"

          first = average(values[:middle])

          last = average(values[middle:])

          if last - first >= 5:

                    return "improving"

          if first - last >= 5:

                    return "declining"

          return "stable"

# =========================================================

# SUBJECT TO SKILL MAPPING

# =========================================================

def map_subject_to_skills(subject_name):

          name = str(subject_name or "").lower()

          mappings = {

                    "mathematics": [

                              "Mathematical Reasoning",

                              "Numerical Ability",

                              "Logical Reasoning",

                              "Problem Solving"

                    ],

                    "science": [

                              "Scientific Thinking",

                              "Analytical Thinking",

                              "Observation",

                              "Research Ability"

                    ],

                    "physics": [

                              "Scientific Thinking",

                              "Analytical Thinking",

                              "Logical Reasoning"

                    ],

                    "chemistry": [

                              "Scientific Thinking",

                              "Analytical Thinking",

                              "Observation"

                    ],

                    "biology": [

                              "Scientific Thinking",

                              "Observation",

                              "Research Ability"

                    ],

                    "english": [

                              "Language Proficiency",

                              "Communication",

                              "Reading Comprehension",

                              "Written Communication"

                    ],

                    "malayalam": [

                              "Language Proficiency",

                              "Communication",

                              "Written Communication"

                    ],

                    "hindi": [

                              "Language Proficiency",

                              "Communication"

                    ],

                    "computer": [

                              "Digital Literacy",

                              "Computational Thinking",

                              "Logical Reasoning",

                              "Problem Solving"

                    ],

                    "informatics": [

                              "Digital Literacy",

                              "Computational Thinking",

                              "Logical Reasoning"

                    ],

                    "social": [

                              "Social Awareness",

                              "Critical Thinking",

                              "Research Ability"

                    ],

                    "history": [

                              "Historical Understanding",

                              "Critical Thinking",

                              "Research Ability"

                    ],

                    "geography": [

                              "Spatial Reasoning",

                              "Environmental Awareness",

                              "Analytical Thinking"

                    ],

                    "art": [

                              "Creativity",

                              "Visual Expression"

                    ],

                    "drawing": [

                              "Creativity",

                              "Visual Expression"

                    ],

                    "music": [

                              "Musical Ability",

                              "Creative Expression"

                    ],

                    "physical education": [

                              "Physical Fitness",

                              "Coordination",

                              "Sports Skills"

                    ]

          }

          matched = []

          for keyword, skills in mappings.items():

                    if keyword in name:

                              matched.extend(skills)

          return list(dict.fromkeys(matched)) or ["Subject Knowledge"]

# =========================================================

# CO-SCHOLASTIC MAPPING

# =========================================================

def map_co_scholastic_to_skills(area_name):

          name = str(area_name or "").lower()

          if "health" in name or "physical" in name:

                    return ["Physical Fitness", "Health Awareness"]

          if "art" in name or "colour" in name or "color" in name:

                    return ["Creativity", "Artistic Expression"]

          if "music" in name:

                    return ["Musical Ability", "Creative Expression"]

          if "robot" in name:

                    return ["Technical Creativity", "Problem Solving"]

          if "co-curricular" in name:

                    return ["Participation", "Collaboration"]

          return ["Co-Scholastic Development"]

def map_achievement_to_skills(title, description, category):
          """Infer demonstrated skills from achievement details."""
          value = f"{title or ''} {description or ''} {category or ''}".lower()

          rules = [
                    (["coding", "programming", "software", "hackathon", "computer"],
                     ["Programming", "Problem Solving", "Computational Thinking"]),
                    (["science", "experiment", "scientific", "innovation"],
                     ["Scientific Thinking", "Research Ability", "Innovation"]),
                    (["mathematics", "maths", "numerical"],
                     ["Mathematical Reasoning", "Logical Reasoning", "Analytical Thinking"]),
                    (["quiz", "olympiad"],
                     ["Knowledge Application", "Analytical Thinking", "Problem Solving"]),
                    (["speech", "debate", "elocution", "oratory"],
                     ["Public Speaking", "Communication", "Critical Thinking"]),
                    (["writing", "essay", "literature", "story"],
                     ["Written Communication", "Creative Writing", "Language Proficiency"]),
                    (["sports", "football", "cricket", "athletics", "chess"],
                     ["Sports Skills", "Physical Fitness", "Teamwork", "Strategic Thinking"]),
                    (["art", "painting", "drawing", "design"],
                     ["Creativity", "Artistic Expression", "Visual Thinking"]),
                    (["music", "singing", "dance"],
                     ["Musical Ability", "Creative Expression", "Performance Skills"]),
                    (["leadership", "captain", "student leader"],
                     ["Leadership", "Teamwork", "Decision Making"]),
                    (["project", "research", "invention"],
                     ["Research Ability", "Innovation", "Problem Solving"]),
                    (["environment", "nature", "social service"],
                     ["Environmental Awareness", "Social Responsibility"])
          ]

          result = []
          for keywords, skills in rules:
                    if any(keyword in value for keyword in keywords):
                              result.extend(skills)

          return list(dict.fromkeys(result)) or ["General Achievement"]

# =========================================================

# SKILL EVIDENCE AGGREGATOR

# =========================================================

def add_evidence(skill_map, skill_name, category, score,

                                            source, evidence, weight=1.0):

          if skill_name not in skill_map:

                    skill_map[skill_name] = {

                              "skill": skill_name,

                              "category": category,

                              "observations": [],

                              "sources": set(),

                              "evidence": []

                    }

          item = skill_map[skill_name]

          item["sources"].add(source)

          item["evidence"].append(evidence)

          if score is not None:

                    item["observations"].append({

                              "score": float(score),

                              "weight": float(weight),

                              "source": source

                    })

def calculate_skill_score(skill):

          """

          First calculate a weighted average within each source.

          Then combine source averages using configured source weights.

          This prevents subjects with many duplicate mapped skills

          from dominating the final score.

          """

          source_groups = defaultdict(list)

          for item in skill["observations"]:

                    source_groups[item["source"]].append(item)

          numerator = 0

          denominator = 0

          for source, items in source_groups.items():

                    source_score = (

                              sum(x["score"] * x["weight"] for x in items)

                              / sum(x["weight"] for x in items)

                    )

                    source_weight = SKILL_CONFIG["source_weights"].get(

                              source, 0.10

                    )

                    numerator += source_score * source_weight

                    denominator += source_weight

          if denominator == 0:

                    return None

          return numerator / denominator

# =========================================================

# MAIN SERVICE FUNCTION

# =========================================================

def extract_skills_by_student_id(db, student_id):

          try:

                    student_id = int(student_id)

          except (ValueError, TypeError):

                    raise ValueError("Valid student_id is required")

          if student_id <= 0:

                    raise ValueError("Valid student_id is required")

          # -----------------------------------------------------

          # 1. STUDENT

          # -----------------------------------------------------

          student = db.execute(text("""

                    SELECT

                              id,

                              school_id,

                              class_id,

                              full_name,

                              date_of_birth,

                              gender

                    FROM students

                    WHERE id = :student_id

                         AND (trash IS NULL OR trash = 0)

                    LIMIT 1

          """), {

                    "student_id": student_id

          }).mappings().first()

          if not student:

                    raise LookupError("Student not found")

          school_id = student["school_id"]

          skill_map = {}

          # -----------------------------------------------------

          # 2. ACADEMIC MARKS

          # -----------------------------------------------------

          academic_rows = db.execute(text("""

                    SELECT

                              m.id AS mark_id,

                              m.student_id,

                              m.marks_obtained,

                              m.status AS mark_status,

                              im.id AS internal_id,

                              im.subject_id,

                              im.internal_name,

                              im.exam_id,

                              im.max_marks,

                              im.date AS assessment_date,

                              sub.subject_name

                    FROM marks m

                    INNER JOIN internal_marks im

                              ON im.id = m.internal_id

                    LEFT JOIN subjects sub

                              ON sub.id = im.subject_id

                    WHERE m.student_id = :student_id

                         AND im.school_id = :school_id

                         AND (im.trash IS NULL OR im.trash = 0)

                         AND m.status = 'present'

                    ORDER BY im.date ASC, m.id ASC

          """), {

                    "student_id": student_id,

                    "school_id": school_id

          }).mappings().all()

          subject_groups = defaultdict(list)

          academic_counts = defaultdict(int)

          academic_category_scores = defaultdict(list)

          academic_evidence = []

          academic_total_obtained = 0.0

          academic_total_maximum = 0.0

          academic_weighted_score_total = 0.0

          academic_total_weight = 0.0

          for row in academic_rows:

                    obtained = safe_float(row["marks_obtained"])

                    maximum = safe_float(row["max_marks"])

                    if obtained is None or maximum is None or maximum <= 0:

                              continue

                    # Do not allow an invalid result above maximum marks.

                    if obtained < 0 or obtained > maximum:

                              continue

                    percentage = (obtained / maximum) * 100

                    academic_total_obtained += obtained

                    academic_total_maximum += maximum

                    exam_type = assessment_type(

                              row["exam_id"],

                              row["internal_name"]

                    )

                    weight = get_assessment_weight(exam_type)

                    academic_weighted_score_total += percentage * weight

                    academic_total_weight += weight

                    subject_name = row["subject_name"] or "Unknown Subject"

                    evidence = {

                              "source": "academic_marks",

                              "mark_id": row["mark_id"],

                              "internal_id": row["internal_id"],

                              "subject_id": row["subject_id"],

                              "subject_name": subject_name,

                              "assessment_name": row["internal_name"],

                              "exam_id": row["exam_id"],

                              "assessment_type": exam_type,

                              "obtained_marks": obtained,

                              "maximum_marks": maximum,

                              "percentage": round_value(percentage),

                              "assessment_date": json_safe(row["assessment_date"])

                    }

                    academic_evidence.append(evidence)

                    academic_counts[exam_type] += 1

                    academic_category_scores[exam_type].append(percentage)

                    subject_groups[subject_name].append({

                              "score": percentage,

                              "obtained": obtained,

                              "maximum": maximum,

                              "date": row["assessment_date"],

                              "type": exam_type,

                              "weight": weight

                    })

                    for skill_name in map_subject_to_skills(subject_name):

                              add_evidence(

                                        skill_map,

                                        skill_name,

                                        "Cognitive",

                                        percentage,

                                        "academic_marks",

                                        evidence,

                                        weight

                              )

          # Subject-wise profiles

          subject_profiles = []

          for subject_name, records in subject_groups.items():

                    def weighted_average(category=None):

                              selected = [

                                        r for r in records

                                        if category is None or r["type"] == category

                              ]

                              if not selected:

                                        return None

                              total_weight = sum(r["weight"] for r in selected)

                              return (

                                        sum(r["score"] * r["weight"] for r in selected)

                                        / total_weight

                              )

                    subject_profiles.append({

                              "subject": subject_name,

                              "obtained_marks": round_value(sum(r["obtained"] for r in records)),

                              "maximum_marks": round_value(sum(r["maximum"] for r in records)),

                              "percentage": round_value(

                                  (sum(r["obtained"] for r in records) / sum(r["maximum"] for r in records)) * 100

                                  if sum(r["maximum"] for r in records) > 0 else None

                              ),

                              "average_score": round_value(weighted_average()),

                              "main_exam_average": round_value(

                                        weighted_average("main_public_exam")

                              ),

                              "other_exam_average": round_value(

                                        weighted_average("other_exam_linked")

                              ),

                              "normal_internal_average": round_value(

                                        weighted_average("normal_internal")

                              ),

                              "assessment_count": len(records),

                              "trend": calculate_trend([

                                        {"score": r["score"], "date": r["date"]}

                                        for r in records

                              ])

                    })

          # -----------------------------------------------------

          # 4. HOMEWORK AND ASSIGNMENTS

          # -----------------------------------------------------

          homework_rows = db.execute(text("""

                    SELECT

                              ha.id AS assignment_id,

                              ha.homework_id,

                              ha.points,

                              ha.remarks,

                              ha.solved_file,

                              ha.createdAt AS submitted_at,

                              ha.is_seen,

                              h.title,

                              h.description,

                              h.type,

                              h.subject_id,

                              sub.subject_name

                    FROM homework_assignments ha

                    INNER JOIN homeworks h

                              ON h.id = ha.homework_id

                    LEFT JOIN subjects sub

                              ON sub.id = h.subject_id

                    WHERE ha.student_id = :student_id

                         AND h.school_id = :school_id

                         AND (h.trash IS NULL OR h.trash = 0)

                    ORDER BY ha.createdAt ASC

          """), {

                    "student_id": student_id,

                    "school_id": school_id

          }).mappings().all()

          # Aggregate homework data only; no assignment-level evidence is returned.

          homework_skill_names = set()

          homework_total_points = 0.0

          homework_points_count = 0

          homework_file_count = 0

          homework_feedback_count = 0

          homework_subject_groups = defaultdict(lambda: {

              "total_assignments": 0,

              "assignments_with_file": 0,

              "with_teacher_feedback": 0,

              "total_points": 0.0,

              "points_count": 0,

              "skills": set()

          })

          for row in homework_rows:

                    subject_name = row["subject_name"] or "Other"

                    homework_group = homework_subject_groups[subject_name]

                    homework_group["total_assignments"] += 1

                    if row["solved_file"]:

                        homework_file_count += 1

                        homework_group["assignments_with_file"] += 1

                    if row["remarks"]:

                        homework_feedback_count += 1

                        homework_group["with_teacher_feedback"] += 1

                    points_value = safe_float(row["points"])

                    if points_value is not None:

                        homework_total_points += points_value

                        homework_points_count += 1

                        homework_group["total_points"] += points_value

                        homework_group["points_count"] += 1

                    task_text = " ".join([

                              str(row["title"] or ""),

                              str(row["description"] or "")

                    ]).lower()

                    derived = map_subject_to_skills(subject_name)

                    if any(word in task_text for word in [

                              "project", "research", "investigation"

                    ]):

                              derived.extend([

                                        "Research Ability",

                                        "Problem Solving",

                                        "Independent Learning"

                              ])

                    if any(word in task_text for word in [

                              "essay", "letter", "story", "writing"

                    ]):

                              derived.extend([

                                        "Written Communication",

                                        "Creative Expression"

                              ])

                    if any(word in task_text for word in [

                              "calculate", "solve", "equation", "problem"

                    ]):

                              derived.extend([

                                        "Problem Solving",

                                        "Logical Reasoning"

                              ])

                    derived = list(dict.fromkeys(derived))

                    # Keep only a compact internal marker for skill counts.

                    # Assignment titles, descriptions, IDs, remarks and timestamps

                    # are intentionally excluded from the returned JSON.

                    evidence = {"source": "homework"}

                    for skill_name in derived:

                              homework_skill_names.add(skill_name)

                              homework_group["skills"].add(skill_name)

                              # Homework-derived skills remain in the homework
                              # reporting profile and do not affect skill scores.

          homework_subject_profiles = []

          for subject_name, group in homework_subject_groups.items():

              homework_subject_profiles.append({

                  "subject": subject_name,

                  "total_assignments": group["total_assignments"],

                  "assignments_with_submission_file": group["assignments_with_file"],

                  "assignments_without_submission_file": (

                      group["total_assignments"] - group["assignments_with_file"]

                  ),

                  "with_teacher_feedback": group["with_teacher_feedback"],

                  "total_points": round_value(group["total_points"]),

                  "average_points": round_value(

                      group["total_points"] / group["points_count"]

                      if group["points_count"] else None

                  ),

                  "skills": sorted(group["skills"])

              })

          # -----------------------------------------------------

          # 5. CO-SCHOLASTIC ASSESSMENTS

          # -----------------------------------------------------

          co_rows = db.execute(text("""

                    SELECT

                              sca.id AS assessment_id,

                              sca.area_id,

                              ca.name AS area_name,

                              sca.grade,

                              sca.score,

                              sca.remarks,

                              sca.assessed_at

                    FROM student_co_scholastic_assessments sca

                    INNER JOIN co_scholastic_areas ca

                              ON ca.id = sca.area_id

                    WHERE sca.student_id = :student_id

                         AND sca.school_id = :school_id

                         AND ca.status = 1

                    ORDER BY sca.assessed_at ASC

          """), {

                    "student_id": student_id,

                    "school_id": school_id

          }).mappings().all()

          co_groups = defaultdict(list)

          for row in co_rows:

                    score = safe_float(row["score"])

                    if score is None:

                              score = parse_grade(row["grade"])

                    if score is None or not 0 <= score <= 100:

                              continue

                    area_name = row["area_name"] or "Unknown Area"

                    evidence = {

                              "source": "co_scholastic",

                              "assessment_id": row["assessment_id"],

                              "area_id": row["area_id"],

                              "area": area_name,

                              "grade": row["grade"],

                              "score": round_value(score),

                              "remarks": row["remarks"],

                              "assessed_at": json_safe(row["assessed_at"])

                    }

                    co_groups[row["area_id"]].append({

                              "score": score,

                              "date": row["assessed_at"],

                              "evidence": evidence

                    })

                    for skill_name in map_co_scholastic_to_skills(area_name):

                              add_evidence(

                                        skill_map,

                                        skill_name,

                                        "Co-Scholastic",

                                        score,

                                        "co_scholastic",

                                        evidence

                              )

          co_profiles = []

          for area_id, records in co_groups.items():

                    co_profiles.append({

                              "area_id": area_id,

                              "area": records[0]["evidence"]["area"],

                              "average_score": round_value(

                                        average([r["score"] for r in records])

                              ),

                              "assessment_count": len(records)

                    })

          # -----------------------------------------------------

          # 6. ATTENDANCE

          # -----------------------------------------------------

          attendance_rows = db.execute(text("""

                    SELECT

                              am.id,

                              am.status,

                              a.date,

                              a.period,

                              a.subject_id

                    FROM attendance_marked am

                    INNER JOIN attendance a

                              ON a.id = am.attendance_id

                    WHERE am.student_id = :student_id

                         AND a.school_id = :school_id

                         AND (a.trash IS NULL OR a.trash = 0)

                    ORDER BY a.date ASC, a.period ASC

          """), {

                    "student_id": student_id,

                    "school_id": school_id

          }).mappings().all()

          attendance = {

                    "present": 0,

                    "absent": 0,

                    "late": 0,

                    "leave": 0

          }

          for row in attendance_rows:

                    status = str(row["status"]).lower()

                    if status in attendance:

                              attendance[status] += 1

          recognized = sum(attendance.values())

          # Late and leave are separately visible.

          # This percentage counts present + late as attended sessions.

          attendance_percentage = (

                    (

                              attendance["present"] + attendance["late"]

                    ) / recognized * 100

                    if recognized else None

          )

          attendance_profile = {

                    "total_sessions": recognized,

                    **attendance,

                    "attendance_percentage": round_value(

                              attendance_percentage

                    )

          }

          # Attendance remains contextual evidence.

          # It is NOT added to professional skill scores.

          # -----------------------------------------------------

          # 7. ACHIEVEMENTS

          # -----------------------------------------------------

          achievement_rows = db.execute(text("""

                    SELECT

                              sa.id AS student_achievement_id,

                              sa.achievement_id,

                              sa.status AS position,

                              sa.remarks,

                              a.title,

                              a.description,

                              a.category,

                              a.level,

                              a.date,

                              a.awarding_body

                    FROM student_achievement sa

                    INNER JOIN achievements a

                              ON a.id = sa.achievement_id

                    WHERE sa.student_id = :student_id

                         AND (sa.trash IS NULL OR sa.trash = 0)

                         AND a.school_id = :school_id

                         AND (a.trash IS NULL OR a.trash = 0)

                    ORDER BY sa.createdAt ASC

          """), {

                    "student_id": student_id,

                    "school_id": school_id

          }).mappings().all()

          achievements = []  # Internal records used for summary counts.
          achievement_categories = defaultdict(int)
          achievement_levels = defaultdict(int)
          achievement_positions = defaultdict(int)

          for row in achievement_rows:
                    level = str(row["level"] or "").strip().lower()
                    position = str(row["position"] or "").strip().lower()

                    level_weight = SKILL_CONFIG["achievement_level_weights"].get(
                              level, 0.25
                    )
                    position_score = SKILL_CONFIG["achievement_position_scores"].get(
                              position, 25
                    )

                    # Final achievement score combines achievement level and position.
                    achievement_score = position_score * level_weight

                    achievements.append({
                              "achievement_id": row["achievement_id"],
                              "student_achievement_id": row["student_achievement_id"],
                              "title": row["title"],
                              "description": row["description"],
                              "category": row["category"],
                              "level": row["level"],
                              "position": row["position"],
                              "date": json_safe(row["date"]),
                              "awarding_body": row["awarding_body"],
                              "remarks": row["remarks"]
                    })

                    achievement_categories[str(row["category"] or "Uncategorized")] += 1
                    achievement_levels[str(row["level"] or "Unspecified")] += 1
                    achievement_positions[str(row["position"] or "Unspecified")] += 1

                    derived_skills = map_achievement_to_skills(
                              row["title"],
                              row["description"],
                              row["category"]
                    )

                    evidence = {
                              "source": "achievements",
                              "achievement_score": round_value(achievement_score),
                              "level": row["level"],
                              "position": row["position"],
                              "achievement_date": row["date"]
                    }

                    for skill_name in derived_skills:
                              add_evidence(
                                        skill_map,
                                        skill_name,
                                        "Demonstrated Skills",
                                        achievement_score,
                                        "achievements",
                                        evidence
                              )

          achievement_summary = {

              "total_achievements": len(achievements),

              "by_category": [

                  {"category": key, "count": value}

                  for key, value in sorted(achievement_categories.items())

              ],

              "by_level": [

                  {"level": key, "count": value}

                  for key, value in sorted(achievement_levels.items())

              ],

              "by_position": [

                  {"position": key, "count": value}

                  for key, value in sorted(achievement_positions.items())

              ]

          }

          # -----------------------------------------------------

          # 8. FINAL SKILL PROFILE

          # -----------------------------------------------------

          final_skills = []

          for skill_name, item in skill_map.items():

                    score = calculate_skill_score(item)

                    source_names = sorted(item["sources"])

                    observation_count = len(item["observations"])

                    # Confidence considers independent source diversity

                    # and actual scored observations.

                    scored_sources = {

                              x["source"] for x in item["observations"]

                    }

                    if observation_count >= 8 and len(scored_sources) >= 2:

                              skill_confidence = "high"

                    elif observation_count >= 4 or len(scored_sources) >= 2:

                              skill_confidence = "medium"

                    elif observation_count >= 1:

                              skill_confidence = "low"

                    else:

                              skill_confidence = "insufficient_evidence"

                    scored_history = []

                    for evidence in item["evidence"]:

                              date = (

                                        evidence.get("assessment_date")

                                        or evidence.get("assessed_at")

                                        or evidence.get("submitted_at")

                              )

                              value = (

                                        evidence.get("percentage")

                                        or evidence.get("normalized_score")

                                        or evidence.get("score")

                              )

                              if value is not None and date is not None:

                                        scored_history.append({

                                                  "score": float(value),

                                                  "date": date

                                        })

                    # Avoid treating multiple evidence types as a single

                    # chronological series with incomparable scoring scales.

                    academic_history = [

                              {

                                        "score": e["percentage"],

                                        "date": e["assessment_date"]

                              }

                              for e in item["evidence"]

                              if e["source"] == "academic_marks"

                    ]

                    achievement_history = [
                              {
                                        "score": e["achievement_score"],
                                        "date": e["achievement_date"]
                              }
                              for e in item["evidence"]
                              if e["source"] == "achievements"
                              and e.get("achievement_score") is not None
                              and e.get("achievement_date") is not None
                    ]

                    co_history = [

                              {

                                        "score": e["score"],

                                        "date": e["assessed_at"]

                              }

                              for e in item["evidence"]

                              if e["source"] == "co_scholastic"

                    ]

                    trend_values = (
                              achievement_history
                              if achievement_history
                              else academic_history
                              if academic_history
                              else co_history
                    )

                    trend = calculate_trend(trend_values)

                    if score is None:

                              status = "unscored"

                    elif score >= 70:

                              status = "strength"

                    elif score >= 50:

                              status = "developing"

                    else:

                              status = "needs_improvement"

                    final_skills.append({

                              "skill": skill_name,

                              "category": item["category"],

                              "score": round_value(score),

                              "level": performance_level(score),

                              "status": status,

                              "confidence": skill_confidence,

                              "trend": trend,

                              "score_observations": observation_count,

                              "data_sources": source_names

                    })

          final_skills.sort(

                    key=lambda x: (

                              x["score"] is not None,

                              x["score"] if x["score"] is not None else -1

                    ),

                    reverse=True

          )

          top_skills = final_skills[:10]

          # -----------------------------------------------------

          # 9. COMPACT RESPONSE - AGGREGATED DATA ONLY

          # No raw assessment evidence or homework evidence is returned.

          # -----------------------------------------------------

          academic_overall_percentage = (

              (academic_total_obtained / academic_total_maximum) * 100

              if academic_total_maximum > 0 else None

          )

          academic_weighted_average = (

              academic_weighted_score_total / academic_total_weight

              if academic_total_weight > 0 else None

          )

          co_scholastic_valid_scores = [

              record["score"]

              for records in co_groups.values()

              for record in records

          ]

          co_scholastic_average = average(co_scholastic_valid_scores)

          academic_exam_averages = {

              "main_public_exam": round_value(

                  average(academic_category_scores["main_public_exam"])

              ),

              "other_exam_linked": round_value(

                  average(academic_category_scores["other_exam_linked"])

              ),

              "normal_internal": round_value(

                  average(academic_category_scores["normal_internal"])

              )

          }

          homework_average_points = (

              homework_total_points / homework_points_count

              if homework_points_count else None

          )

          attendance_percentage = attendance_profile["attendance_percentage"]

          overall_skill_score = average([

              item["score"] for item in final_skills

              if item["score"] is not None

          ])

          return {

              "success": True,

              "data": {

                  "profile_version": "6.0",

                  "generated_at": datetime.now(timezone.utc).isoformat(),

                  "student": {

                      "student_id": student["id"],

                      "student_name": student["full_name"],

                      "school_id": student["school_id"],

                      "class_id": student["class_id"],

                      "gender":student["gender"],

                      "date_of_birth": json_safe(student["date_of_birth"])

                  },

                  "overall_summary": {

                      "overall_academic_percentage": round_value(academic_overall_percentage),

                      "weighted_academic_average": round_value(academic_weighted_average),

                      "overall_skill_score": round_value(overall_skill_score),

                      "co_scholastic_average": round_value(co_scholastic_average),

                      "homework_average_points": round_value(homework_average_points),

                      "attendance_percentage": round_value(attendance_percentage),

                      "total_skills_identified": len(final_skills),

                      "total_academic_assessments": len(academic_evidence),

                      "total_homework_assignments": len(homework_rows),

                      "total_co_scholastic_assessments": len(co_rows),

                      "total_attendance_sessions": attendance_profile["total_sessions"],

                      "total_achievements": len(achievements)

                  },

                  "skill_summary": {

                      "total_skills": len(final_skills),

                      "top_skills": top_skills,

                      "all_skills": final_skills

                  },

                  "academic_profile": {

                      "total_assessments": len(academic_evidence),

                      "total_obtained_marks": round_value(academic_total_obtained),

                      "total_maximum_marks": round_value(academic_total_maximum),

                      "overall_percentage": round_value(academic_overall_percentage),

                      "weighted_average_percentage": round_value(academic_weighted_average),

                      "main_exam_count": academic_counts["main_public_exam"],

                      "other_exam_count": academic_counts["other_exam_linked"],

                      "normal_internal_count": academic_counts["normal_internal"],

                      "exam_type_averages": academic_exam_averages,

                      "subjects": subject_profiles

                  },

                  "homework_profile": {

                      "total_assignments": len(homework_rows),

                      "assignments_with_submission_file": homework_file_count,

                      "assignments_without_submission_file": len(homework_rows) - homework_file_count,

                      "with_teacher_feedback": homework_feedback_count,

                      "total_points": round_value(homework_total_points),

                      "average_points": round_value(homework_average_points),

                      "derived_skills": sorted(homework_skill_names),

                      "subjects": homework_subject_profiles,

                      "points_note": "Points are shown as recorded. A percentage is not calculated because the maximum possible homework points are not available. File counts indicate uploaded files only; they do not determine whether offline work was submitted."

                  },

                  "co_scholastic_profile": {

                      "total_assessments": len(co_rows),

                      "valid_assessments": len(co_scholastic_valid_scores),

                      "average_score": round_value(co_scholastic_average),

                      "areas": co_profiles

                  },

                  "attendance_profile": attendance_profile,

                  "achievement_profile": achievement_summary,

                  "data_quality": {

                      "academic_records": len(academic_evidence),

                      "homework_records": len(homework_rows),

                      "attendance_records": len(attendance_rows),

                      "co_scholastic_records": len(co_rows),

                      "achievement_records": len(achievements),

                      "limitations": [

                          "Homework points have no known maximum score, so only total and average points are provided.",

                          "Attendance is a consistency indicator and is not included in professional skill scores.",

                          "Career interests and preferences are not available in these database tables.",

                          "Subject-to-skill mappings are inferred indicators, not direct skill examinations."

                      ]

                  },

                  "career_ai_input": {

                      "ready": True,

                      "purpose": "future_career_identification",

                      "instruction": (

                          "Use the aggregated academic, homework, co-scholastic, "

                          "attendance, achievement and skill summaries as evidence-based indicators. "

                          "Do not assume academic marks alone prove professional competence. "

                          "Consider age, interests, confidence and missing information before "

                          "suggesting exploratory career fields."

                      )

                  }

              }

          }
