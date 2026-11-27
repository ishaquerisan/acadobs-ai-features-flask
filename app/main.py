
from contextlib import asynccontextmanager
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import academic_performance
from app.routers import student_performance
from app.routers import student_skills
from app.services.performance_scoring.scheduler import (
    shutdown_performance_scheduler,
    start_performance_scheduler,
)

load_dotenv()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_performance_scheduler()
    try:
        yield
    finally:
        shutdown_performance_scheduler()


def _parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or [default]


cors_origins = _parse_csv_env("CORS_ALLOW_ORIGINS", "*")
cors_methods = _parse_csv_env("CORS_ALLOW_METHODS", "*")
cors_headers = _parse_csv_env("CORS_ALLOW_HEADERS", "*")
cors_allow_credentials = (
    os.getenv("CORS_ALLOW_CREDENTIALS", "false").strip().lower() in ("1", "true", "yes")
    and "*" not in cors_origins
)

app = FastAPI(
    title="Acadobs AI Engine",
    description="Student Skill Extraction, Academic Analytics, and Monthly Performance Scoring",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=cors_methods,
    allow_headers=cors_headers,
)


app.include_router(student_skills.router)
app.include_router(academic_performance.router)
app.include_router(student_performance.router)


@app.get("/")
def home():

    return {
        "success": True,
        "message": "Acadobs AI Engine is running"
    }
