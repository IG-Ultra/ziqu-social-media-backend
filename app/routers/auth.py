import logging
import random
import smtplib
import uuid
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..config import settings
from ..database import get_db
from ..schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
)
from ..security import create_access_token, get_current_uid, hash_password, verify_password
from ..utils import USERNAME_REGEX, now_ms, row_to_user

logger = logging.getLogger("ziqu.auth")
router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def _send_reset_email(to_email: str, username: str, reset_code: str) -> bool:
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = "ZIQU Password Reset Code"
        msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
        msg["To"] = to_email
        msg.set_content(
            f"Hi @{username},\n\nYour ZIQU password reset code is: {reset_code}\n"
            "This code expires in 15 minutes.\n\nIf you didn't request this, ignore this email."
        )
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as exc:
        logger.warning("Failed to send SMTP reset email: %s", exc)
        return False


@router.get("/check-username")
def check_username(username: str = Query(..., min_length=1)):
    clean = username.strip()
    if not USERNAME_REGEX.match(clean):
        return {
            "available": False,
            "valid": False,
            "message": "Username must be 3-30 characters and contain only letters, numbers, dots, or underscores.",
        }
    lower = clean.lower()
    with get_db() as db:
        row = db.execute(
            "SELECT uid FROM users WHERE username_lower = ?",
            (lower,),
        ).fetchone()
    if row:
        return {
            "available": False,
            "valid": True,
            "message": "Username already taken. Please choose another username.",
        }
    return {"available": True, "valid": True, "message": "Username is available"}


@router.post("/register")
def register(payload: RegisterRequest, request: Request):
    email = payload.email.strip().lower()
    username = payload.username.strip().lstrip("@")
    password = payload.password

    if "@" not in email or "." not in email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter a valid email address.",
        )
    if len(password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password is too weak. Please use at least 6 characters.",
        )
    if not USERNAME_REGEX.match(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-30 characters (letters, numbers, dots, underscores).",
        )

    username_lower = username.lower()
    uid = str(uuid.uuid4())
    created = now_ms()
    pw_hash = hash_password(password)
    display_name = payload.displayName.strip() or username

    with get_db() as db:
        existing_email = db.execute("SELECT uid FROM users WHERE email = ?", (email,)).fetchone()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists. Please log in instead.",
            )
        existing_user = db.execute(
            "SELECT uid FROM users WHERE username_lower = ?", (username_lower,)
        ).fetchone()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken. Please choose another username.",
            )

        is_owner_admin = 1 if email in settings.ADMIN_EMAIL_SET else 0
        db.execute(
            """
            INSERT INTO users(
                uid, username, username_lower, email, password_hash,
                display_name, bio, profile_image, created_at,
                followers_count, following_count, posts_count, is_verified, is_admin, is_banned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 0)
            """,
            (
                uid,
                username,
                username_lower,
                email,
                pw_hash,
                display_name,
                payload.bio.strip(),
                payload.profileImage.strip(),
                created,
                is_owner_admin,
                is_owner_admin,
            ),
        )
        user_row = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()

    token = create_access_token(uid, username)
    return {"token": token, "user": row_to_user(user_row, request)}


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    identifier = payload.identifier.strip()
    password = payload.password
    if not identifier or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter your email or username, and password.",
        )

    with get_db() as db:
        if "@" in identifier:
            row = db.execute(
                "SELECT * FROM users WHERE email = ?",
                (identifier.lower(),),
            ).fetchone()
        else:
            uname_lower = identifier.lstrip("@").strip().lower()
            row = db.execute(
                "SELECT * FROM users WHERE username_lower = ?",
                (uname_lower,),
            ).fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No account found for '{identifier}'. Please check your credentials or register.",
            )
        if not verify_password(password, row["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect password. Please try again or tap 'Forgot password?'",
            )
        if row["is_banned"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has been suspended by an administrator.",
            )
        if (row["email"] or "").strip().lower() in settings.ADMIN_EMAIL_SET and not row["is_admin"]:
            db.execute("UPDATE users SET is_admin = 1 WHERE uid = ?", (row["uid"],))
            row = db.execute("SELECT * FROM users WHERE uid = ?", (row["uid"],)).fetchone()

    token = create_access_token(row["uid"], row["username"])
    return {"token": token, "user": row_to_user(row, request)}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest):
    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter your email or username.",
        )

    with get_db() as db:
        if "@" in identifier:
            row = db.execute("SELECT * FROM users WHERE email = ?", (identifier.lower(),)).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM users WHERE username_lower = ?",
                (identifier.lstrip("@").lower(),),
            ).fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No account found with '{identifier}'.",
            )

        code = f"{random.randint(100000, 999999)}"
        expires = now_ms() + (15 * 60 * 1000)
        db.execute(
            """
            INSERT INTO password_resets(id, uid, email, reset_code, expires_at, used)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (str(uuid.uuid4()), row["uid"], row["email"], code, expires),
        )

    sent_email = _send_reset_email(row["email"], row["username"], code)
    logger.info("Password reset code for @%s (%s): %s", row["username"], row["email"], code)

    if sent_email:
        return {
            "success": True,
            "message": "Password reset code sent to your registered email.",
            "devResetCode": None,
        }
    return {
        "success": True,
        "message": f"Reset code generated (SMTP not configured on server). Use code: {code}",
        "devResetCode": code,
    }


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest):
    identifier = payload.identifier.strip()
    code = payload.resetCode.strip()
    new_pw = payload.newPassword

    if len(new_pw) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters.",
        )

    with get_db() as db:
        if "@" in identifier:
            user = db.execute("SELECT * FROM users WHERE email = ?", (identifier.lower(),)).fetchone()
        else:
            user = db.execute(
                "SELECT * FROM users WHERE username_lower = ?",
                (identifier.lstrip("@").lower(),),
            ).fetchone()

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

        reset_row = db.execute(
            """
            SELECT * FROM password_resets
            WHERE uid = ? AND reset_code = ? AND used = 0 AND expires_at >= ?
            ORDER BY expires_at DESC LIMIT 1
            """,
            (user["uid"], code, now_ms()),
        ).fetchone()

        if not reset_row:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset code.",
            )

        db.execute(
            "UPDATE users SET password_hash = ? WHERE uid = ?",
            (hash_password(new_pw), user["uid"]),
        )
        db.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (reset_row["id"],))

    return {"success": True, "message": "Password updated successfully. You can now log in."}


@router.get("/me")
def get_me(request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return row_to_user(row, request)


@router.post("/logout")
def logout(uid: str = Depends(get_current_uid)):
    now = now_ms()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO user_presence(uid, is_online, last_seen)
            VALUES(?, 0, ?)
            ON CONFLICT(uid) DO UPDATE SET is_online=0, last_seen=excluded.last_seen
            """,
            (uid, now),
        )
    return {"success": True}
