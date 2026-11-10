from fastapi import APIRouter

from app.analytics.academic_analysis import (
    calculate_average
)

router = APIRouter(
    prefix="/api/analytics",
    tags=["Analytics"]
)


@router.post("/calculate-average")
def calculate_student_average(data: dict):

    marks = data.get("marks", [])

    average = calculate_average(marks)

    return {
        "average": average,
        "status": "success"
    }