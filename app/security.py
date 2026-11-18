import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, Header, HTTPException, status

from .cache import auth_status_cache, invalidate_user_caches
from .config import settings
from .database import get_db

ALGORITHM = "HS256"
PBKDF2_ITERATIONS = 120_000
_PRESENCE_TOUCH_INTERVAL_MS = 60_000
_MAX_PRESENCE_TRACKED = 50_000
_presence_lock = threading.Lock()
_LAST_PRESENCE_TOUCH: "OrderedDict[str, int]" = OrderedDict()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        parts = stored_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = parts[2]
        expected_hex = parts[3]
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return hmac.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


def create_access_token(uid: str, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": uid,
        "username": username,
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload.get("sub")
        return str(uid) if uid else None
    except Exception:
        return None


def invalidate_user_auth_cache(uid: str) -> None:
    invalidate_user_caches(uid)


def _should_touch_presence(uid: str, now_ms_val: int) -> bool:
    with _presence_lock:
        last_touch = _LAST_PRESENCE_TOUCH.get(uid, 0)
        if (now_ms_val - last_touch) < _PRESENCE_TOUCH_INTERVAL_MS:
            return False
        _LAST_PRESENCE_TOUCH[uid] = now_ms_val
        _LAST_PRESENCE_TOUCH.move_to_end(uid)
        while len(_LAST_PRESENCE_TOUCH) > _MAX_PRESENCE_TRACKED:
            _LAST_PRESENCE_TOUCH.popitem(last=False)
        return True


def _get_auth_status(uid: str) -> Optional[Dict[str, Any]]:
    cached = auth_status_cache.get(uid)
    if cached is not None:
        return cached
    with get_db() as db:
        row = db.execute(
            "SELECT uid, email, is_admin, is_banned FROM users WHERE uid = ?",
            (uid,),
        ).fetchone()
    if not row:
        return None
    email_val = (row["email"] or "").strip().lower()
    info = {
        "uid": row["uid"],
        "email": email_val,
        "is_admin": bool(row["is_admin"]) or (email_val in settings.ADMIN_EMAIL_SET),
        "is_banned": bool(row["is_banned"]),
    }
    auth_status_cache.set(uid, info, ttl_s=10.0)
    return info


def is_user_allowed_ws(uid: str) -> bool:
    info = _get_auth_status(uid)
    return bool(info and not info["is_banned"])


def get_current_uid(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.split(" ", 1)[1].strip()
    uid = decode_token(token)
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    info = _get_auth_status(uid)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not found",
        )
    if info["is_banned"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended by an administrator.",
        )

    now_ms_val = int(time.time() * 1000)
    if _should_touch_presence(uid, now_ms_val):
        try:
            with get_db(write=True) as db:
                db.execute(
                    """
                    INSERT INTO user_presence(uid, is_online, last_seen)
                    VALUES(?, 1, ?)
                    ON CONFLICT(uid) DO UPDATE SET last_seen=excluded.last_seen,
                        is_online=1
                    """,
                    (uid, now_ms_val),
                )
        except Exception:
            # Never fail a request if a non-critical presence heartbeat hits brief contention
            pass
    return uid


def require_admin_uid(authorization: Optional[str] = Header(default=None)) -> str:
    uid = get_current_uid(authorization)
    info = _get_auth_status(uid)
    if not info:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found")
    if info["email"] not in settings.ADMIN_EMAIL_SET and not info["is_admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner administrator access required",
        )
    return uid


def get_optional_uid(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return decode_token(token)

