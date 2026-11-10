from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.services.student_skill_service import (
    extract_skills_by_student_id
)

router = APIRouter(
    prefix="/api/students",
    tags=["Student AI Skills"]
)


@router.get("/{student_id}/skills")
def get_student_skills(
    student_id: int,
    db: Session = Depends(get_db)
):
    try:
        result = extract_skills_by_student_id(db, student_id,)

        return {
            "success": True,
            "data": result
        }

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except LookupError:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    except Exception:
        # Log the actual exception on the server.
        import logging
        logging.exception("Student skill extraction failed")

        raise HTTPException(
            status_code=500,
            detail="Failed to extract student skills"
        )