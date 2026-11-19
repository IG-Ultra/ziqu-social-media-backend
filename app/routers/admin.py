import asyncio
import uuid
from typing import List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..cache import (
    invalidate_feed_caches,
    invalidate_stories_cache,
    invalidate_user_caches,
)
from ..config import settings
from ..database import get_db
from ..realtime import manager
from ..security import invalidate_user_auth_cache, require_admin_uid
from ..utils import get_request_base_url, now_ms, resolve_url, row_to_post, row_to_user

router = APIRouter(prefix="/api/admin", tags=["Owner Admin"])


class ResolveReportRequest(BaseModel):
    status: str = "resolved"  # "resolved" | "dismissed"


class BanUserRequest(BaseModel):
    ban: Optional[bool] = None


class BroadcastAnnouncementRequest(BaseModel):
    title: str = "ZIQU Official"
    message: str


ACTIVE_WINDOW_MS = 5 * 60 * 1000  # Consider users active within last 5 minutes or live WS as online


def _compute_online_uids(db, now: int) -> set:
    ws_uids = list(manager.online_uids())
    recent_cutoff = now - ACTIVE_WINDOW_MS
    rows = db.execute(
        """
        SELECT up.uid
        FROM user_presence up
        JOIN users u ON u.uid = up.uid
        WHERE u.is_banned = 0
          AND (up.is_online = 1 OR up.last_seen >= ?)
        """,
        (recent_cutoff,),
    ).fetchall()
    db_uids = {r["uid"] for r in rows}
    if not ws_uids:
        return db_uids

    for i in range(0, len(ws_uids), 500):
        chunk = ws_uids[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        for r in db.execute(
            f"SELECT uid FROM users WHERE is_banned = 0 AND uid IN ({placeholders})",
            tuple(chunk),
        ).fetchall():
            db_uids.add(r["uid"])
    return db_uids


@router.get("/dashboard")
def get_admin_dashboard(admin_uid: str = Depends(require_admin_uid)):
    now = now_ms()
    with get_db(write=True) as db:
        # Keep the viewing admin marked active as well
        db.execute(
            """
            INSERT INTO user_presence(uid, is_online, last_seen)
            VALUES(?, 1, ?)
            ON CONFLICT(uid) DO UPDATE SET is_online=1, last_seen=excluded.last_seen
            """,
            (admin_uid, now),
        )
        stats_row = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE is_verified = 1) AS verified_users,
                (SELECT COUNT(*) FROM users WHERE is_banned = 1) AS banned_users,
                (SELECT COUNT(*) FROM posts WHERE is_reel = 0 AND media_type != 'video') AS total_posts,
                (SELECT COUNT(*) FROM posts WHERE is_reel = 1 OR media_type = 'video') AS total_reels,
                (SELECT COUNT(*) FROM stories WHERE expires_at > ?) AS active_stories,
                (SELECT COUNT(*) FROM conversations) AS total_chats,
                (SELECT COUNT(*) FROM groups) AS total_groups,
                (SELECT COUNT(*) FROM reports WHERE status = 'pending') AS pending_reports,
                (SELECT COUNT(*) FROM reports) AS total_reports
            """,
            (now,),
        ).fetchone()
        online_uids = _compute_online_uids(db, now)

    return {
        "totalUsers": int(stats_row["total_users"] or 0),
        "verifiedUsers": int(stats_row["verified_users"] or 0),
        "bannedUsers": int(stats_row["banned_users"] or 0),
        "totalPosts": int(stats_row["total_posts"] or 0),
        "totalReels": int(stats_row["total_reels"] or 0),
        "activeStories": int(stats_row["active_stories"] or 0),
        "totalChats": int(stats_row["total_chats"] or 0),
        "totalGroups": int(stats_row["total_groups"] or 0),
        "pendingReports": int(stats_row["pending_reports"] or 0),
        "totalReports": int(stats_row["total_reports"] or 0),
        "onlineUsers": len(online_uids),
    }


@router.get("/users")
def admin_list_users(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=300),
    admin_uid: str = Depends(require_admin_uid),
):
    cleaned = q.strip().lower().lstrip("@")
    now = now_ms()
    with get_db() as db:
        online_uids = _compute_online_uids(db, now)
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

    users_out = []
    for r in rows:
        u = row_to_user(r, request)
        u["isOnline"] = (r["uid"] in online_uids) and not u["isBanned"]
        users_out.append(u)
    return users_out


def _admin_toggle_verify_sync(target_uid: str) -> int:
    with get_db(write=True) as db:
        row = db.execute("SELECT uid, is_verified FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        new_ver = 0 if row["is_verified"] else 1
        db.execute("UPDATE users SET is_verified = ? WHERE uid = ?", (new_ver, target_uid))
        db.execute("UPDATE posts SET is_verified = ? WHERE user_id = ?", (new_ver, target_uid))
        db.execute("UPDATE comments SET is_verified = ? WHERE user_id = ?", (new_ver, target_uid))
        db.execute("UPDATE stories SET is_verified = ? WHERE user_id = ?", (new_ver, target_uid))
    invalidate_user_caches(target_uid)
    invalidate_feed_caches()
    invalidate_stories_cache()
    return new_ver


@router.post("/users/{target_uid}/verify")
async def admin_toggle_verify(target_uid: str, admin_uid: str = Depends(require_admin_uid)):
    new_ver = await run_in_threadpool(_admin_toggle_verify_sync, target_uid)
    manager.schedule_broadcast(f"profile_updated:{target_uid}", {"event": "profile_updated", "uid": target_uid}, delay_s=0.15)
    return {"success": True, "isVerified": bool(new_ver)}


def _admin_toggle_ban_sync(target_uid: str, admin_uid: str, explicit_ban: Optional[bool] = None) -> int:
    if target_uid == admin_uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot ban your own owner account.")

    with get_db(write=True) as db:
        row = db.execute("SELECT uid, email, is_banned FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if (row["email"] or "").strip().lower() in settings.ADMIN_EMAIL_SET:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot ban an owner admin account.")

        if explicit_ban is None:
            new_ban = 0 if row["is_banned"] else 1
        else:
            new_ban = 1 if explicit_ban else 0
        db.execute("UPDATE users SET is_banned = ? WHERE uid = ?", (new_ban, target_uid))

    invalidate_user_auth_cache(target_uid)
    invalidate_feed_caches()
    invalidate_stories_cache()
    return new_ban


@router.post("/users/{target_uid}/ban")
async def admin_toggle_ban(
    target_uid: str,
    payload: Optional[BanUserRequest] = Body(default=None),
    admin_uid: str = Depends(require_admin_uid),
):
    explicit_ban = payload.ban if payload is not None else None
    new_ban = await run_in_threadpool(_admin_toggle_ban_sync, target_uid, admin_uid, explicit_ban)

    if new_ban == 1:
        await manager.disconnect_user(
            target_uid,
            reason="Your account has been suspended by an administrator.",
        )
        manager.schedule_broadcast("post_deleted", {"event": "post_deleted", "userId": target_uid}, delay_s=0.15)
        manager.schedule_broadcast("story_updated", {"event": "story_updated", "userId": target_uid}, delay_s=0.15)
    manager.schedule_broadcast(f"profile_updated:{target_uid}", {"event": "profile_updated", "uid": target_uid}, delay_s=0.15)
    return {"success": True, "isBanned": bool(new_ban)}


def _admin_delete_user_sync(target_uid: str, admin_uid: str) -> bool:
    if target_uid == admin_uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete owner admin account.")

    with get_db(write=True) as db:
        row = db.execute("SELECT uid, email FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not row:
            return False
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
        db.execute("DELETE FROM reports WHERE reporter_uid = ? OR reported_uid = ?", (target_uid, target_uid))
        db.execute("DELETE FROM users WHERE uid = ?", (target_uid,))

    invalidate_user_auth_cache(target_uid)
    invalidate_feed_caches()
    invalidate_stories_cache()
    return True


@router.delete("/users/{target_uid}")
async def admin_delete_user(target_uid: str, admin_uid: str = Depends(require_admin_uid)):
    existed = await run_in_threadpool(_admin_delete_user_sync, target_uid, admin_uid)
    if not existed:
        return {"success": True}

    await manager.disconnect_user(target_uid, reason="Your account has been deleted by an administrator.")
    manager.schedule_broadcast("post_deleted", {"event": "post_deleted", "userId": target_uid}, delay_s=0.15)
    manager.schedule_broadcast("story_updated", {"event": "story_updated", "userId": target_uid}, delay_s=0.15)
    return {"success": True}


@router.get("/reports")
def admin_list_reports(
    request: Request,
    status_filter: str = Query(default="all", alias="status"),
    admin_uid: str = Depends(require_admin_uid),
):
    with get_db() as db:
        base_sql = """
            SELECT
                r.*,
                COALESCE(NULLIF(r.reporter_username, ''), ru.username, '') AS eff_reporter_username,
                COALESCE(NULLIF(r.reported_username, ''), tu.username, '') AS eff_reported_username,
                COALESCE(tu.is_banned, 0) AS reported_is_banned,
                tu.uid AS reported_user_exists_uid,
                p.post_id AS post_exists_id,
                p.caption AS post_caption,
                p.media_url AS post_media_url,
                p.thumbnail_url AS post_thumb_url,
                c.comment_id AS comment_exists_id,
                c.comment_text AS comment_text,
                s.story_id AS story_exists_id,
                s.caption AS story_caption,
                s.media_url AS story_media_url
            FROM reports r
            LEFT JOIN users ru ON ru.uid = r.reporter_uid
            LEFT JOIN users tu ON tu.uid = r.reported_uid
            LEFT JOIN posts p ON r.target_type IN ('post', 'reel') AND p.post_id = r.target_id
            LEFT JOIN comments c ON r.target_type = 'comment' AND c.comment_id = r.target_id
            LEFT JOIN stories s ON r.target_type = 'story' AND s.story_id = r.target_id
        """
        if status_filter and status_filter != "all":
            rows = db.execute(
                base_sql + " WHERE r.status = ? ORDER BY r.timestamp DESC LIMIT 150",
                (status_filter,),
            ).fetchall()
        else:
            rows = db.execute(base_sql + " ORDER BY r.timestamp DESC LIMIT 150").fetchall()

        results = []
        for r in rows:
            preview = ""
            media_preview = ""
            ttype = r["target_type"] or "user"
            target_exists = False
            if ttype in ("post", "reel"):
                target_exists = bool(r["post_exists_id"])
                if target_exists:
                    preview = r["post_caption"] or ("Video Reel" if ttype == "reel" else "Photo Post")
                    raw_media = r["post_thumb_url"] or r["post_media_url"] or ""
                    media_preview = resolve_url(raw_media, request)
                else:
                    preview = "[Content removed]"
            elif ttype == "comment":
                target_exists = bool(r["comment_exists_id"])
                preview = r["comment_text"] if target_exists else "[Comment removed]"
            elif ttype == "story":
                target_exists = bool(r["story_exists_id"])
                if target_exists:
                    preview = r["story_caption"] or "Story"
                    media_preview = resolve_url(r["story_media_url"] or "", request)
                else:
                    preview = "[Story removed]"
            else:
                target_exists = bool(r["reported_user_exists_uid"])

            results.append(
                {
                    "reportId": r["report_id"],
                    "reporterUid": r["reporter_uid"],
                    "reporterUsername": r["eff_reporter_username"] or "",
                    "reportedUid": r["reported_uid"],
                    "reportedUsername": r["eff_reported_username"] or "",
                    "reportedIsBanned": bool(r["reported_is_banned"]),
                    "reason": r["reason"] or "",
                    "details": r["details"] or "",
                    "targetType": ttype,
                    "targetId": r["target_id"] or "",
                    "targetExists": target_exists,
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
    with get_db(write=True) as db:
        db.execute("UPDATE reports SET status = ? WHERE report_id = ?", (new_status, report_id))
    return {"success": True, "status": new_status}


def _admin_delete_reported_target_sync(report_id: str) -> Tuple[str, str, str]:
    with get_db(write=True) as db:
        rep = db.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
        if not rep:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
        ttype = (rep["target_type"] or "user").strip().lower()
        tid = (rep["target_id"] or "").strip()
        extra_post_id = ""

        if ttype in ("post", "reel") and tid:
            post = db.execute("SELECT user_id FROM posts WHERE post_id = ?", (tid,)).fetchone()
            if post:
                author_uid = post["user_id"]
                db.execute("DELETE FROM posts WHERE post_id = ?", (tid,))
                db.execute("DELETE FROM likes WHERE post_id = ?", (tid,))
                db.execute("DELETE FROM saved_posts WHERE post_id = ?", (tid,))
                db.execute("DELETE FROM comments WHERE post_id = ?", (tid,))
                db.execute("UPDATE users SET posts_count = MAX(0, posts_count - 1) WHERE uid = ?", (author_uid,))
                invalidate_feed_caches()
                invalidate_user_caches(author_uid)
        elif ttype == "comment" and tid:
            comment = db.execute("SELECT post_id FROM comments WHERE comment_id = ?", (tid,)).fetchone()
            if comment:
                extra_post_id = comment["post_id"] or ""
                db.execute("DELETE FROM comments WHERE comment_id = ?", (tid,))
                if extra_post_id:
                    db.execute("UPDATE posts SET comments_count = MAX(0, comments_count - 1) WHERE post_id = ?", (extra_post_id,))
                invalidate_feed_caches()
        elif ttype == "story" and tid:
            story = db.execute("SELECT story_id FROM stories WHERE story_id = ?", (tid,)).fetchone()
            if story:
                db.execute("DELETE FROM stories WHERE story_id = ?", (tid,))
                db.execute("DELETE FROM story_viewers WHERE story_id = ?", (tid,))
                db.execute("DELETE FROM story_reactions WHERE story_id = ?", (tid,))
                invalidate_stories_cache()
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove content for a user-only report.")

        if tid:
            db.execute(
                "UPDATE reports SET status = 'resolved' WHERE target_type = ? AND target_id = ? AND status = 'pending'",
                (ttype, tid),
            )
        db.execute("UPDATE reports SET status = 'resolved' WHERE report_id = ?", (report_id,))

    return ttype, tid, extra_post_id


@router.delete("/reports/{report_id}/target")
async def admin_delete_reported_target(report_id: str, admin_uid: str = Depends(require_admin_uid)):
    ttype, tid, extra_post_id = await run_in_threadpool(_admin_delete_reported_target_sync, report_id)
    if ttype in ("post", "reel") and tid:
        manager.schedule_broadcast("post_deleted", {"event": "post_deleted", "postId": tid}, delay_s=0.15)
    elif ttype == "story" and tid:
        manager.schedule_broadcast("story_updated", {"event": "story_updated", "storyId": tid}, delay_s=0.15)
    elif ttype == "comment" and extra_post_id:
        manager.schedule_broadcast(
            f"comments_updated:{extra_post_id}",
            {"event": "comments_updated", "postId": extra_post_id},
            delay_s=0.15,
        )
    return {"success": True, "targetType": ttype, "targetId": tid}


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


def _admin_delete_post_sync(post_id: str) -> bool:
    with get_db(write=True) as db:
        post = db.execute("SELECT user_id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not post:
            return False
        author_uid = post["user_id"]
        db.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM saved_posts WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        db.execute("UPDATE users SET posts_count = MAX(0, posts_count - 1) WHERE uid = ?", (author_uid,))
    invalidate_feed_caches()
    invalidate_user_caches(author_uid)
    return True


@router.delete("/posts/{post_id}")
async def admin_delete_post(post_id: str, admin_uid: str = Depends(require_admin_uid)):
    deleted = await run_in_threadpool(_admin_delete_post_sync, post_id)
    if deleted:
        manager.schedule_broadcast("post_deleted", {"event": "post_deleted", "postId": post_id}, delay_s=0.15)
    return {"success": True}


def _admin_broadcast_sync(
    admin_uid: str, title: str, text: str, ts: int, base_url: str
) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    with get_db(write=True) as db:
        admin_user = db.execute("SELECT username, profile_image FROM users WHERE uid = ?", (admin_uid,)).fetchone()
        admin_username = admin_user["username"] if admin_user else "ziqu"
        admin_avatar = admin_user["profile_image"] if admin_user else ""
        resolved_avatar = resolve_url(admin_avatar or "", base_url=base_url)

        recipients = db.execute(
            "SELECT uid FROM users WHERE is_banned = 0 AND uid != ?",
            (admin_uid,),
        ).fetchall()

        insert_rows = []
        sent_pairs: List[Tuple[str, str]] = []
        for r in recipients:
            ruid = r["uid"]
            nid = str(uuid.uuid4())
            insert_rows.append((nid, ruid, admin_uid, admin_username, admin_avatar or "", text, title, ts))
            sent_pairs.append((ruid, nid))

        if insert_rows:
            db.executemany(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, text, title, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, 'admin', ?, ?, ?, 0, 0)
                """,
                insert_rows,
            )
    return admin_username, resolved_avatar, title, sent_pairs


async def _fanout_announcement_ws(
    online_pairs: List[Tuple[str, str]],
    admin_uid: str,
    admin_username: str,
    resolved_avatar: str,
    title: str,
    text: str,
) -> None:
    for i in range(0, len(online_pairs), 100):
        chunk = online_pairs[i : i + 100]
        await asyncio.gather(
            *(
                manager.send_to_user(
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
                for ruid, nid in chunk
            ),
            return_exceptions=True,
        )


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
    base_url = get_request_base_url(request)

    admin_username, resolved_avatar, title, sent_pairs = await run_in_threadpool(
        _admin_broadcast_sync, admin_uid, title, text, ts, base_url
    )

    online_now = manager.online_uids()
    online_pairs = [(ruid, nid) for ruid, nid in sent_pairs if ruid in online_now]
    if online_pairs:
        asyncio.create_task(
            _fanout_announcement_ws(online_pairs, admin_uid, admin_username, resolved_avatar, title, text)
        )

    return {"success": True, "recipientsCount": len(sent_pairs)}

