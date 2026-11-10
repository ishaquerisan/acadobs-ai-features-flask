
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.services.academic_performance_service import (
    get_academic_performance
)
from app.services.academic_performance_service import (
    get_subject_strength_weakness
)
router = APIRouter(
    prefix="/api/analytics",
    tags=["Academic Performance Intelligence"]
)

router = APIRouter(
    prefix="/api/analytics",
    tags=["Subject Strength and Weakness"]
)


@router.get("/students/{student_id}/academic-performance")
def academic_performance(
    student_id: int,
    db: Session = Depends(get_db)
):
    if student_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="Invalid student ID"
        )

    try:
        return get_academic_performance(db, student_id)

    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error)
        )

    except Exception as error:
        # Log the full exception on the server in production.
        print(f"Academic analytics error: {error}")

        raise HTTPException(
            status_code=500,
            detail="Unable to generate academic performance analytics"
        )

@router.get("/students/{student_id}/subject-strength-weakness")
def subject_strength_weakness(
    student_id: int,
    db: Session = Depends(get_db)
):
    if student_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="Invalid student ID"
        )

    try:
        return get_subject_strength_weakness(
            db,
            student_id
        )

    except LookupError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error)
        )

    except Exception as error:
        # Replace with your project's logger in production.
        print(f"Subject strength/weakness error: {error}")

        raise HTTPException(
            status_code=500,
            detail="Unable to analyse subject-wise performance"
        )

