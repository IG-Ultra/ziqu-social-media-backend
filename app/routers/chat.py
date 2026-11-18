import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from ..database import get_db
from ..realtime import manager
from ..schemas import (
    AddGroupMembersRequest,
    CreateGroupRequest,
    ReactionRequest,
    SendChatMessageRequest,
    SendGroupMessageRequest,
    TypingRequest,
    UpdateGroupImageRequest,
    UpdateGroupNameRequest,
)
from ..security import get_current_uid
from ..utils import (
    build_group_dict,
    build_groups_batch,
    get_blocked_peer_set_cached,
    get_request_base_url,
    is_pair_blocked,
    now_ms,
    resolve_url,
    row_to_chat_message,
)

router = APIRouter(tags=["Chat & Groups"])


def _safe_dict(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ==================== 1-ON-1 CHAT ====================


@router.get("/api/chats")
def list_conversations(request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        convs = db.execute(
            """
            SELECT * FROM conversations
            WHERE user1_uid = ? OR user2_uid = ?
            ORDER BY last_message_time DESC
            LIMIT 200
            """,
            (uid, uid),
        ).fetchall()

        if not convs:
            return []

        blocked_set = get_blocked_peer_set_cached(uid, db)

        visible_convs = []
        other_uids = []
        conv_ids = []
        for c in convs:
            other_uid = c["user2_uid"] if c["user1_uid"] == uid else c["user1_uid"]
            if not other_uid or other_uid == uid or other_uid in blocked_set:
                continue
            visible_convs.append((c, other_uid))
            other_uids.append(other_uid)
            conv_ids.append(c["conversation_id"])

        if not visible_convs:
            return []

        users_map: Dict[str, Any] = {}
        unique_others = list(dict.fromkeys(other_uids))
        for i in range(0, len(unique_others), 500):
            chunk = unique_others[i : i + 500]
            ph = ",".join("?" for _ in chunk)
            for urow in db.execute(
                f"SELECT uid, username, profile_image FROM users WHERE uid IN ({ph})",
                tuple(chunk),
            ).fetchall():
                users_map[urow["uid"]] = urow

        unread_map: Dict[str, int] = {}
        for i in range(0, len(conv_ids), 500):
            chunk = conv_ids[i : i + 500]
            ph = ",".join("?" for _ in chunk)
            for ucnt in db.execute(
                f"""
                SELECT conversation_id, COUNT(*) AS cnt
                FROM chat_messages
                WHERE receiver_id = ? AND seen = 0 AND conversation_id IN ({ph})
                GROUP BY conversation_id
                """,
                tuple([uid, *chunk]),
            ).fetchall():
                unread_map[ucnt["conversation_id"]] = int(ucnt["cnt"] or 0)

        result = []
        for c, other_uid in visible_convs:
            other = users_map.get(other_uid)
            cid = c["conversation_id"]
            result.append(
                {
                    "conversationId": cid,
                    "participants": {uid: True, other_uid: True},
                    "otherUserId": other_uid,
                    "otherUsername": other["username"] if other else "",
                    "otherProfileImage": resolve_url(other["profile_image"] or "", request) if other else "",
                    "lastMessage": c["last_message"] or "",
                    "lastMessageTime": int(c["last_message_time"] or 0),
                    "unreadCount": unread_map.get(cid, 0),
                }
            )
    return result


@router.get("/api/chats/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    request: Request,
    limit: int = Query(default=300, ge=1, le=1000),
    uid: str = Depends(get_current_uid),
):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM (
                SELECT * FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ) sub
            ORDER BY timestamp ASC
            """,
            (conversation_id, limit),
        ).fetchall()
    messages = []
    for r in rows:
        msg = row_to_chat_message(r, request)
        if not msg["deletedFor"].get(uid, False):
            messages.append(msg)
    return messages


def _persist_chat_message_sync(
    uid: str,
    conversation_id: str,
    receiver_id: str,
    text: str,
    media: str,
    msg_type: str,
    forwarded: bool,
    reply_to_message_id: str,
    reply_to_sender_username: str,
    reply_to_text: str,
    effective_base_url: Optional[str],
):
    ts = now_ms()
    msg_id = str(uuid.uuid4())
    nid = str(uuid.uuid4())

    with get_db(write=True) as db:
        if is_pair_blocked(db, uid, receiver_id):
            raise PermissionError("You cannot message this user")

        sender = db.execute("SELECT username, profile_image FROM users WHERE uid = ?", (uid,)).fetchone()
        if not sender:
            raise LookupError("Sender not found")

        u1, u2 = (uid, receiver_id) if uid < receiver_id else (receiver_id, uid)
        conv_id = conversation_id.strip() if conversation_id else f"{u1}_{u2}"
        last_msg_summary = text if text else "Media"

        db.execute(
            """
            INSERT INTO conversations(conversation_id, user1_uid, user2_uid, last_message, last_message_time)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                last_message=excluded.last_message,
                last_message_time=excluded.last_message_time
            """,
            (conv_id, u1, u2, last_msg_summary, ts),
        )

        db.execute(
            """
            INSERT INTO chat_messages(
                message_id, conversation_id, sender_id, receiver_id,
                sender_username, sender_profile_image, message, media_url,
                timestamp, type, seen, deleted, deleted_for_json, reactions_json,
                forwarded, reply_to_message_id, reply_to_sender_username, reply_to_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '{}', '{}', ?, ?, ?, ?)
            """,
            (
                msg_id,
                conv_id,
                uid,
                receiver_id,
                sender["username"],
                sender["profile_image"] or "",
                text,
                media,
                ts,
                msg_type,
                1 if forwarded else 0,
                reply_to_message_id or "",
                reply_to_sender_username or "",
                reply_to_text or "",
            ),
        )

        notif_text = text if text else "Sent a photo"
        db.execute(
            """
            INSERT INTO notifications(
                notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                type, text, conversation_id, timestamp, is_read, pushed_to_system
            ) VALUES (?, ?, ?, ?, ?, 'chat', ?, ?, ?, 0, 0)
            """,
            (
                nid,
                receiver_id,
                uid,
                sender["username"],
                sender["profile_image"] or "",
                notif_text,
                conv_id,
                ts,
            ),
        )

        msg_row = db.execute("SELECT * FROM chat_messages WHERE message_id = ?", (msg_id,)).fetchone()
        msg_dict = row_to_chat_message(msg_row, None, effective_base_url)

    return conv_id, nid, msg_dict


async def persist_and_broadcast_chat_message(
    uid: str,
    conversation_id: str,
    receiver_id: str,
    message_text: str = "",
    media_url: str = "",
    media_type: str = "text",
    forwarded: bool = False,
    reply_to_message_id: str = "",
    reply_to_sender_username: str = "",
    reply_to_text: str = "",
    request: Optional[Request] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    receiver_id = receiver_id.strip()
    if not receiver_id:
        raise ValueError("receiverId is required")

    text = (message_text or "").strip()
    media = (media_url or "").strip()
    msg_type = (media_type or "text").strip() if media else "text"

    if not text and not media:
        raise ValueError("Message cannot be empty")

    effective_base_url = base_url or (get_request_base_url(request) if request is not None else "")
    conv_id, nid, msg_dict = await run_in_threadpool(
        _persist_chat_message_sync,
        uid,
        conversation_id,
        receiver_id,
        text,
        media,
        msg_type,
        forwarded,
        reply_to_message_id,
        reply_to_sender_username,
        reply_to_text,
        effective_base_url,
    )

    await asyncio.gather(
        manager.send_to_user(
            uid,
            {
                "event": "chat_message",
                "conversationId": conv_id,
                "message": msg_dict,
            },
        ),
        manager.send_to_user(
            receiver_id,
            {
                "event": "chat_message",
                "conversationId": conv_id,
                "notificationId": nid,
                "message": msg_dict,
            },
        ),
        return_exceptions=True,
    )
    return msg_dict


def _mark_chat_message_seen_sync(uid: str, conversation_id: str, message_id: str) -> str:
    sender_id = ""
    with get_db(write=True) as db:
        row = db.execute(
            "SELECT sender_id, receiver_id FROM chat_messages WHERE conversation_id = ? AND message_id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if row and row["receiver_id"] == uid:
            sender_id = row["sender_id"]
            db.execute(
                "UPDATE chat_messages SET seen = 1 WHERE conversation_id = ? AND message_id = ?",
                (conversation_id, message_id),
            )
            db.execute(
                """
                UPDATE notifications SET is_read = 1, pushed_to_system = 1
                WHERE receiver_id = ? AND conversation_id = ? AND type = 'chat'
                """,
                (uid, conversation_id),
            )
    return sender_id


async def mark_chat_message_seen_and_notify(uid: str, conversation_id: str, message_id: str) -> None:
    sender_id = await run_in_threadpool(_mark_chat_message_seen_sync, uid, conversation_id, message_id)
    if sender_id:
        await manager.send_to_users(
            [uid, sender_id],
            {"event": "message_seen", "conversationId": conversation_id, "messageId": message_id},
        )


def _toggle_chat_reaction_sync(uid: str, conversation_id: str, message_id: str, emoji: str):
    with get_db(write=True) as db:
        row = db.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? AND message_id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if not row:
            raise LookupError("Message not found")
        peers = [row["sender_id"], row["receiver_id"]]
        reactions = _safe_dict(row["reactions_json"])
        if reactions.get(uid) == emoji:
            reactions.pop(uid, None)
        else:
            reactions[uid] = emoji
        db.execute(
            "UPDATE chat_messages SET reactions_json = ? WHERE message_id = ?",
            (json.dumps(reactions), message_id),
        )
    return peers, reactions


async def toggle_chat_message_reaction_and_notify(
    uid: str,
    conversation_id: str,
    message_id: str,
    emoji: str,
) -> Dict[str, Any]:
    peers, reactions = await run_in_threadpool(
        _toggle_chat_reaction_sync, uid, conversation_id, message_id, emoji
    )
    await manager.send_to_users(
        peers,
        {"event": "chat_updated", "conversationId": conversation_id, "messageId": message_id},
    )
    return reactions


async def broadcast_typing_indicator(uid: str, conversation_id: str, is_typing: bool) -> None:
    parts = conversation_id.split("_")
    targets = [p for p in parts if p and p != uid]
    await manager.send_to_users(
        targets,
        {
            "event": "typing",
            "conversationId": conversation_id,
            "userId": uid,
            "isTyping": bool(is_typing),
        },
    )


@router.post("/api/chats/{conversation_id}/messages")
async def send_chat_message(
    conversation_id: str,
    payload: SendChatMessageRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    try:
        return await persist_and_broadcast_chat_message(
            uid=uid,
            conversation_id=conversation_id,
            receiver_id=payload.receiverId,
            message_text=payload.messageText,
            media_url=payload.mediaUrl,
            media_type=payload.mediaType,
            forwarded=payload.forwarded,
            reply_to_message_id=payload.replyToMessageId,
            reply_to_sender_username=payload.replyToSenderUsername,
            reply_to_text=payload.replyToText,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/api/chats/{conversation_id}/messages/{message_id}/seen")
async def mark_message_seen(
    conversation_id: str,
    message_id: str,
    uid: str = Depends(get_current_uid),
):
    await mark_chat_message_seen_and_notify(uid, conversation_id, message_id)
    return {"success": True}


@router.post("/api/chats/{conversation_id}/read")
def mark_conversation_read(conversation_id: str, uid: str = Depends(get_current_uid)):
    with get_db(write=True) as db:
        db.execute(
            "UPDATE chat_messages SET seen = 1 WHERE conversation_id = ? AND receiver_id = ? AND seen = 0",
            (conversation_id, uid),
        )
        db.execute(
            """
            UPDATE notifications SET is_read = 1, pushed_to_system = 1
            WHERE receiver_id = ? AND conversation_id = ? AND type = 'chat'
            """,
            (uid, conversation_id),
        )
    return {"success": True}


def _delete_chat_message_sync(conversation_id: str, message_id: str, for_everyone: bool, uid: str) -> Optional[List[str]]:
    with get_db(write=True) as db:
        row = db.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? AND message_id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if not row:
            return None
        peers = [row["sender_id"], row["receiver_id"]]
        if for_everyone:
            if row["sender_id"] != uid:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only sender can unsend")
            db.execute("DELETE FROM chat_messages WHERE message_id = ?", (message_id,))
        else:
            deleted_for = _safe_dict(row["deleted_for_json"])
            deleted_for[uid] = True
            db.execute(
                "UPDATE chat_messages SET deleted_for_json = ? WHERE message_id = ?",
                (json.dumps(deleted_for), message_id),
            )
    return peers


@router.delete("/api/chats/{conversation_id}/messages/{message_id}")
async def delete_chat_message(
    conversation_id: str,
    message_id: str,
    forEveryone: bool = Query(default=False),
    uid: str = Depends(get_current_uid),
):
    peers = await run_in_threadpool(_delete_chat_message_sync, conversation_id, message_id, forEveryone, uid)
    if peers:
        await manager.send_to_users(
            peers,
            {"event": "chat_updated", "conversationId": conversation_id, "messageId": message_id},
        )
    return {"success": True}


@router.post("/api/chats/{conversation_id}/messages/{message_id}/reaction")
async def toggle_chat_reaction(
    conversation_id: str,
    message_id: str,
    payload: ReactionRequest,
    uid: str = Depends(get_current_uid),
):
    try:
        reactions = await toggle_chat_message_reaction_and_notify(
            uid=uid,
            conversation_id=conversation_id,
            message_id=message_id,
            emoji=payload.emoji,
        )
        return {"reactions": reactions}
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/api/chats/{conversation_id}/typing")
async def update_typing(
    conversation_id: str,
    payload: TypingRequest,
    uid: str = Depends(get_current_uid),
):
    await broadcast_typing_indicator(uid, conversation_id, payload.isTyping)
    return {"success": True}


# ==================== GROUP CHAT ====================


def _group_member_uids(db, group_id: str) -> List[str]:
    rows = db.execute("SELECT uid FROM group_members WHERE group_id = ?", (group_id,)).fetchall()
    return [r["uid"] for r in rows]


def _write_group_system_message(db, group_id: str, text: str, ts: int) -> None:
    mid = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO group_messages(
            message_id, group_id, sender_id, receiver_id, sender_username,
            sender_profile_image, message, media_url, timestamp, type
        ) VALUES (?, ?, 'system', ?, 'ZIQU', '', ?, '', ?, 'text')
        """,
        (mid, group_id, group_id, text, ts),
    )
    db.execute(
        "UPDATE groups SET last_message = ?, last_message_time = ? WHERE group_id = ?",
        (text, ts, group_id),
    )


def _create_group_sync(gid: str, name: str, payload: CreateGroupRequest, uid: str, all_members: List[str], ts: int, base_url: str):
    with get_db(write=True) as db:
        admin = db.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        if not admin:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found")

        init_text = f"@{admin['username']} created the group \"{name}\""
        db.execute(
            """
            INSERT INTO groups(
                group_id, name, description, group_image, admin_id, admin_username,
                last_message, last_message_sender_id, last_message_sender_name, last_message_time, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gid,
                name,
                payload.description.strip(),
                payload.groupImage.strip(),
                uid,
                admin["username"],
                init_text,
                uid,
                admin["username"],
                ts,
                ts,
            ),
        )
        db.executemany(
            "INSERT OR IGNORE INTO group_members(group_id, uid, is_admin, joined_at) VALUES (?, ?, ?, ?)",
            [(gid, m_uid, 1 if m_uid == uid else 0, ts) for m_uid in all_members],
        )

        msg_id = str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO group_messages(
                message_id, group_id, sender_id, receiver_id, sender_username,
                sender_profile_image, message, media_url, timestamp, type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, 'text')
            """,
            (msg_id, gid, uid, gid, admin["username"], admin["profile_image"] or "", init_text, ts),
        )
        g_row = db.execute("SELECT * FROM groups WHERE group_id = ?", (gid,)).fetchone()
        g_dict = build_group_dict(db, g_row)
        if base_url:
            if (g_dict.get("groupImage") or "").startswith("/"):
                g_dict["groupImage"] = f"{base_url}{g_dict['groupImage']}"
            for k_uid, p_url in list(g_dict.get("memberProfiles", {}).items()):
                if (p_url or "").startswith("/"):
                    g_dict["memberProfiles"][k_uid] = f"{base_url}{p_url}"
    return g_dict


@router.post("/api/groups")
async def create_group(
    payload: CreateGroupRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Group name cannot be empty")
    gid = str(uuid.uuid4())
    ts = now_ms()
    all_members = list({uid, *[m for m in payload.memberUids if m]})
    base_url = get_request_base_url(request)

    g_dict = await run_in_threadpool(_create_group_sync, gid, name, payload, uid, all_members, ts, base_url)
    await manager.send_to_users(all_members, {"event": "group_updated", "groupId": gid})
    return g_dict


@router.get("/api/groups")
def list_user_groups(request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT g.* FROM groups g
            JOIN group_members gm ON gm.group_id = g.group_id
            WHERE gm.uid = ?
            ORDER BY g.last_message_time DESC
            """,
            (uid,),
        ).fetchall()
        return build_groups_batch(db, rows, request)


@router.get("/api/groups/{group_id}")
def get_group_details(group_id: str, request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        return build_group_dict(db, row, request)


@router.get("/api/groups/{group_id}/messages")
def list_group_messages(
    group_id: str,
    request: Request,
    limit: int = Query(default=300, ge=1, le=1000),
    uid: str = Depends(get_current_uid),
):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM (
                SELECT * FROM group_messages
                WHERE group_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ) sub
            ORDER BY timestamp ASC
            """,
            (group_id, limit),
        ).fetchall()
    msgs = []
    for r in rows:
        m = row_to_chat_message(r, request)
        if not m["deletedFor"].get(uid, False):
            msgs.append(m)
    return msgs


def _persist_group_message_sync(
    uid: str,
    group_id: str,
    text: str,
    media: str,
    msg_type: str,
    reply_to_message_id: str,
    reply_to_sender_username: str,
    reply_to_text: str,
    effective_base_url: Optional[str],
):
    ts = now_ms()
    msg_id = str(uuid.uuid4())

    with get_db(write=True) as db:
        group = db.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if not group:
            raise LookupError("Group not found")
        sender = db.execute("SELECT username, profile_image FROM users WHERE uid = ?", (uid,)).fetchone()
        if not sender:
            raise LookupError("Sender not found")

        db.execute(
            """
            INSERT INTO group_messages(
                message_id, group_id, sender_id, receiver_id, sender_username,
                sender_profile_image, message, media_url, timestamp, type,
                reply_to_message_id, reply_to_sender_username, reply_to_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                group_id,
                uid,
                group_id,
                sender["username"],
                sender["profile_image"] or "",
                text,
                media,
                ts,
                msg_type,
                reply_to_message_id or "",
                reply_to_sender_username or "",
                reply_to_text or "",
            ),
        )

        last_summary = text if text else ("Video" if msg_type == "video" else "Photo")
        db.execute(
            """
            UPDATE groups
            SET last_message = ?, last_message_sender_id = ?,
                last_message_sender_name = ?, last_message_time = ?
            WHERE group_id = ?
            """,
            (f"{sender['username']}: {last_summary}", uid, sender["username"], ts, group_id),
        )

        members = _group_member_uids(db, group_id)
        member_nids: Dict[str, str] = {}
        notif_rows = []
        for member_id in members:
            if member_id != uid:
                nid = str(uuid.uuid4())
                member_nids[member_id] = nid
                notif_rows.append(
                    (
                        nid,
                        member_id,
                        uid,
                        sender["username"],
                        sender["profile_image"] or "",
                        group_id,
                        group["name"],
                        f"{sender['username']}: {last_summary}",
                        ts,
                    )
                )
        if notif_rows:
            db.executemany(
                """
                INSERT INTO notifications(
                    notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                    type, group_id, title, text, timestamp, is_read, pushed_to_system
                ) VALUES (?, ?, ?, ?, ?, 'group_chat', ?, ?, ?, ?, 0, 0)
                """,
                notif_rows,
            )

        row = db.execute("SELECT * FROM group_messages WHERE message_id = ?", (msg_id,)).fetchone()
        msg_dict = row_to_chat_message(row, None, effective_base_url)
        group_name = group["name"] or "Group Chat"
        group_image = resolve_url(group["group_image"] or "", None, effective_base_url)

    return members, member_nids, group_name, group_image, msg_dict


async def persist_and_broadcast_group_message(
    uid: str,
    group_id: str,
    message_text: str = "",
    media_url: str = "",
    media_type: str = "text",
    reply_to_message_id: str = "",
    reply_to_sender_username: str = "",
    reply_to_text: str = "",
    request: Optional[Request] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    text = (message_text or "").strip()
    media = (media_url or "").strip()
    msg_type = (media_type or "text").strip() if media else "text"

    if not text and not media:
        raise ValueError("Group message cannot be empty")

    effective_base_url = base_url or (get_request_base_url(request) if request is not None else "")
    members, member_nids, group_name, group_image, msg_dict = await run_in_threadpool(
        _persist_group_message_sync,
        uid,
        group_id,
        text,
        media,
        msg_type,
        reply_to_message_id,
        reply_to_sender_username,
        reply_to_text,
        effective_base_url,
    )

    unique_members = [m for m in set(members) if m]
    if unique_members:
        await asyncio.gather(
            *(
                manager.send_to_user(
                    member_id,
                    {
                        "event": "group_message",
                        "groupId": group_id,
                        "groupName": group_name,
                        "groupImage": group_image,
                        "notificationId": member_nids.get(member_id, ""),
                        "message": msg_dict,
                    },
                )
                for member_id in unique_members
            ),
            return_exceptions=True,
        )
    return msg_dict


@router.post("/api/groups/{group_id}/read")
def mark_group_read(group_id: str, uid: str = Depends(get_current_uid)):
    with get_db(write=True) as db:
        db.execute(
            """
            UPDATE notifications SET is_read = 1, pushed_to_system = 1
            WHERE receiver_id = ? AND group_id = ? AND type = 'group_chat'
            """,
            (uid, group_id),
        )
    return {"success": True}


@router.post("/api/groups/{group_id}/messages")
async def send_group_message(
    group_id: str,
    payload: SendGroupMessageRequest,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    try:
        return await persist_and_broadcast_group_message(
            uid=uid,
            group_id=group_id,
            message_text=payload.messageText,
            media_url=payload.mediaUrl,
            media_type=payload.mediaType,
            reply_to_message_id=payload.replyToMessageId,
            reply_to_sender_username=payload.replyToSenderUsername,
            reply_to_text=payload.replyToText,
            request=request,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


def _delete_group_message_sync(group_id: str, message_id: str, for_everyone: bool, uid: str) -> Optional[List[str]]:
    with get_db(write=True) as db:
        row = db.execute(
            "SELECT * FROM group_messages WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if not row:
            return None
        members = _group_member_uids(db, group_id)
        if for_everyone:
            if row["sender_id"] != uid:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only sender can unsend")
            db.execute("DELETE FROM group_messages WHERE message_id = ?", (message_id,))
        else:
            deleted_for = _safe_dict(row["deleted_for_json"])
            deleted_for[uid] = True
            db.execute(
                "UPDATE group_messages SET deleted_for_json = ? WHERE message_id = ?",
                (json.dumps(deleted_for), message_id),
            )
    return members


@router.delete("/api/groups/{group_id}/messages/{message_id}")
async def delete_group_message(
    group_id: str,
    message_id: str,
    forEveryone: bool = Query(default=False),
    uid: str = Depends(get_current_uid),
):
    members = await run_in_threadpool(_delete_group_message_sync, group_id, message_id, forEveryone, uid)
    if members:
        await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


def _toggle_group_reaction_sync(group_id: str, message_id: str, emoji: str, uid: str):
    with get_db(write=True) as db:
        row = db.execute(
            "SELECT * FROM group_messages WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        reactions = _safe_dict(row["reactions_json"])
        if reactions.get(uid) == emoji:
            reactions.pop(uid, None)
        else:
            reactions[uid] = emoji
        db.execute(
            "UPDATE group_messages SET reactions_json = ? WHERE message_id = ?",
            (json.dumps(reactions), message_id),
        )
        members = _group_member_uids(db, group_id)
    return members, reactions


@router.post("/api/groups/{group_id}/messages/{message_id}/reaction")
async def toggle_group_reaction(
    group_id: str,
    message_id: str,
    payload: ReactionRequest,
    uid: str = Depends(get_current_uid),
):
    members, reactions = await run_in_threadpool(
        _toggle_group_reaction_sync, group_id, message_id, payload.emoji, uid
    )
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"reactions": reactions}


def _add_group_members_sync(group_id: str, member_uids: List[str], uid: str, ts: int) -> List[str]:
    with get_db(write=True) as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        clean_uids = [m for m in member_uids if m][:100]
        added_names = []
        if clean_uids:
            ph = ",".join("?" for _ in clean_uids)
            u_rows = db.execute(
                f"SELECT uid, username FROM users WHERE uid IN ({ph})",
                tuple(clean_uids),
            ).fetchall()
            valid_rows = [(group_id, r["uid"], ts) for r in u_rows]
            if valid_rows:
                db.executemany(
                    "INSERT OR IGNORE INTO group_members(group_id, uid, is_admin, joined_at) VALUES (?, ?, 0, ?)",
                    valid_rows,
                )
                added_names = [f"@{r['username']}" for r in u_rows]
        if added_names:
            actor_name = actor["username"] if actor else ""
            sys_text = (
                f"@{actor_name} added {', '.join(added_names)}"
                if actor_name
                else f"Added {', '.join(added_names)} to the group"
            )
            _write_group_system_message(db, group_id, sys_text, ts)
        return _group_member_uids(db, group_id)


@router.post("/api/groups/{group_id}/members")
async def add_group_members(
    group_id: str,
    payload: AddGroupMembersRequest,
    uid: str = Depends(get_current_uid),
):
    ts = now_ms()
    members = await run_in_threadpool(_add_group_members_sync, group_id, payload.memberUids, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


def _update_group_name_sync(group_id: str, new_name: str, uid: str, ts: int) -> List[str]:
    with get_db(write=True) as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute("UPDATE groups SET name = ? WHERE group_id = ?", (new_name, group_id))
        uname = actor["username"] if actor else "Someone"
        _write_group_system_message(db, group_id, f"@{uname} changed group name to \"{new_name}\"", ts)
        return _group_member_uids(db, group_id)


@router.put("/api/groups/{group_id}/name")
async def update_group_name(
    group_id: str,
    payload: UpdateGroupNameRequest,
    uid: str = Depends(get_current_uid),
):
    new_name = payload.name.strip()
    if not new_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Group name cannot be empty")
    ts = now_ms()
    members = await run_in_threadpool(_update_group_name_sync, group_id, new_name, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


def _update_group_image_sync(group_id: str, img_url: str, uid: str, ts: int) -> List[str]:
    with get_db(write=True) as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute("UPDATE groups SET group_image = ? WHERE group_id = ?", (img_url, group_id))
        uname = actor["username"] if actor else "Someone"
        _write_group_system_message(db, group_id, f"@{uname} updated group photo", ts)
        return _group_member_uids(db, group_id)


@router.put("/api/groups/{group_id}/image")
async def update_group_image(
    group_id: str,
    payload: UpdateGroupImageRequest,
    uid: str = Depends(get_current_uid),
):
    ts = now_ms()
    img_url = payload.groupImage.strip()
    members = await run_in_threadpool(_update_group_image_sync, group_id, img_url, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True, "groupImage": img_url}


def _set_group_admin_sync(group_id: str, member_id: str, is_admin_flag: int, uid: str, ts: int) -> List[str]:
    with get_db(write=True) as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        target = db.execute("SELECT username FROM users WHERE uid = ?", (member_id,)).fetchone()
        db.execute(
            "UPDATE group_members SET is_admin = ? WHERE group_id = ? AND uid = ?",
            (is_admin_flag, group_id, member_id),
        )
        a_name = actor["username"] if actor else "Admin"
        t_name = target["username"] if target else "member"
        verb = "a group admin" if is_admin_flag else "as admin"
        action_word = "made" if is_admin_flag else "dismissed"
        _write_group_system_message(db, group_id, f"@{a_name} {action_word} @{t_name} {verb}", ts)
        return _group_member_uids(db, group_id)


@router.post("/api/groups/{group_id}/admins/{member_id}")
async def promote_admin(group_id: str, member_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    members = await run_in_threadpool(_set_group_admin_sync, group_id, member_id, 1, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


@router.delete("/api/groups/{group_id}/admins/{member_id}")
async def dismiss_admin(group_id: str, member_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    members = await run_in_threadpool(_set_group_admin_sync, group_id, member_id, 0, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


def _remove_member_sync(group_id: str, member_id: str, uid: str, ts: int) -> List[str]:
    with get_db(write=True) as db:
        members = _group_member_uids(db, group_id)
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        target = db.execute("SELECT username FROM users WHERE uid = ?", (member_id,)).fetchone()
        db.execute(
            "DELETE FROM group_members WHERE group_id = ? AND uid = ?",
            (group_id, member_id),
        )
        a_name = actor["username"] if actor else "Admin"
        t_name = target["username"] if target else "member"
        _write_group_system_message(db, group_id, f"@{a_name} removed @{t_name} from the group", ts)
    return members


@router.delete("/api/groups/{group_id}/members/{member_id}")
async def remove_member(group_id: str, member_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    members = await run_in_threadpool(_remove_member_sync, group_id, member_id, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


def _leave_group_sync(group_id: str, uid: str, ts: int) -> List[str]:
    with get_db(write=True) as db:
        members = _group_member_uids(db, group_id)
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute("DELETE FROM group_members WHERE group_id = ? AND uid = ?", (group_id, uid))
        if actor:
            _write_group_system_message(db, group_id, f"@{actor['username']} left the group", ts)
    return members


@router.post("/api/groups/{group_id}/leave")
async def leave_group(group_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    members = await run_in_threadpool(_leave_group_sync, group_id, uid, ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}

