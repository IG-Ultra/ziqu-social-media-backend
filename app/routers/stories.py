import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..database import get_db
from ..realtime import manager
from ..schemas import CreateStoryRequest, StoryReactionRequest
from ..security import get_current_uid, get_optional_uid
from ..utils import get_blocked_peer_ids, now_ms, resolve_url, row_to_story

router = APIRouter(prefix="/api/stories", tags=["Stories"])

TWENTY_FOUR_HOURS_MS = 24 * 60 * 60 * 1000


@router.get("")
def list_active_stories(
    request: Request,
    uid: Optional[str] = Depends(get_optional_uid),
):
    now = now_ms()
    with get_db() as db:
        blocked = set(get_blocked_peer_ids(db, uid)) if uid else set()
        rows = db.execute(
            "SELECT * FROM stories WHERE expires_at > ? ORDER BY timestamp ASC",
            (now,),
        ).fetchall()
        result = [row_to_story(db, r, request) for r in rows if r["user_id"] not in blocked]
    return result


@router.post("")
async def create_story(
    payload: CreateStoryRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    story_id = str(uuid.uuid4())
    ts = now_ms()
    expires = ts + TWENTY_FOUR_HOURS_MS

    with get_db() as db:
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
        story_dict = row_to_story(db, row, request)

    await manager.broadcast_all({"event": "story_updated", "storyId": story_id})
    return story_dict


@router.post("/{story_id}/view")
async def mark_story_viewed(story_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO story_viewers(story_id, viewer_uid, timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(story_id, viewer_uid) DO NOTHING
            """,
            (story_id, uid, ts),
        )
    await manager.broadcast_all({"event": "story_updated", "storyId": story_id})
    return {"success": True}


@router.post("/{story_id}/react")
async def react_to_story(
    story_id: str,
    payload: StoryReactionRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    ts = now_ms()
    story_owner = ""
    notif_payload = None
    with get_db() as db:
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
                "senderProfileImage": resolve_url(user["profile_image"] or "", request),
                "text": ntext,
            }

    await manager.broadcast_all({"event": "story_updated", "storyId": story_id})
    if story_owner and notif_payload:
        await manager.send_to_user(story_owner, notif_payload)
    return {"success": True}


@router.delete("/{story_id}")
async def delete_story(story_id: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute("SELECT user_id FROM stories WHERE story_id = ?", (story_id,)).fetchone()
        if not row:
            return {"success": True}
        if row["user_id"] != uid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another user's story")
        db.execute("DELETE FROM stories WHERE story_id = ?", (story_id,))
        db.execute("DELETE FROM story_viewers WHERE story_id = ?", (story_id,))
        db.execute("DELETE FROM story_reactions WHERE story_id = ?", (story_id,))
    await manager.broadcast_all({"event": "story_updated", "storyId": story_id})
    return {"success": True}
