import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..database import get_db
from ..realtime import manager
from ..schemas import BlockUserRequest, ReportRequest
from ..security import get_current_uid
from ..utils import get_blocked_peer_ids, is_pair_blocked, now_ms, resolve_url

router = APIRouter(tags=["Moderation & Safety"])


def _row_to_block(row, request: Request) -> dict:
    return {
        "blockId": row["block_id"],
        "blockerUid": row["blocker_uid"],
        "blockerUsername": row["blocker_username"] or "",
        "blockedUid": row["blocked_uid"],
        "blockedUsername": row["blocked_username"] or "",
        "blockedDisplayName": row["blocked_display_name"] or "",
        "blockedProfileImage": resolve_url(row["blocked_profile_image"] or "", request),
        "timestamp": int(row["timestamp"]),
    }


@router.get("/api/blocks/mine")
def list_my_blocked_accounts(request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM blocks WHERE blocker_uid = ? ORDER BY timestamp DESC",
            (uid,),
        ).fetchall()
    return [_row_to_block(r, request) for r in rows]


@router.get("/api/blocks/all-ids")
def list_all_blocked_ids(uid: str = Depends(get_current_uid)):
    with get_db() as db:
        all_ids = get_blocked_peer_ids(db, uid)
        by_me_rows = db.execute("SELECT blocked_uid FROM blocks WHERE blocker_uid = ?", (uid,)).fetchall()
        by_me = [r["blocked_uid"] for r in by_me_rows]
    return {"allBlockedIds": all_ids, "blockedByMeIds": by_me}


@router.get("/api/blocks/status/{target_uid}")
def get_block_status(target_uid: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        blocked_by_me = (
            db.execute(
                "SELECT 1 FROM blocks WHERE blocker_uid = ? AND blocked_uid = ?",
                (uid, target_uid),
            ).fetchone()
            is not None
        )
        blocked_either = is_pair_blocked(db, uid, target_uid)
    return {
        "isBlocked": blocked_either,
        "isBlockedByMe": blocked_by_me,
        "isBlockedByOther": blocked_either and not blocked_by_me,
    }


@router.post("/api/blocks/{blocked_uid}")
async def block_user(
    blocked_uid: str,
    payload: BlockUserRequest = BlockUserRequest(),
    uid: str = Depends(get_current_uid),
):
    if not blocked_uid or blocked_uid == uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user ID")
    ts = now_ms()
    block_id = f"{uid}_{blocked_uid}"

    with get_db() as db:
        blocker = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        blocked = db.execute(
            "SELECT username, display_name, profile_image FROM users WHERE uid = ?",
            (blocked_uid,),
        ).fetchone()

        db.execute(
            """
            INSERT INTO blocks(
                block_id, blocker_uid, blocker_username,
                blocked_uid, blocked_username, blocked_display_name, blocked_profile_image, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(blocker_uid, blocked_uid) DO UPDATE SET timestamp=excluded.timestamp
            """,
            (
                block_id,
                uid,
                blocker["username"] if blocker else "",
                blocked_uid,
                payload.blockedUsername or (blocked["username"] if blocked else ""),
                blocked["display_name"] if blocked else "",
                blocked["profile_image"] if blocked else "",
                ts,
            ),
        )

        # Clean up any follow relationship in either direction safely
        if db.execute(
            "DELETE FROM follows WHERE follower_uid = ? AND following_uid = ?",
            (uid, blocked_uid),
        ).rowcount > 0:
            db.execute("UPDATE users SET following_count = MAX(0, following_count - 1) WHERE uid = ?", (uid,))
            db.execute("UPDATE users SET followers_count = MAX(0, followers_count - 1) WHERE uid = ?", (blocked_uid,))

        if db.execute(
            "DELETE FROM follows WHERE follower_uid = ? AND following_uid = ?",
            (blocked_uid, uid),
        ).rowcount > 0:
            db.execute("UPDATE users SET following_count = MAX(0, following_count - 1) WHERE uid = ?", (blocked_uid,))
            db.execute("UPDATE users SET followers_count = MAX(0, followers_count - 1) WHERE uid = ?", (uid,))

    await manager.send_to_users([uid, blocked_uid], {"event": "block_updated", "blockerUid": uid, "blockedUid": blocked_uid})
    return {"success": True}


@router.delete("/api/blocks/{blocked_uid}")
async def unblock_user(blocked_uid: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        db.execute(
            "DELETE FROM blocks WHERE blocker_uid = ? AND blocked_uid = ?",
            (uid, blocked_uid),
        )
    await manager.send_to_users([uid, blocked_uid], {"event": "block_updated", "blockerUid": uid, "blockedUid": blocked_uid})
    return {"success": True}


@router.post("/api/reports")
async def submit_report(payload: ReportRequest, uid: str = Depends(get_current_uid)):
    report_id = str(uuid.uuid4())
    ts = now_ms()
    with get_db() as db:
        reporter = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute(
            """
            INSERT INTO reports(
                report_id, reporter_uid, reporter_username, reported_uid, reported_username,
                reason, details, target_type, target_id, timestamp, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                report_id,
                uid,
                reporter["username"] if reporter else "",
                payload.reportedUid,
                payload.reportedUsername,
                payload.reason,
                payload.details.strip(),
                payload.targetType,
                payload.targetId or payload.reportedUid,
                ts,
            ),
        )

    if payload.alsoBlock and payload.reportedUid and payload.reportedUid != uid:
        await block_user(
            blocked_uid=payload.reportedUid,
            payload=BlockUserRequest(blockedUsername=payload.reportedUsername),
            uid=uid,
        )

    return {"success": True, "reportId": report_id}
