import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..database import get_db
from ..realtime import manager
from ..schemas import CreateCommentRequest, CreatePostRequest, PostMetricRequest
from ..security import get_current_uid, get_optional_uid
from ..utils import get_blocked_peer_ids, now_ms, row_to_comment, row_to_post

router = APIRouter(prefix="/api/posts", tags=["Posts & Reels"])


@router.post("")
async def create_post(
    payload: CreatePostRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    post_id = payload.postId or str(uuid.uuid4())
    created = now_ms()
    is_reel_int = 1 if (payload.isReel or payload.mediaType == "video") else 0

    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        db.execute(
            """
            INSERT INTO posts(
                post_id, user_id, username, user_profile_image,
                media_url, thumbnail_url, media_type, caption, mood,
                timestamp, likes_count, comments_count, shares_count, saves_count,
                watch_time_ms, views_count, completions_count, rewatch_count,
                is_reel, is_verified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, ?, ?)
            """,
            (
                post_id,
                uid,
                user["username"],
                user["profile_image"] or "",
                payload.mediaUrl.strip(),
                payload.thumbnailUrl.strip(),
                payload.mediaType.strip() or "image",
                payload.caption.strip(),
                payload.mood.strip(),
                created,
                is_reel_int,
                int(user["is_verified"] or 0),
            ),
        )
        db.execute("UPDATE users SET posts_count = posts_count + 1 WHERE uid = ?", (uid,))
        post_row = db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()

    post_dict = row_to_post(post_row, request)
    await manager.broadcast_all({"event": "post_created", "postId": post_id, "userId": uid})
    return post_dict


@router.get("/feed")
def get_feed(
    request: Request,
    limit: int = Query(default=100, ge=1, le=300),
    uid: Optional[str] = Depends(get_optional_uid),
):
    with get_db() as db:
        blocked = set(get_blocked_peer_ids(db, uid)) if uid else set()
        rows = db.execute("SELECT * FROM posts ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return [row_to_post(r, request) for r in rows if r["user_id"] not in blocked]


@router.get("/reels")
def get_reels(
    request: Request,
    limit: int = Query(default=150, ge=1, le=300),
    uid: Optional[str] = Depends(get_optional_uid),
):
    with get_db() as db:
        blocked = set(get_blocked_peer_ids(db, uid)) if uid else set()
        rows = db.execute(
            "SELECT * FROM posts WHERE is_reel = 1 OR media_type = 'video' ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            rows = db.execute("SELECT * FROM posts ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return [row_to_post(r, request) for r in rows if r["user_id"] not in blocked]


@router.get("/explore")
def get_explore(
    request: Request,
    limit: int = Query(default=60, ge=1, le=200),
    uid: Optional[str] = Depends(get_optional_uid),
):
    with get_db() as db:
        blocked = set(get_blocked_peer_ids(db, uid)) if uid else set()
        rows = db.execute("SELECT * FROM posts ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return [row_to_post(r, request) for r in rows if r["user_id"] not in blocked]


@router.get("/user/{target_uid}")
def get_user_posts(
    target_uid: str,
    request: Request,
    uid: Optional[str] = Depends(get_optional_uid),
):
    with get_db() as db:
        if uid and target_uid in set(get_blocked_peer_ids(db, uid)):
            return []
        rows = db.execute(
            "SELECT * FROM posts WHERE user_id = ? ORDER BY timestamp DESC",
            (target_uid,),
        ).fetchall()
    return [row_to_post(r, request) for r in rows]


@router.get("/likes/ids")
def get_my_liked_post_ids(uid: str = Depends(get_current_uid)):
    with get_db() as db:
        rows = db.execute("SELECT post_id FROM likes WHERE user_id = ?", (uid,)).fetchall()
    return [r["post_id"] for r in rows]


@router.get("/saved/ids")
def get_my_saved_post_ids(uid: str = Depends(get_current_uid)):
    with get_db() as db:
        rows = db.execute("SELECT post_id FROM saved_posts WHERE user_id = ?", (uid,)).fetchall()
    return [r["post_id"] for r in rows]


@router.get("/saved/me")
def get_my_saved_posts(request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT p.* FROM saved_posts s
            JOIN posts p ON p.post_id = s.post_id
            WHERE s.user_id = ?
            ORDER BY s.saved_at DESC
            """,
            (uid,),
        ).fetchall()
    return [row_to_post(r, request) for r in rows]


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        comment = db.execute("SELECT * FROM comments WHERE comment_id = ?", (comment_id,)).fetchone()
        if not comment:
            return {"success": True}
        post_id = comment["post_id"]
        post = db.execute("SELECT user_id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if comment["user_id"] != uid and (not post or post["user_id"] != uid):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this comment")

        db.execute("DELETE FROM comments WHERE comment_id = ?", (comment_id,))
        db.execute(
            "UPDATE posts SET comments_count = MAX(0, comments_count - 1) WHERE post_id = ?",
            (post_id,),
        )
    await manager.broadcast_all({"event": "comments_updated", "postId": post_id})
    return {"success": True}


@router.get("/{post_id}")
def get_post_by_id(post_id: str, request: Request):
    with get_db() as db:
        row = db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return row_to_post(row, request)


@router.delete("/{post_id}")
async def delete_post(post_id: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute("SELECT user_id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        if row["user_id"] != uid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another user's post")

        db.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM saved_posts WHERE post_id = ?", (post_id,))
        db.execute("UPDATE users SET posts_count = MAX(0, posts_count - 1) WHERE uid = ?", (uid,))

    await manager.broadcast_all({"event": "post_deleted", "postId": post_id, "userId": uid})
    return {"success": True}


@router.get("/{post_id}/like-status")
def get_like_status(post_id: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM likes WHERE post_id = ? AND user_id = ?",
            (post_id, uid),
        ).fetchone()
    return {"liked": row is not None}


@router.post("/{post_id}/like")
async def toggle_like(post_id: str, request: Request, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    notif_receiver = ""
    notif_payload = None

    with get_db() as db:
        post = db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not post:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        actor = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        existing = db.execute(
            "SELECT 1 FROM likes WHERE post_id = ? AND user_id = ?",
            (post_id, uid),
        ).fetchone()

        if existing:
            db.execute("DELETE FROM likes WHERE post_id = ? AND user_id = ?", (post_id, uid))
            db.execute("UPDATE posts SET likes_count = MAX(0, likes_count - 1) WHERE post_id = ?", (post_id,))
            liked = False
        else:
            db.execute(
                "INSERT INTO likes(post_id, user_id, timestamp) VALUES (?, ?, ?)",
                (post_id, uid, ts),
            )
            db.execute("UPDATE posts SET likes_count = likes_count + 1 WHERE post_id = ?", (post_id,))
            liked = True

        post_owner = post["user_id"]
        if post_owner and post_owner != uid and actor:
            nid = str(uuid.uuid4())
            ntype = "like" if liked else "unlike"
            ntext = "liked your post" if liked else "unliked your post"
            db.execute(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, post_id, post_media_url, text, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    nid,
                    post_owner,
                    uid,
                    actor["username"],
                    actor["profile_image"] or "",
                    ntype,
                    post_id,
                    post["media_url"],
                    ntext,
                    ts,
                ),
            )
            if liked:
                notif_receiver = post_owner
                notif_payload = {
                    "event": "notification",
                    "notificationId": nid,
                    "type": ntype,
                    "senderUsername": actor["username"],
                    "text": ntext,
                }

        updated = db.execute("SELECT likes_count FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        likes_count = int(updated["likes_count"] or 0) if updated else 0

    await manager.broadcast_all({"event": "post_updated", "postId": post_id})
    if notif_receiver and notif_payload:
        await manager.send_to_user(notif_receiver, notif_payload)

    return {"liked": liked, "likesCount": likes_count}


@router.get("/{post_id}/save-status")
def get_save_status(post_id: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM saved_posts WHERE post_id = ? AND user_id = ?",
            (post_id, uid),
        ).fetchone()
    return {"saved": row is not None}


@router.post("/{post_id}/save")
def toggle_save(post_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    with get_db() as db:
        post = db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not post:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        existing = db.execute(
            "SELECT 1 FROM saved_posts WHERE post_id = ? AND user_id = ?",
            (post_id, uid),
        ).fetchone()
        if existing:
            db.execute("DELETE FROM saved_posts WHERE post_id = ? AND user_id = ?", (post_id, uid))
            db.execute("UPDATE posts SET saves_count = MAX(0, saves_count - 1) WHERE post_id = ?", (post_id,))
            saved = False
        else:
            db.execute(
                "INSERT INTO saved_posts(post_id, user_id, saved_at) VALUES (?, ?, ?)",
                (post_id, uid, ts),
            )
            db.execute("UPDATE posts SET saves_count = saves_count + 1 WHERE post_id = ?", (post_id,))
            saved = True
    return {"saved": saved}


@router.post("/{post_id}/metrics")
def record_post_metrics(post_id: str, payload: PostMetricRequest):
    with get_db() as db:
        db.execute(
            """
            UPDATE posts
            SET views_count = views_count + 1,
                watch_time_ms = watch_time_ms + ?,
                completions_count = completions_count + ?,
                rewatch_count = rewatch_count + ?,
                shares_count = shares_count + ?
            WHERE post_id = ?
            """,
            (
                max(0, payload.watchTimeMs),
                1 if payload.completed else 0,
                1 if payload.rewatched else 0,
                1 if payload.shared else 0,
                post_id,
            ),
        )
    return {"success": True}


@router.get("/{post_id}/comments")
def list_comments(post_id: str, request: Request):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY timestamp ASC",
            (post_id,),
        ).fetchall()
    return [row_to_comment(r, request) for r in rows]


@router.post("/{post_id}/comments")
async def add_comment(
    post_id: str,
    payload: CreateCommentRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    text = payload.commentText.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Comment cannot be empty")
    comment_id = str(uuid.uuid4())
    ts = now_ms()
    notif_receiver = ""
    notif_payload = None

    with get_db() as db:
        post = db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not post:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        user = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        db.execute(
            """
            INSERT INTO comments(
                comment_id, post_id, user_id, username, user_profile_image,
                comment_text, timestamp, likes_count, parent_id,
                replied_to_user_id, replied_to_username, is_verified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                comment_id,
                post_id,
                uid,
                user["username"],
                user["profile_image"] or "",
                text,
                ts,
                payload.parentId.strip(),
                payload.repliedToUserId.strip(),
                payload.repliedToUsername.strip(),
                int(user["is_verified"] or 0),
            ),
        )
        db.execute("UPDATE posts SET comments_count = comments_count + 1 WHERE post_id = ?", (post_id,))

        receiver_id = (
            payload.repliedToUserId.strip()
            if payload.repliedToUserId.strip() and payload.repliedToUserId.strip() != uid
            else post["user_id"]
        )
        if receiver_id and receiver_id != uid:
            nid = str(uuid.uuid4())
            is_reply = bool(payload.parentId.strip())
            ntype = "reply" if is_reply else "comment"
            ntext = f"replied to your comment: {text}" if is_reply else f"commented: {text}"
            db.execute(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, post_id, post_media_url, comment_id, text, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    nid,
                    receiver_id,
                    uid,
                    user["username"],
                    user["profile_image"] or "",
                    ntype,
                    post_id,
                    post["media_url"],
                    comment_id,
                    ntext,
                    ts,
                ),
            )
            notif_receiver = receiver_id
            notif_payload = {
                "event": "notification",
                "notificationId": nid,
                "type": ntype,
                "senderUsername": user["username"],
                "text": ntext,
            }

        comment_row = db.execute("SELECT * FROM comments WHERE comment_id = ?", (comment_id,)).fetchone()

    await manager.broadcast_all({"event": "comments_updated", "postId": post_id})
    await manager.broadcast_all({"event": "post_updated", "postId": post_id})
    if notif_receiver and notif_payload:
        await manager.send_to_user(notif_receiver, notif_payload)

    return row_to_comment(comment_row, request)
