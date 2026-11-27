"""
Authentication and Role-Based Authorization dependencies compatible with Acadobs backend.

Acadobs issues HS256 JWT tokens signed with `JWT_SECRET` containing:
    {
        "user_id": int,
        "role": "superadmin" | "admin" | "teacher" | "staff" | "guardian" | "driver",
        "school_id": int,
        "name": str,
        "sessionId": int (optional; verified against `sessions` table when present)
    }

Relationship facts verified from Acadobs schema:
- `students.guardian_id` directly references `users.id` of the guardian user.
- `staffs` links `user_id` to `school_id` and homeroom `class_id`.
- `timetables` links `staff_id` (= `users.id`) to `class_id` within `school_id`.
- `staff_permissions` grants school-wide flags (`students`, `reports`, `aiAnalytics`, etc.) by `user_id`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Set

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.connection import get_db

try:
    import jwt as pyjwt  # type: ignore
except ImportError:  # pragma: no cover - fallback when PyJWT is not installed
    pyjwt = None

logger = logging.getLogger(__name__)


class JWTVerificationError(Exception):
    """Raised when a bearer token is malformed, invalid, or expired."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """Verified user context extracted from JWT and confirmed against database."""

    user_id: int
    role: str
    school_id: Optional[int]
    name: Optional[str] = None
    session_id: Optional[int] = None


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _decode_hs256_fallback(token: str, secret: str) -> dict[str, Any]:
    """Pure-stdlib HS256 JWT verifier matching PyJWT semantics."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTVerificationError("Malformed JWT token")

    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        provided_sig = _b64url_decode(signature_b64)
    except Exception as exc:
        raise JWTVerificationError("Invalid JWT encoding") from exc

    alg = header.get("alg")
    if alg != "HS256":
        raise JWTVerificationError(f"Unsupported JWT algorithm: {alg}")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise JWTVerificationError("Invalid JWT signature")

    exp = payload.get("exp")
    if exp is not None:
        try:
            if float(exp) < time.time():
                raise JWTVerificationError("Token expired")
        except (TypeError, ValueError) as exc:
            raise JWTVerificationError("Invalid exp claim") from exc

    nbf = payload.get("nbf")
    if nbf is not None:
        try:
            if float(nbf) > time.time():
                raise JWTVerificationError("Token not yet valid")
        except (TypeError, ValueError) as exc:
            raise JWTVerificationError("Invalid nbf claim") from exc

    if not isinstance(payload, dict):
        raise JWTVerificationError("Invalid JWT payload")
    return payload


def decode_jwt_token(token: str, secret: Optional[str] = None) -> dict[str, Any]:
    """Verify and decode a JWT token using JWT_SECRET."""
    secret_key = secret if secret is not None else os.getenv("JWT_SECRET", "")
    if not secret_key:
        logger.error("JWT_SECRET environment variable is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service is not configured",
        )

    if pyjwt is not None:
        try:
            return pyjwt.decode(token, secret_key, algorithms=["HS256"])
        except Exception as exc:
            raise JWTVerificationError(str(exc)) from exc

    return _decode_hs256_fallback(token, secret_key)


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing Authorization header",
        )
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid Bearer token format",
        )
    return parts[1].strip()


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    """
    Validate Bearer JWT, confirm active session (if sessionId is in token),
    and verify user exists and is active in `users`.
    """
    token = _extract_bearer_token(request)
    try:
        decoded = decode_jwt_token(token)
    except JWTVerificationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Token expired or invalid",
        )

    raw_user_id = decoded.get("user_id")
    if raw_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid token claims",
        )

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid user identifier",
        )

    session_id = decoded.get("sessionId")
    if session_id is not None:
        session_row = db.execute(
            text(
                """
                SELECT id, user_id
                FROM sessions
                WHERE id = :session_id
                LIMIT 1
                """
            ),
            {"session_id": session_id},
        ).mappings().first()

        if not session_row or int(session_row["user_id"]) != user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Session expired or logged in from another device",
            )

    user_row = db.execute(
        text(
            """
            SELECT id, school_id, role, name, status, trash
            FROM users
            WHERE id = :user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()

    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: User not found",
        )

    if (user_row["trash"] is not None and int(user_row["trash"]) != 0) or (
        str(user_row["status"] or "").lower() != "active"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: User account is inactive",
        )

    return AuthenticatedUser(
        user_id=int(user_row["id"]),
        role=str(user_row["role"]).lower(),
        school_id=int(user_row["school_id"]) if user_row["school_id"] is not None else None,
        name=user_row["name"],
        session_id=int(session_id) if session_id is not None else None,
    )


def _has_school_wide_student_permission(db: Session, user_id: int) -> bool:
    """Check `staff_permissions` for school-wide student/report/AI analytics permission."""
    row = db.execute(
        text(
            """
            SELECT students, reports, aiAnalytics
            FROM staff_permissions
            WHERE user_id = :user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()

    if not row:
        return False

    return bool(row["students"] or row["reports"] or row["aiAnalytics"])


def get_teacher_accessible_class_ids(db: Session, user_id: int, school_id: int) -> Set[int]:
    """
    Determine class IDs a teacher is authorized to access within their school:
    1. Homeroom class assigned in `staffs.class_id`.
    2. Classes taught via `timetables.staff_id = user_id`.
    """
    class_ids: Set[int] = set()

    staff_rows = db.execute(
        text(
            """
            SELECT class_id
            FROM staffs
            WHERE user_id = :user_id
              AND school_id = :school_id
              AND (trash IS NULL OR trash = 0)
              AND class_id IS NOT NULL
            """
        ),
        {"user_id": user_id, "school_id": school_id},
    ).mappings().all()
    for row in staff_rows:
        if row["class_id"] is not None:
            class_ids.add(int(row["class_id"]))

    timetable_rows = db.execute(
        text(
            """
            SELECT DISTINCT class_id
            FROM timetables
            WHERE staff_id = :user_id
              AND school_id = :school_id
              AND class_id IS NOT NULL
            """
        ),
        {"user_id": user_id, "school_id": school_id},
    ).mappings().all()
    for row in timetable_rows:
        if row["class_id"] is not None:
            class_ids.add(int(row["class_id"]))

    return class_ids


def authorize_school_performance_read(
    db: Session,
    user: AuthenticatedUser,
    school_id: int,
    requested_class_id: Optional[int] = None,
) -> Optional[Set[int]]:
    """
    Authorize school-level student performance listing.

    Returns:
        None if the user may access all classes in `school_id`, OR
        a non-empty set of permitted class_ids for class-scoped teachers.
    Raises:
        HTTPException(403) if unauthorized or crossing school boundaries.
    """
    if user.role == "superadmin":
        return None

    if user.school_id != school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Cross-school access is not permitted",
        )

    if user.role == "admin":
        return None

    if user.role == "teacher":
        if _has_school_wide_student_permission(db, user.user_id):
            return None
        allowed_classes = get_teacher_accessible_class_ids(db, user.user_id, school_id)
        if not allowed_classes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Teacher has no assigned classes or school-wide reporting permission",
            )
        if requested_class_id is not None and requested_class_id not in allowed_classes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You are not authorized to access this class",
            )
        return allowed_classes

    if user.role == "staff":
        if _has_school_wide_student_permission(db, user.user_id):
            return None
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Insufficient staff permissions",
        )

    # Guardians and drivers cannot view school-wide student lists
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: You are not authorized to view school-wide student scores",
    )


def authorize_student_performance_read(
    db: Session,
    user: AuthenticatedUser,
    student_id: int,
) -> dict[str, Any]:
    """
    Authorize access to an individual student's performance snapshot.

    Returns the student's core identity row (id, school_id, class_id, guardian_id).
    Never leaks another student's data when access is denied.
    """
    student = db.execute(
        text(
            """
            SELECT id, school_id, class_id, guardian_id
            FROM students
            WHERE id = :student_id
              AND (trash IS NULL OR trash = 0)
            LIMIT 1
            """
        ),
        {"student_id": student_id},
    ).mappings().first()

    if not student:
        # For guardians, return 403 if probing nonexistent/unowned IDs?
        # Standard REST returns 404 when student truly doesn't exist for admins/teachers,
        # and 403 for guardians if not linked to avoid ID enumeration.
        if user.role == "guardian":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You are not authorized to view this student's performance",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )

    student_school_id = int(student["school_id"])
    student_class_id = int(student["class_id"]) if student["class_id"] is not None else None
    student_guardian_id = int(student["guardian_id"]) if student["guardian_id"] is not None else None

    if user.role == "superadmin":
        return dict(student)

    if user.role == "guardian":
        if student_guardian_id != user.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Parents can only access their own linked children",
            )
        if user.school_id is not None and user.school_id != student_school_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Cross-school access is not permitted",
            )
        return dict(student)

    if user.school_id != student_school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Cross-school access is not permitted",
        )

    if user.role == "admin":
        return dict(student)

    if user.role == "teacher":
        if _has_school_wide_student_permission(db, user.user_id):
            return dict(student)
        allowed_classes = get_teacher_accessible_class_ids(db, user.user_id, student_school_id)
        if student_class_id is not None and student_class_id in allowed_classes:
            return dict(student)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Teacher is not assigned to this student's class",
        )

    if user.role == "staff":
        if _has_school_wide_student_permission(db, user.user_id):
            return dict(student)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Insufficient staff permissions",
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: Access denied",
    )


def authorize_school_recalculation(
    user: AuthenticatedUser,
    school_id: int,
) -> None:
    """Only superadmin (any school) or school admin (own school) may trigger recalculation."""
    if user.role == "superadmin":
        return

    if user.role == "admin" and user.school_id == school_id:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: Only authorized school administrators or super administrators may recalculate scores",
    )
