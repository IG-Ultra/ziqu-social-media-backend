import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from ..cache import hot_stories_cache, invalidate_stories_cache
from ..database import get_db
from ..realtime import manager
from ..schemas import CreateStoryRequest, StoryReactionRequest
from ..security import get_current_uid, get_optional_uid
from ..utils import (
    get_blocked_peer_set_cached,
    get_request_base_url,
    now_ms,
    resolve_url,
    row_to_story,
)

router = APIRouter(prefix="/api/stories", tags=["Stories"])

TWENTY_FOUR_HOURS_MS = 24 * 60 * 60 * 1000


@router.get("")
def list_active_stories(
    request: Request,
    uid: Optional[str] = Depends(get_optional_uid),
):
    base_url = get_request_base_url(request)
    cache_key = ("stories", base_url)
    cached_stories: Optional[List[Dict[str, Any]]] = hot_stories_cache.get(cache_key)

    if cached_stories is None:
        now = now_ms()
        with get_db() as db:
            rows = db.execute(
                """
                SELECT s.* FROM stories s
                JOIN users u ON u.uid = s.user_id
                WHERE s.expires_at > ? AND COALESCE(u.is_banned, 0) = 0
                ORDER BY s.timestamp ASC
                """,
                (now,),
            ).fetchall()
            if not rows:
                hot_stories_cache.set(cache_key, [], ttl_s=4.0)
                return []

            story_ids = [r["story_id"] for r in rows]
            viewers_map: Dict[str, List[str]] = {sid: [] for sid in story_ids}
            reactions_map: Dict[str, Dict[str, str]] = {sid: {} for sid in story_ids}
            reaction_usernames_map: Dict[str, Dict[str, str]] = {sid: {} for sid in story_ids}
            reaction_profiles_map: Dict[str, Dict[str, str]] = {sid: {} for sid in story_ids}

            for i in range(0, len(story_ids), 500):
                chunk = story_ids[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)

                for vr in db.execute(
                    f"SELECT story_id, viewer_uid FROM story_viewers WHERE story_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall():
                    viewers_map.setdefault(vr["story_id"], []).append(vr["viewer_uid"])

                for rr in db.execute(
                    f"""
                    SELECT story_id, reactor_uid, reactor_username, reactor_profile_image, emoji
                    FROM story_reactions
                    WHERE story_id IN ({placeholders})
                    """,
                    tuple(chunk),
                ).fetchall():
                    sid = rr["story_id"]
                    ruid = rr["reactor_uid"]
                    reactions_map.setdefault(sid, {})[ruid] = rr["emoji"]
                    reaction_usernames_map.setdefault(sid, {})[ruid] = rr["reactor_username"]
                    reaction_profiles_map.setdefault(sid, {})[ruid] = resolve_url(
                        rr["reactor_profile_image"] or "", request
                    )

            cached_stories = []
            for row in rows:
                sid = row["story_id"]
                is_ver = bool(row["is_verified"])
                cached_stories.append(
                    {
                        "storyId": sid,
                        "userId": row["user_id"],
                        "username": row["username"],
                        "userProfileImage": resolve_url(row["user_profile_image"] or "", request),
                        "mediaUrl": resolve_url(row["media_url"] or "", request),
                        "mediaType": row["media_type"] or "image",
                        "timestamp": int(row["timestamp"]),
                        "expiresAt": int(row["expires_at"]),
                        "viewers": viewers_map.get(sid, []),
                        "isVerified": is_ver,
                        "verifiedAccount": is_ver,
                        "originalPostId": row["original_post_id"] or "",
                        "originalAuthorUsername": row["original_author_username"] or "",
                        "caption": row["caption"] or "",
                        "sticker": row["sticker"] or "",
                        "textX": float(row["text_x"] if row["text_x"] is not None else 0.5),
                        "textY": float(row["text_y"] if row["text_y"] is not None else 0.5),
                        "textColor": row["text_color"] or "#FFFFFF",
                        "textScale": float(row["text_scale"] if row["text_scale"] is not None else 1.0),
                        "textRotation": float(row["text_rotation"] if row["text_rotation"] is not None else 0.0),
                        "reactions": reactions_map.get(sid, {}),
                        "reactionUsernames": reaction_usernames_map.get(sid, {}),
                        "reactionProfiles": reaction_profiles_map.get(sid, {}),
                    }
                )
        hot_stories_cache.set(cache_key, cached_stories, ttl_s=4.0)

    if not uid or not cached_stories:
        return cached_stories
    blocked = get_blocked_peer_set_cached(uid)
    if not blocked:
        return cached_stories
    return [s for s in cached_stories if s["userId"] not in blocked]


def _create_story_sync(story_id: str, uid: str, payload: CreateStoryRequest, ts: int, expires: int, base_url: str):
    with get_db(write=True) as db:
        user = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        db.execute(
            """
            INSERT INTO stories(
                story_id, user_id, username, user_profile_image,
                media_url, media_type, timestamp, expires_at, is_verified,
                original_post_id, original_author_username,
                caption, sticker, text_x, text_y, text_color, text_scale, text_rotation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                uid,
                user["username"],
                user["profile_image"] or "",
                payload.mediaUrl.strip(),
                payload.mediaType.strip() or "image",
                ts,
                expires,
                int(user["is_verified"] or 0),
                payload.originalPostId.strip(),
                payload.originalAuthorUsername.strip(),
                payload.caption,
                payload.sticker,
                payload.textX,
                payload.textY,
                payload.textColor,
                payload.textScale,
                payload.textRotation,
            ),
        )
        row = db.execute("SELECT * FROM stories WHERE story_id = ?", (story_id,)).fetchone()
        story_dict = row_to_story(db, row)
        if base_url:
            for k in ("userProfileImage", "mediaUrl"):
                val = story_dict.get(k) or ""
                if val.startswith("/"):
                    story_dict[k] = f"{base_url}{val}"
    invalidate_stories_cache()
    return story_dict


@router.post("")
async def create_story(
    payload: CreateStoryRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    story_id = str(uuid.uuid4())
    ts = now_ms()
    expires = ts + TWENTY_FOUR_HOURS_MS
    base_url = get_request_base_url(request)

    story_dict = await run_in_threadpool(_create_story_sync, story_id, uid, payload, ts, expires, base_url)
    manager.schedule_broadcast("story_updated", {"event": "story_updated", "storyId": story_id}, delay_s=0.2)
    return story_dict


def _mark_story_viewed_sync(story_id: str, uid: str, ts: int) -> Optional[str]:
    with get_db(write=True) as db:
        cur = db.execute(
            """
            INSERT INTO story_viewers(story_id, viewer_uid, timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(story_id, viewer_uid) DO NOTHING
            """,
            (story_id, uid, ts),
        )
        if cur.rowcount == 0:
            return None
        row = db.execute("SELECT user_id FROM stories WHERE story_id = ?", (story_id,)).fetchone()
        invalidate_stories_cache()
        if row and row["user_id"] and row["user_id"] != uid:
            return row["user_id"]
        return None


@router.post("/{story_id}/view")
async def mark_story_viewed(story_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    owner_uid = await run_in_threadpool(_mark_story_viewed_sync, story_id, uid, ts)
    # Notify only the story owner instead of broadcasting story views globally to every user
    if owner_uid:
        await manager.send_to_user(owner_uid, {"event": "story_updated", "storyId": story_id})
    return {"success": True}


def _react_to_story_sync(story_id: str, payload: StoryReactionRequest, uid: str, ts: int, base_url: str):
    story_owner = ""
    notif_payload = None
    with get_db(write=True) as db:
        story = db.execute("SELECT * FROM stories WHERE story_id = ?", (story_id,)).fetchone()
        if not story:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story not found")
        user = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        db.execute(
            """
            INSERT INTO story_reactions(story_id, reactor_uid, reactor_username, reactor_profile_image, emoji, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, reactor_uid) DO UPDATE SET
                emoji=excluded.emoji,
                timestamp=excluded.timestamp
            """,
            (story_id, uid, user["username"], user["profile_image"] or "", payload.emoji, ts),
        )
        db.execute(
            """
            INSERT INTO story_viewers(story_id, viewer_uid, timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(story_id, viewer_uid) DO NOTHING
            """,
            (story_id, uid, ts),
        )

        story_owner = story["user_id"]
        if story_owner and story_owner != uid:
            nid = str(uuid.uuid4())
            ntext = f"reacted {payload.emoji} to your story"
            db.execute(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, post_id, post_media_url, text, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, 'story_reaction', ?, ?, ?, ?, 0, 0)
                """,
                (
                    nid,
                    story_owner,
                    uid,
                    user["username"],
                    user["profile_image"] or "",
                    story_id,
                    story["media_url"],
                    ntext,
                    ts,
                ),
            )
            notif_payload = {
                "event": "notification",
                "notificationId": nid,
                "type": "story_reaction",
                "senderId": uid,
                "senderUsername": user["username"],
                "senderProfileImage": resolve_url(user["profile_image"] or "", base_url=base_url),
                "text": ntext,
            }
    invalidate_stories_cache()
    return story_owner, notif_payload


@router.post("/{story_id}/react")
async def react_to_story(
    story_id: str,
    payload: StoryReactionRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    ts = now_ms()
    base_url = get_request_base_url(request)
    story_owner, notif_payload = await run_in_threadpool(
        _react_to_story_sync, story_id, payload, uid, ts, base_url
    )

    if story_owner and notif_payload:
        await manager.send_to_user(story_owner, notif_payload)
    if story_owner:
        await manager.send_to_user(story_owner, {"event": "story_updated", "storyId": story_id})
    return {"success": True}


def _delete_story_sync(story_id: str, uid: str) -> bool:
    with get_db(write=True) as db:
        row = db.execute("SELECT user_id FROM stories WHERE story_id = ?", (story_id,)).fetchone()
        if not row:
            return False
        if row["user_id"] != uid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another user's story")
        db.execute("DELETE FROM stories WHERE story_id = ?", (story_id,))
        db.execute("DELETE FROM story_viewers WHERE story_id = ?", (story_id,))
        db.execute("DELETE FROM story_reactions WHERE story_id = ?", (story_id,))
    invalidate_stories_cache()
    return True


@router.delete("/{story_id}")
async def delete_story(story_id: str, uid: str = Depends(get_current_uid)):
    deleted = await run_in_threadpool(_delete_story_sync, story_id, uid)
    if deleted:
        manager.schedule_broadcast("story_updated", {"event": "story_updated", "storyId": story_id}, delay_s=0.2)
    return {"success": True}

