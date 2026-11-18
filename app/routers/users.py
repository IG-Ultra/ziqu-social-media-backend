from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..cache import (
    invalidate_feed_caches,
    invalidate_stories_cache,
    invalidate_user_caches,
    user_profile_cache,
    username_uid_cache,
)
from ..database import get_db
from ..realtime import manager
from ..schemas import (
    BatchUsersRequest,
    ChangeUsernameRequest,
    PresenceUpdateRequest,
    UpdateProfileRequest,
)
from ..security import get_current_uid
from ..utils import USERNAME_REGEX, get_request_base_url, now_ms, row_to_user

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.get("")
def list_users(request: Request, limit: int = Query(default=200, ge=1, le=500)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM users WHERE COALESCE(is_banned, 0) = 0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_user(r, request) for r in rows]


@router.get("/search")
def search_users(request: Request, q: str = Query(default="")):
    clean = q.strip().lstrip("@").lower()
    with get_db() as db:
        if not clean:
            rows = db.execute(
                "SELECT * FROM users WHERE COALESCE(is_banned, 0) = 0 ORDER BY followers_count DESC LIMIT 20"
            ).fetchall()
        else:
            like_pattern = f"%{clean}%"
            rows = db.execute(
                """
                SELECT * FROM users
                WHERE COALESCE(is_banned, 0) = 0
                  AND (username_lower LIKE ? OR LOWER(display_name) LIKE ?)
                ORDER BY
                    CASE WHEN username_lower LIKE ? THEN 0 ELSE 1 END,
                    followers_count DESC
                LIMIT 30
                """,
                (like_pattern, like_pattern, f"{clean}%"),
            ).fetchall()
    return [row_to_user(r, request) for r in rows]


@router.get("/by-username/{username}")
def get_by_username(username: str, request: Request):
    clean = username.strip().lstrip("@").lower()
    if not clean:
        return None
    base_url = get_request_base_url(request)
    cached_uid = username_uid_cache.get(clean)
    if cached_uid:
        cached_user = user_profile_cache.get((cached_uid, base_url))
        if cached_user is not None and not cached_user.get("isBanned", False):
            return cached_user

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM users WHERE username_lower = ? AND COALESCE(is_banned, 0) = 0",
            (clean,),
        ).fetchone()
    if not row:
        return None
    user_dict = row_to_user(row, request)
    username_uid_cache.set(clean, row["uid"], ttl_s=30.0)
    user_profile_cache.set((row["uid"], base_url), user_dict, ttl_s=20.0)
    return user_dict


@router.post("/batch")
def fetch_users_batch(payload: BatchUsersRequest, request: Request):
    uids = [u for u in payload.uids if u][:100]
    if not uids:
        return []
    base_url = get_request_base_url(request)
    found = {}
    missing = []
    for u in uids:
        cached = user_profile_cache.get((u, base_url))
        if cached is not None:
            if not cached.get("isBanned", False):
                found[u] = cached
        else:
            missing.append(u)

    if missing:
        placeholders = ",".join("?" for _ in missing)
        with get_db() as db:
            rows = db.execute(
                f"SELECT * FROM users WHERE uid IN ({placeholders}) AND COALESCE(is_banned, 0) = 0",
                tuple(missing),
            ).fetchall()
        for r in rows:
            u_dict = row_to_user(r, request)
            found[r["uid"]] = u_dict
            user_profile_cache.set((r["uid"], base_url), u_dict, ttl_s=20.0)

    return [found[u] for u in uids if u in found]


@router.put("/me")
def update_my_profile(
    payload: UpdateProfileRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    display_name = payload.displayName.strip()
    bio = payload.bio.strip()
    profile_image = payload.profileImage.strip() if payload.profileImage else None

    with get_db(write=True) as db:
        if profile_image:
            db.execute(
                "UPDATE users SET display_name = ?, bio = ?, profile_image = ? WHERE uid = ?",
                (display_name, bio, profile_image, uid),
            )
            db.execute("UPDATE posts SET user_profile_image = ? WHERE user_id = ?", (profile_image, uid))
            db.execute("UPDATE comments SET user_profile_image = ? WHERE user_id = ?", (profile_image, uid))
            db.execute("UPDATE stories SET user_profile_image = ? WHERE user_id = ?", (profile_image, uid))
        else:
            db.execute(
                "UPDATE users SET display_name = ?, bio = ? WHERE uid = ?",
                (display_name, bio, uid),
            )
        row = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()

    invalidate_user_caches(uid)
    invalidate_feed_caches()
    invalidate_stories_cache()
    return row_to_user(row, request)


@router.put("/me/username")
def change_my_username(
    payload: ChangeUsernameRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    new_username = payload.newUsername.strip().lstrip("@")
    if not USERNAME_REGEX.match(new_username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-30 characters and use only letters, numbers, dots, or underscores.",
        )
    new_lower = new_username.lower()

    with get_db(write=True) as db:
        conflict = db.execute(
            "SELECT uid FROM users WHERE username_lower = ? AND uid != ?",
            (new_lower, uid),
        ).fetchone()
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken. Please choose another username.",
            )

        db.execute(
            "UPDATE users SET username = ?, username_lower = ? WHERE uid = ?",
            (new_username, new_lower, uid),
        )
        db.execute("UPDATE posts SET username = ? WHERE user_id = ?", (new_username, uid))
        db.execute("UPDATE comments SET username = ? WHERE user_id = ?", (new_username, uid))
        db.execute("UPDATE stories SET username = ? WHERE user_id = ?", (new_username, uid))
        row = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()

    invalidate_user_caches(uid)
    invalidate_feed_caches()
    invalidate_stories_cache()
    return row_to_user(row, request)


@router.post("/me/presence")
async def update_my_presence(payload: PresenceUpdateRequest, uid: str = Depends(get_current_uid)):
    await manager.set_presence(uid, payload.isOnline)
    return {"isOnline": payload.isOnline, "lastSeen": now_ms()}


@router.get("/{uid}")
def get_user_profile(uid: str, request: Request):
    base_url = get_request_base_url(request)
    cache_key = (uid, base_url)
    cached = user_profile_cache.get(cache_key)
    if cached is not None:
        return cached
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user_dict = row_to_user(row, request)
    user_profile_cache.set(cache_key, user_dict, ttl_s=20.0)
    return user_dict


@router.get("/{uid}/presence")
def get_user_presence(uid: str):
    with get_db() as db:
        row = db.execute("SELECT is_online, last_seen FROM user_presence WHERE uid = ?", (uid,)).fetchone()
    if not row:
        return {"isOnline": False, "lastSeen": 0}
    return {"isOnline": bool(row["is_online"]), "lastSeen": int(row["last_seen"] or 0)}

