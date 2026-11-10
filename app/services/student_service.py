from sqlalchemy import text


def get_student(db, student_id):

    query = text("""
        SELECT *
        FROM students
        WHERE id = :student_id
    """)

    result = db.execute(
        query,
        {"student_id": student_id}
    )

    return result.mappings().first()