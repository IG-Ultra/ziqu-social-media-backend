import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from ..cache import blocked_peers_cache, invalidate_user_caches
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


def _block_user_sync(uid: str, blocked_uid: str, blocked_username: str, ts: int) -> None:
    block_id = f"{uid}_{blocked_uid}"
    with get_db(write=True) as db:
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
                blocked_username or (blocked["username"] if blocked else ""),
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

    blocked_peers_cache.delete(uid)
    blocked_peers_cache.delete(blocked_uid)
    invalidate_user_caches(uid)
    invalidate_user_caches(blocked_uid)


@router.post("/api/blocks/{blocked_uid}")
async def block_user(
    blocked_uid: str,
    payload: BlockUserRequest = BlockUserRequest(),
    uid: str = Depends(get_current_uid),
):
    if not blocked_uid or blocked_uid == uid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user ID")
    ts = now_ms()
    await run_in_threadpool(_block_user_sync, uid, blocked_uid, payload.blockedUsername, ts)
    await manager.send_to_users([uid, blocked_uid], {"event": "block_updated", "blockerUid": uid, "blockedUid": blocked_uid})
    return {"success": True}


def _unblock_user_sync(uid: str, blocked_uid: str) -> None:
    with get_db(write=True) as db:
        db.execute(
            "DELETE FROM blocks WHERE blocker_uid = ? AND blocked_uid = ?",
            (uid, blocked_uid),
        )
    blocked_peers_cache.delete(uid)
    blocked_peers_cache.delete(blocked_uid)


@router.delete("/api/blocks/{blocked_uid}")
async def unblock_user(blocked_uid: str, uid: str = Depends(get_current_uid)):
    await run_in_threadpool(_unblock_user_sync, uid, blocked_uid)
    await manager.send_to_users([uid, blocked_uid], {"event": "block_updated", "blockerUid": uid, "blockedUid": blocked_uid})
    return {"success": True}


def _submit_report_sync(payload: ReportRequest, uid: str, report_id: str, ts: int):
    reason = payload.reason.strip()
    target_type = (payload.targetType or "user").strip().lower()
    if target_type not in ("user", "post", "reel", "comment", "story"):
        target_type = "user"
    target_id = (payload.targetId or payload.reportedUid or "").strip()
    reported_uid = (payload.reportedUid or "").strip()
    reported_username = (payload.reportedUsername or "").strip().lstrip("@")

    with get_db(write=True) as db:
        reporter = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()

        if target_type in ("post", "reel") and target_id:
            p = db.execute("SELECT user_id, username FROM posts WHERE post_id = ?", (target_id,)).fetchone()
            if not p:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reported post was not found.")
            reported_uid = reported_uid or (p["user_id"] or "")
            if not reported_username:
                reported_username = p["username"] or ""
        elif target_type == "comment" and target_id:
            c = db.execute("SELECT user_id, username FROM comments WHERE comment_id = ?", (target_id,)).fetchone()
            if not c:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reported comment was not found.")
            reported_uid = reported_uid or (c["user_id"] or "")
            if not reported_username:
                reported_username = c["username"] or ""
        elif target_type == "story" and target_id:
            s = db.execute("SELECT user_id, username FROM stories WHERE story_id = ?", (target_id,)).fetchone()
            if not s:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reported story was not found.")
            reported_uid = reported_uid or (s["user_id"] or "")
            if not reported_username:
                reported_username = s["username"] or ""
        elif target_type == "user":
            lookup_uid = reported_uid or target_id
            if lookup_uid:
                tu = db.execute("SELECT uid, username FROM users WHERE uid = ?", (lookup_uid,)).fetchone()
                if not tu:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reported user was not found.")
                reported_uid = tu["uid"]
                if not reported_username:
                    reported_username = tu["username"] or ""

        if reported_uid and not reported_username:
            tu = db.execute("SELECT username FROM users WHERE uid = ?", (reported_uid,)).fetchone()
            if tu:
                reported_username = tu["username"] or ""

        final_target_id = target_id or reported_uid
        if not reported_uid and not final_target_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing report target.")
        if reported_uid and reported_uid == uid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot report your own account or content.",
            )

        existing = db.execute(
            """
            SELECT report_id FROM reports
            WHERE reporter_uid = ? AND target_type = ? AND target_id = ? AND status = 'pending'
            LIMIT 1
            """,
            (uid, target_type, final_target_id),
        ).fetchone()

        is_duplicate = False
        effective_report_id = report_id
        if existing:
            is_duplicate = True
            effective_report_id = existing["report_id"]
            db.execute(
                """
                UPDATE reports
                SET reason = ?, details = ?, timestamp = ?
                WHERE report_id = ?
                """,
                (reason, payload.details.strip(), ts, effective_report_id),
            )
            admin_uids: List[str] = []
        else:
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
                    reported_uid,
                    reported_username,
                    reason,
                    payload.details.strip(),
                    target_type,
                    final_target_id,
                    ts,
                ),
            )
            admin_rows = db.execute("SELECT uid FROM users WHERE is_admin = 1").fetchall()
            admin_uids = [r["uid"] for r in admin_rows if r["uid"]]

    return effective_report_id, is_duplicate, target_type, reported_uid, reported_username, admin_uids


@router.post("/api/reports")
async def submit_report(payload: ReportRequest, uid: str = Depends(get_current_uid)):
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Please select a report reason.")

    report_id = str(uuid.uuid4())
    ts = now_ms()
    (
        effective_report_id,
        is_duplicate,
        target_type,
        reported_uid,
        reported_username,
        admin_uids,
    ) = await run_in_threadpool(_submit_report_sync, payload, uid, report_id, ts)

    if payload.alsoBlock and reported_uid and reported_uid != uid:
        await block_user(
            blocked_uid=reported_uid,
            payload=BlockUserRequest(blockedUsername=reported_username),
            uid=uid,
        )

    if admin_uids and not is_duplicate:
        await manager.send_to_users(
            admin_uids,
            {
                "event": "report_created",
                "reportId": effective_report_id,
                "targetType": target_type,
                "reportedUid": reported_uid,
            },
        )

    return {"success": True, "reportId": effective_report_id, "duplicate": is_duplicate}


