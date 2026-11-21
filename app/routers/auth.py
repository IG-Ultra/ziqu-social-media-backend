import hashlib
import hmac
import logging
import secrets
import uuid
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from ..cache import rate_limiter
from ..config import settings
from ..database import get_db, is_unique_violation
from ..email_service import (
    is_email_configured,
    mask_email,
    render_reset_password_html,
    send_password_reset_email,
)
from ..schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordByTokenRequest,
    ResetPasswordRequest,
)
from ..security import (
    create_access_token,
    get_current_uid,
    hash_password,
    invalidate_user_auth_cache,
    verify_password,
)
from ..utils import USERNAME_REGEX, get_request_base_url, now_ms, row_to_user

logger = logging.getLogger("ziqu.auth")
router = APIRouter(prefix="/api/auth", tags=["Authentication"])
web_router = APIRouter(tags=["Password Reset Page"])

RESET_EXPIRY_MS = 15 * 60 * 1000
MAX_RESET_ATTEMPTS = 5


def _hash_reset_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _hash_reset_code(uid: str, code: str) -> str:
    return hashlib.sha256(f"{uid}:{code}".encode("utf-8")).hexdigest()


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

    is_owner_admin = 1 if email in settings.ADMIN_EMAIL_SET else 0
    with get_db(write=True) as db:
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

        try:
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
        except Exception as exc:
            if is_unique_violation(exc):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account with this email or username already exists.",
                )
            raise
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

    # Read user row lock-free before running CPU-heavy PBKDF2 password verification
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

    with get_db(write=True) as db:
        if (row["email"] or "").strip().lower() in settings.ADMIN_EMAIL_SET and not row["is_admin"]:
            db.execute("UPDATE users SET is_admin = 1 WHERE uid = ?", (row["uid"],))
            row = db.execute("SELECT * FROM users WHERE uid = ?", (row["uid"],)).fetchone()
        db.execute(
            """
            INSERT INTO user_presence(uid, is_online, last_seen)
            VALUES(?, 1, ?)
            ON CONFLICT(uid) DO UPDATE SET is_online=1, last_seen=excluded.last_seen
            """,
            (row["uid"], now_ms()),
        )

    token = create_access_token(row["uid"], row["username"])
    return {"token": token, "user": row_to_user(row, request)}


def _create_and_send_reset_sync(identifier: str, base_url: str) -> dict:
    if not is_email_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email reset service is not configured on the server yet. Please contact the app administrator.",
        )

    now = now_ms()
    with get_db(write=True) as db:
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
        if row["is_banned"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has been suspended by an administrator.",
            )

        allowed, retry_after = rate_limiter.allow(f"pwreset:{row['uid']}", limit=3, window_s=600.0)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many reset emails requested. Please check your inbox or wait {retry_after}s.",
            )

        raw_token = secrets.token_urlsafe(32)
        reset_code = f"{100000 + secrets.randbelow(900000)}"
        token_hash = _hash_reset_token(raw_token)
        code_hash = _hash_reset_code(row["uid"], reset_code)
        reset_id = str(uuid.uuid4())
        expires = now + RESET_EXPIRY_MS

        # Invalidate any prior unused reset requests for this account
        db.execute(
            "UPDATE password_resets SET used = 1 WHERE uid = ? AND used = 0",
            (row["uid"],),
        )
        db.execute(
            """
            INSERT INTO password_resets(
                id, uid, email, reset_code, token_hash, failed_attempts, created_at, expires_at, used
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0)
            """,
            (reset_id, row["uid"], row["email"], code_hash, token_hash, now, expires),
        )

    encoded_token = quote(raw_token, safe="")
    reset_link = (
        f"{base_url.rstrip('/')}/reset-password?token={encoded_token}"
        if base_url
        else f"/reset-password?token={encoded_token}"
    )
    deep_link = f"{settings.APP_RESET_DEEP_LINK_SCHEME}://reset-password?token={encoded_token}"

    sent, err_detail = send_password_reset_email(
        to_email=row["email"],
        username=row["username"],
        reset_link=reset_link,
        deep_link=deep_link,
        reset_code=reset_code,
    )
    if not sent:
        with get_db(write=True) as db:
            db.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (reset_id,))
        logger.error("Password reset email delivery failed for uid=%s: %s", row["uid"], err_detail)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send password reset email. Please try again shortly.",
        )

    masked = mask_email(row["email"])
    return {
        "success": True,
        "linkSent": True,
        "maskedEmail": masked,
        "message": f"Password reset link sent to {masked}. Check your inbox (and spam folder).",
        "devResetCode": None,
    }


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, request: Request):
    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter your registered email or username.",
        )
    base_url = get_request_base_url(request)
    return await run_in_threadpool(_create_and_send_reset_sync, identifier, base_url)


def _reset_password_by_code_sync(identifier: str, code: str, new_pw: str) -> str:
    now = now_ms()
    new_pw_hash = hash_password(new_pw)
    error_detail: Optional[str] = None
    updated_uid: Optional[str] = None

    with get_db(write=True) as db:
        if "@" in identifier:
            user = db.execute("SELECT * FROM users WHERE email = ?", (identifier.lower(),)).fetchone()
        else:
            user = db.execute(
                "SELECT * FROM users WHERE username_lower = ?",
                (identifier.lstrip("@").lower(),),
            ).fetchone()

        if not user:
            error_detail = "Account not found."
        else:
            reset_row = db.execute(
                """
                SELECT * FROM password_resets
                WHERE uid = ? AND used = 0 AND expires_at >= ?
                ORDER BY expires_at DESC LIMIT 1
                """,
                (user["uid"], now),
            ).fetchone()

            if not reset_row:
                error_detail = "Invalid or expired reset code. Please request a new reset link."
            else:
                attempts = int(reset_row["failed_attempts"] or 0)
                if attempts >= MAX_RESET_ATTEMPTS:
                    db.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (reset_row["id"],))
                    error_detail = "Too many incorrect attempts. Please request a new reset link."
                else:
                    expected_hash = _hash_reset_code(user["uid"], code)
                    if not hmac.compare_digest(str(reset_row["reset_code"] or ""), expected_hash):
                        next_attempts = attempts + 1
                        expire_now = 1 if next_attempts >= MAX_RESET_ATTEMPTS else 0
                        db.execute(
                            "UPDATE password_resets SET failed_attempts = ?, used = ? WHERE id = ?",
                            (next_attempts, expire_now, reset_row["id"]),
                        )
                        error_detail = (
                            "Too many incorrect attempts. Please request a new reset link."
                            if expire_now
                            else "Invalid verification code. Please check your email and try again."
                        )
                    else:
                        db.execute(
                            "UPDATE users SET password_hash = ? WHERE uid = ?",
                            (new_pw_hash, user["uid"]),
                        )
                        db.execute(
                            "UPDATE password_resets SET used = 1 WHERE uid = ? AND used = 0",
                            (user["uid"],),
                        )
                        updated_uid = user["uid"]

    if error_detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if error_detail == "Account not found." else status.HTTP_400_BAD_REQUEST,
            detail=error_detail,
        )
    if updated_uid:
        invalidate_user_auth_cache(updated_uid)
    return "Password updated successfully. You can now log in."


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest):
    identifier = payload.identifier.strip()
    code = payload.resetCode.strip()
    new_pw = payload.newPassword

    if not identifier:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email or username is required.")
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Please enter the 6-digit code from your email.")
    if len(new_pw) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters.",
        )

    msg = await run_in_threadpool(_reset_password_by_code_sync, identifier, code, new_pw)
    return {"success": True, "message": msg}


def _reset_password_by_token_sync(raw_token: str, new_pw: str) -> str:
    now = now_ms()
    token_hash = _hash_reset_token(raw_token)
    new_pw_hash = hash_password(new_pw)
    updated_uid: Optional[str] = None

    with get_db(write=True) as db:
        reset_row = db.execute(
            """
            SELECT * FROM password_resets
            WHERE token_hash = ? AND used = 0 AND expires_at >= ? AND COALESCE(failed_attempts, 0) < ?
            ORDER BY expires_at DESC LIMIT 1
            """,
            (token_hash, now, MAX_RESET_ATTEMPTS),
        ).fetchone()
        if not reset_row:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This reset link is invalid, expired, or has already been used.",
            )

        user = db.execute("SELECT uid FROM users WHERE uid = ?", (reset_row["uid"],)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")

        db.execute(
            "UPDATE users SET password_hash = ? WHERE uid = ?",
            (new_pw_hash, user["uid"]),
        )
        db.execute(
            "UPDATE password_resets SET used = 1 WHERE uid = ? AND used = 0",
            (user["uid"],),
        )
        updated_uid = user["uid"]

    if updated_uid:
        invalidate_user_auth_cache(updated_uid)
    return "Password updated successfully. You can now log in to ZIQU."


@router.post("/reset-password-by-token")
async def reset_password_by_token(payload: ResetPasswordByTokenRequest):
    token = payload.token.strip()
    new_pw = payload.newPassword
    if not token or len(token) < 16:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid password reset link.")
    if len(new_pw) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters.",
        )

    msg = await run_in_threadpool(_reset_password_by_token_sync, token, new_pw)
    return {"success": True, "message": msg}


@web_router.get("/reset-password", response_class=HTMLResponse)
def reset_password_web_page(token: str = Query(default="")):
    clean_token = token.strip()
    if not clean_token or len(clean_token) < 16:
        return HTMLResponse(
            content=render_reset_password_html(
                valid=False,
                error_message="Missing or invalid password reset link.",
            ),
            status_code=400,
        )

    now = now_ms()
    token_hash = _hash_reset_token(clean_token)
    with get_db() as db:
        reset_row = db.execute(
            """
            SELECT pr.uid, u.username
            FROM password_resets pr
            JOIN users u ON u.uid = pr.uid
            WHERE pr.token_hash = ?
              AND pr.used = 0
              AND pr.expires_at >= ?
              AND COALESCE(pr.failed_attempts, 0) < ?
            ORDER BY pr.expires_at DESC
            LIMIT 1
            """,
            (token_hash, now, MAX_RESET_ATTEMPTS),
        ).fetchone()

    if not reset_row:
        return HTMLResponse(
            content=render_reset_password_html(
                valid=False,
                error_message="This password reset link has expired or has already been used.",
            ),
            status_code=400,
        )

    deep_link = f"{settings.APP_RESET_DEEP_LINK_SCHEME}://reset-password?token={quote(clean_token, safe='')}"
    return HTMLResponse(
        content=render_reset_password_html(
            token=clean_token,
            username=reset_row["username"],
            valid=True,
            deep_link=deep_link,
        )
    )


@router.get("/me")
def get_me(request: Request, uid: str = Depends(get_current_uid)):
    now = now_ms()
    with get_db(write=True) as db:
        row = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        db.execute(
            """
            INSERT INTO user_presence(uid, is_online, last_seen)
            VALUES(?, 1, ?)
            ON CONFLICT(uid) DO UPDATE SET is_online=1, last_seen=excluded.last_seen
            """,
            (uid, now),
        )
    return row_to_user(row, request)


@router.post("/logout")
def logout(uid: str = Depends(get_current_uid)):
    now = now_ms()
    with get_db(write=True) as db:
        db.execute(
            """
            INSERT INTO user_presence(uid, is_online, last_seen)
            VALUES(?, 0, ?)
            ON CONFLICT(uid) DO UPDATE SET is_online=0, last_seen=excluded.last_seen
            """,
            (uid, now),
        )
    return {"success": True}

