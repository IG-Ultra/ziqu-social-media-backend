import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..realtime import manager
from ..security import require_admin_uid
from ..utils import now_ms, resolve_url, row_to_post, row_to_user

router = APIRouter(prefix="/api/admin", tags=["Owner Admin"])


class ResolveReportRequest(BaseModel):
    status: str = "resolved"  # "resolved" | "dismissed"


class BroadcastAnnouncementRequest(BaseModel):
    title: str = "ZIQU Official"
    message: str


@router.get("/dashboard")
def get_admin_dashboard(admin_uid: str = Depends(require_admin_uid)):
    now = now_ms()
    with get_db() as db:
        total_users = db.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        verified_users = db.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_verified = 1").fetchone()["cnt"]
        banned_users = db.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_banned = 1").fetchone()["cnt"]
        total_posts = db.execute("SELECT COUNT(*) AS cnt FROM posts WHERE is_reel = 0 AND media_type != 'video'").fetchone()["cnt"]
        total_reels = db.execute("SELECT COUNT(*) AS cnt FROM posts WHERE is_reel = 1 OR media_type = 'video'").fetchone()["cnt"]
        active_stories = db.execute("SELECT COUNT(*) AS cnt FROM stories WHERE expires_at > ?", (now,)).fetchone()["cnt"]
        total_chats = db.execute("SELECT COUNT(*) AS cnt FROM conversations").fetchone()["cnt"]
        total_groups = db.execute("SELECT COUNT(*) AS cnt FROM groups").fetchone()["cnt"]
        pending_reports = db.execute("SELECT COUNT(*) AS cnt FROM reports WHERE status = 'pending'").fetchone()["cnt"]
        total_reports = db.execute("SELECT COUNT(*) AS cnt FROM reports").fetchone()["cnt"]

    stats = manager.active_stats()
    return {
        "totalUsers": int(total_users or 0),
        "verifiedUsers": int(verified_users or 0),
        "bannedUsers": int(banned_users or 0),
        "totalPosts": int(total_posts or 0),
        "totalReels": int(total_reels or 0),
        "activeStories": int(active_stories or 0),
        "totalChats": int(total_chats or 0),
        "totalGroups": int(total_groups or 0),
        "pendingReports": int(pending_reports or 0),
        "totalReports": int(total_reports or 0),
        "onlineUsers": int(stats.get("onlineUsers", 0)),
    }


@router.get("/users")
def admin_list_users(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=300),
    admin_uid: str = Depends(require_admin_uid),
):
    cleaned = q.strip().lower().lstrip("@")
    with get_db() as db:
        if cleaned:
            pattern = f"%{cleaned}%"
            rows = db.execute(
                """
                SELECT * FROM users
                WHERE username_lower LIKE ? OR lower(display_name) LIKE ? OR lower(email) LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (pattern, pattern, pattern, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [row_to_user(r, request) for r in rows]


@router.post("/users/{target_uid}/verify")
async def admin_toggle_verify(target_uid: str, admin_uid: str = Depends(require_admin_uid)):
    with get_db() as db:
        row = db.execute("SELECT uid, is_verified FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        new_ver = 0 if row["is_verified"] else 1
        db.execute("UPDATE users SET is_verified = ? WHERE uid = ?", (new_ver, target_uid))
        db.execute("UPDATE posts SET is_verified = ? WHERE user_id = ?", (new_ver, target_uid))
        db.execute("UPDATE comments SET is_verified = ? WHERE user_id = ?", (new_ver, target_uid))
        db.execute("UPDATE stories SET is_verified = ? WHERE user_id = ?", (new_ver, target_uid))

    await manager.broadcast_all({"event": "profile_updated", "uid": target_uid})
    return {"success": True, "isVerified": bool(new_ver)}


@router.post("/users/{target_uid}/ban")
async def admin_toggle_ban(target_uid: str, admin_uid: str = Depends(require_admin_uid)):
    if target_uid == admin_uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot ban your own owner account.")

    with get_db() as db:
        row = db.execute("SELECT uid, email, is_banned FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if (row["email"] or "").strip().lower() in settings.ADMIN_EMAIL_SET:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot ban an owner admin account.")

        new_ban = 0 if row["is_banned"] else 1
        db.execute("UPDATE users SET is_banned = ? WHERE uid = ?", (new_ban, target_uid))

    return {"success": True, "isBanned": bool(new_ban)}


@router.delete("/users/{target_uid}")
async def admin_delete_user(target_uid: str, admin_uid: str = Depends(require_admin_uid)):
    if target_uid == admin_uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete owner admin account.")

    with get_db() as db:
        row = db.execute("SELECT uid, email FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not row:
            return {"success": True}
        if (row["email"] or "").strip().lower() in settings.ADMIN_EMAIL_SET:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete an owner admin account.")

        db.execute("DELETE FROM posts WHERE user_id = ?", (target_uid,))
        db.execute("DELETE FROM comments WHERE user_id = ?", (target_uid,))
        db.execute("DELETE FROM likes WHERE user_id = ?", (target_uid,))
        db.execute("DELETE FROM saved_posts WHERE user_id = ?", (target_uid,))
        db.execute("DELETE FROM stories WHERE user_id = ?", (target_uid,))
        db.execute("DELETE FROM follows WHERE follower_uid = ? OR following_uid = ?", (target_uid, target_uid))
        db.execute("DELETE FROM blocks WHERE blocker_uid = ? OR blocked_uid = ?", (target_uid, target_uid))
        db.execute("DELETE FROM notifications WHERE receiver_id = ? OR sender_id = ?", (target_uid, target_uid))
        db.execute("DELETE FROM user_presence WHERE uid = ?", (target_uid,))
        db.execute("DELETE FROM users WHERE uid = ?", (target_uid,))

    await manager.broadcast_all({"event": "post_deleted", "userId": target_uid})
    return {"success": True}


@router.get("/reports")
def admin_list_reports(
    request: Request,
    status_filter: str = Query(default="all", alias="status"),
    admin_uid: str = Depends(require_admin_uid),
):
    with get_db() as db:
        if status_filter and status_filter != "all":
            rows = db.execute(
                "SELECT * FROM reports WHERE status = ? ORDER BY timestamp DESC LIMIT 150",
                (status_filter,),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM reports ORDER BY timestamp DESC LIMIT 150").fetchall()

        results = []
        for r in rows:
            preview = ""
            media_preview = ""
            if r["target_type"] in ("post", "reel") and r["target_id"]:
                p = db.execute("SELECT caption, media_url FROM posts WHERE post_id = ?", (r["target_id"],)).fetchone()
                if p:
                    preview = p["caption"] or "Media post"
                    media_preview = resolve_url(p["media_url"] or "", request)
            elif r["target_type"] == "comment" and r["target_id"]:
                c = db.execute("SELECT comment_text FROM comments WHERE comment_id = ?", (r["target_id"],)).fetchone()
                if c:
                    preview = c["comment_text"] or ""

            results.append(
                {
                    "reportId": r["report_id"],
                    "reporterUid": r["reporter_uid"],
                    "reporterUsername": r["reporter_username"] or "",
                    "reportedUid": r["reported_uid"],
                    "reportedUsername": r["reported_username"] or "",
                    "reason": r["reason"] or "",
                    "details": r["details"] or "",
                    "targetType": r["target_type"] or "user",
                    "targetId": r["target_id"] or "",
                    "timestamp": int(r["timestamp"]),
                    "status": r["status"] or "pending",
                    "targetPreview": preview,
                    "targetMediaUrl": media_preview,
                }
            )
    return results


@router.post("/reports/{report_id}/resolve")
def admin_resolve_report(
    report_id: str,
    payload: ResolveReportRequest,
    admin_uid: str = Depends(require_admin_uid),
):
    new_status = payload.status if payload.status in ("resolved", "dismissed", "pending") else "resolved"
    with get_db() as db:
        db.execute("UPDATE reports SET status = ? WHERE report_id = ?", (new_status, report_id))
    return {"success": True, "status": new_status}


@router.get("/posts")
def admin_list_posts(
    request: Request,
    reelsOnly: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
    admin_uid: str = Depends(require_admin_uid),
):
    with get_db() as db:
        if reelsOnly:
            rows = db.execute(
                "SELECT * FROM posts WHERE is_reel = 1 OR media_type = 'video' ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM posts ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [row_to_post(r, request) for r in rows]


@router.delete("/posts/{post_id}")
async def admin_delete_post(post_id: str, admin_uid: str = Depends(require_admin_uid)):
    with get_db() as db:
        post = db.execute("SELECT user_id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not post:
            return {"success": True}
        author_uid = post["user_id"]
        db.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM saved_posts WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        db.execute("UPDATE users SET posts_count = MAX(0, posts_count - 1) WHERE uid = ?", (author_uid,))

    await manager.broadcast_all({"event": "post_deleted", "postId": post_id})
    return {"success": True}


@router.post("/broadcast")
async def admin_broadcast_announcement(
    payload: BroadcastAnnouncementRequest,
    request: Request,
    admin_uid: str = Depends(require_admin_uid),
):
    text = payload.message.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Announcement message cannot be empty.")
    title = payload.title.strip() or "ZIQU Official"
    ts = now_ms()

    with get_db() as db:
        admin_user = db.execute("SELECT username, profile_image FROM users WHERE uid = ?", (admin_uid,)).fetchone()
        admin_username = admin_user["username"] if admin_user else "ziqu"
        admin_avatar = admin_user["profile_image"] if admin_user else ""
        resolved_avatar = resolve_url(admin_avatar or "", request)

        recipients = db.execute(
            "SELECT uid FROM users WHERE is_banned = 0 AND uid != ?",
            (admin_uid,),
        ).fetchall()

        sent_pairs = []
        for r in recipients:
            ruid = r["uid"]
            nid = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, text, title, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, 'admin', ?, ?, ?, 0, 0)
                """,
                (nid, ruid, admin_uid, admin_username, admin_avatar or "", text, title, ts),
            )
            sent_pairs.append((ruid, nid))

    for ruid, nid in sent_pairs:
        await manager.send_to_user(
            ruid,
            {
                "event": "notification",
                "notificationId": nid,
                "type": "admin",
                "title": title,
                "senderId": admin_uid,
                "senderUsername": admin_username,
                "senderProfileImage": resolved_avatar,
                "text": text,
            },
        )

    return {"success": True, "recipientsCount": len(sent_pairs)}
