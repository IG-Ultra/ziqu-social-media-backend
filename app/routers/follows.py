import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..database import get_db
from ..realtime import manager
from ..security import get_current_uid
from ..utils import now_ms, row_to_user

router = APIRouter(prefix="/api/follows", tags=["Follows"])


@router.post("/{target_uid}")
async def toggle_follow(target_uid: str, uid: str = Depends(get_current_uid)):
    if uid == target_uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot follow yourself")

    ts = now_ms()
    notif_payload = None
    with get_db() as db:
        target = db.execute("SELECT uid FROM users WHERE uid = ?", (target_uid,)).fetchone()
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")
        actor = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        existing = db.execute(
            "SELECT 1 FROM follows WHERE follower_uid = ? AND following_uid = ?",
            (uid, target_uid),
        ).fetchone()

        if existing:
            db.execute(
                "DELETE FROM follows WHERE follower_uid = ? AND following_uid = ?",
                (uid, target_uid),
            )
            db.execute("UPDATE users SET followers_count = MAX(0, followers_count - 1) WHERE uid = ?", (target_uid,))
            db.execute("UPDATE users SET following_count = MAX(0, following_count - 1) WHERE uid = ?", (uid,))
            following = False
            ntype, ntext = "unfollow", "unfollowed you"
        else:
            db.execute(
                "INSERT INTO follows(follower_uid, following_uid, timestamp) VALUES (?, ?, ?)",
                (uid, target_uid, ts),
            )
            db.execute("UPDATE users SET followers_count = followers_count + 1 WHERE uid = ?", (target_uid,))
            db.execute("UPDATE users SET following_count = following_count + 1 WHERE uid = ?", (uid,))
            following = True
            ntype, ntext = "follow", "started following you"

        if actor:
            nid = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, text, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    nid,
                    target_uid,
                    uid,
                    actor["username"],
                    actor["profile_image"] or "",
                    ntype,
                    ntext,
                    ts,
                ),
            )
            if following:
                notif_payload = {
                    "event": "notification",
                    "notificationId": nid,
                    "type": ntype,
                    "senderUsername": actor["username"],
                    "text": ntext,
                }

    if notif_payload:
        await manager.send_to_user(target_uid, notif_payload)
    await manager.broadcast_all({"event": "follow_updated", "followerUid": uid, "targetUid": target_uid})
    return {"following": following}


@router.get("/{target_uid}/status")
def check_follow_status(target_uid: str, uid: str = Depends(get_current_uid)):
    if uid == target_uid:
        return {"following": False}
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM follows WHERE follower_uid = ? AND following_uid = ?",
            (uid, target_uid),
        ).fetchone()
    return {"following": row is not None}


@router.get("/{uid}/following-ids")
def get_following_ids(uid: str):
    with get_db() as db:
        rows = db.execute("SELECT following_uid FROM follows WHERE follower_uid = ?", (uid,)).fetchall()
    return [r["following_uid"] for r in rows]


@router.get("/{uid}/followers")
def get_followers(uid: str, request: Request):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT u.* FROM follows f
            JOIN users u ON u.uid = f.follower_uid
            WHERE f.following_uid = ?
            ORDER BY f.timestamp DESC
            """,
            (uid,),
        ).fetchall()
    return [row_to_user(r, request) for r in rows]


@router.get("/{uid}/following")
def get_following(uid: str, request: Request):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT u.* FROM follows f
            JOIN users u ON u.uid = f.following_uid
            WHERE f.follower_uid = ?
            ORDER BY f.timestamp DESC
            """,
            (uid,),
        ).fetchall()
    return [row_to_user(r, request) for r in rows]


@router.delete("/followers/{follower_uid}")
async def remove_follower(follower_uid: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        deleted = db.execute(
            "DELETE FROM follows WHERE follower_uid = ? AND following_uid = ?",
            (follower_uid, uid),
        ).rowcount
        if deleted > 0:
            db.execute("UPDATE users SET followers_count = MAX(0, followers_count - 1) WHERE uid = ?", (uid,))
            db.execute("UPDATE users SET following_count = MAX(0, following_count - 1) WHERE uid = ?", (follower_uid,))
    await manager.broadcast_all({"event": "follow_updated", "followerUid": follower_uid, "targetUid": uid})
    return {"success": True}
