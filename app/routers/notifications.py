import uuid

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from ..database import get_db
from ..realtime import manager
from ..schemas import AckPendingPushRequest, CreateNotificationRequest
from ..security import get_current_uid
from ..utils import (
    get_blocked_peer_set_cached,
    get_request_base_url,
    is_pair_blocked,
    now_ms,
    row_to_notification,
)

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


@router.get("")
def list_notifications(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    uid: str = Depends(get_current_uid),
):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT n.* FROM notifications n
            LEFT JOIN users su ON su.uid = n.sender_id
            WHERE n.receiver_id = ?
              AND n.type NOT IN ('chat', 'group_chat')
              AND (n.type = 'admin' OR COALESCE(su.is_banned, 0) = 0)
            ORDER BY n.timestamp DESC
            LIMIT ?
            """,
            (uid, limit),
        ).fetchall()
    blocked = get_blocked_peer_set_cached(uid)
    return [row_to_notification(r, request) for r in rows if r["sender_id"] not in blocked]


def _create_notification_sync(payload: CreateNotificationRequest, uid: str, nid: str, ts: int, base_url: str):
    with get_db(write=True) as db:
        if is_pair_blocked(db, uid, payload.receiverId):
            return "", None
        rec = db.execute("SELECT is_banned FROM users WHERE uid = ?", (payload.receiverId,)).fetchone()
        if not rec or rec["is_banned"]:
            return "", None
        sender = db.execute("SELECT username, profile_image FROM users WHERE uid = ?", (uid,)).fetchone()
        s_name = sender["username"] if sender else ""
        s_img = sender["profile_image"] if sender else ""
        db.execute(
            """
            INSERT INTO notifications(
                notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                type, post_id, post_media_url, comment_id, text, conversation_id, group_id, title,
                timestamp, is_read, pushed_to_system
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                nid,
                payload.receiverId,
                uid,
                s_name,
                s_img,
                payload.type,
                payload.postId,
                payload.postMediaUrl,
                payload.commentId,
                payload.text,
                payload.conversationId,
                payload.groupId,
                payload.title,
                ts,
            ),
        )
        row = db.execute("SELECT * FROM notifications WHERE notification_id = ?", (nid,)).fetchone()
        notif_dict = row_to_notification(row)
        if base_url:
            for k in ("senderProfileImage", "postMediaUrl"):
                val = notif_dict.get(k) or ""
                if val.startswith("/"):
                    notif_dict[k] = f"{base_url}{val}"
    return s_name, notif_dict


@router.post("")
async def create_notification(
    payload: CreateNotificationRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    if not payload.receiverId or payload.receiverId == uid:
        return {"success": True}
    nid = str(uuid.uuid4())
    ts = now_ms()
    base_url = get_request_base_url(request)

    s_name, notif_dict = await run_in_threadpool(
        _create_notification_sync, payload, uid, nid, ts, base_url
    )
    if notif_dict is None:
        return {"success": True}

    await manager.send_to_user(
        payload.receiverId,
        {
            "event": "notification",
            "notification": notif_dict,
            "notificationId": nid,
            "type": payload.type,
            "senderId": uid,
            "senderUsername": s_name,
            "senderProfileImage": notif_dict.get("senderProfileImage", ""),
            "title": payload.title,
            "text": payload.text,
        },
    )
    return notif_dict


@router.get("/pending-push")
def get_pending_push_notifications(request: Request, uid: str = Depends(get_current_uid)):
    recent_cutoff = now_ms() - (24 * 60 * 60 * 1000)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT n.* FROM notifications n
            LEFT JOIN users su ON su.uid = n.sender_id
            WHERE n.receiver_id = ?
              AND n.is_read = 0
              AND n.pushed_to_system = 0
              AND n.timestamp >= ?
              AND (n.type = 'admin' OR COALESCE(su.is_banned, 0) = 0)
            ORDER BY n.timestamp ASC
            LIMIT 50
            """,
            (uid, recent_cutoff),
        ).fetchall()
    blocked = get_blocked_peer_set_cached(uid)
    return [row_to_notification(r, request) for r in rows if r["sender_id"] not in blocked]


@router.post("/pending-push/ack")
def ack_pending_push(payload: AckPendingPushRequest, uid: str = Depends(get_current_uid)):
    ids = [i for i in payload.notificationIds if i][:500]
    if not ids:
        return {"success": True}
    placeholders = ",".join("?" for _ in ids)
    with get_db(write=True) as db:
        db.execute(
            f"UPDATE notifications SET pushed_to_system = 1 WHERE receiver_id = ? AND notification_id IN ({placeholders})",
            tuple([uid, *ids]),
        )
    return {"success": True}


@router.post("/read-all")
def mark_all_read(uid: str = Depends(get_current_uid)):
    with get_db(write=True) as db:
        db.execute("UPDATE notifications SET is_read = 1 WHERE receiver_id = ? AND is_read = 0", (uid,))
    return {"success": True}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, uid: str = Depends(get_current_uid)):
    with get_db(write=True) as db:
        db.execute(
            "UPDATE notifications SET is_read = 1 WHERE notification_id = ? AND receiver_id = ?",
            (notification_id, uid),
        )
    return {"success": True}

