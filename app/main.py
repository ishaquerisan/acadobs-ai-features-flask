
from fastapi import FastAPI

from app.routers import student_skills
from app.routers import academic_performance
app = FastAPI(
    title="Acadobs AI Engine",
    description="Student Skill Extraction and Academic Analytics",
    version="1.0.0"
)


app.include_router(student_skills.router)
app.include_router(academic_performance.router)


@app.get("/")
def home():

    return {
        "success": True,
        "message": "Acadobs AI Engine is running"
    }
