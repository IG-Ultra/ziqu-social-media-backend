import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

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
            """,
            (uid, uid),
        ).fetchall()

        blocked_rows = db.execute(
            "SELECT blocker_uid, blocked_uid FROM blocks WHERE blocker_uid = ? OR blocked_uid = ?",
            (uid, uid),
        ).fetchall()
        blocked_set = set()
        for b in blocked_rows:
            if b["blocker_uid"] == uid:
                blocked_set.add(b["blocked_uid"])
            else:
                blocked_set.add(b["blocker_uid"])

        result = []
        for c in convs:
            other_uid = c["user2_uid"] if c["user1_uid"] == uid else c["user1_uid"]
            if not other_uid or other_uid == uid or other_uid in blocked_set:
                continue
            other = db.execute(
                "SELECT username, profile_image FROM users WHERE uid = ?",
                (other_uid,),
            ).fetchone()
            unread_row = db.execute(
                """
                SELECT COUNT(*) AS cnt FROM chat_messages
                WHERE conversation_id = ? AND receiver_id = ? AND seen = 0
                """,
                (c["conversation_id"], uid),
            ).fetchone()

            result.append(
                {
                    "conversationId": c["conversation_id"],
                    "participants": {uid: True, other_uid: True},
                    "otherUserId": other_uid,
                    "otherUsername": other["username"] if other else "",
                    "otherProfileImage": resolve_url(other["profile_image"] or "", request) if other else "",
                    "lastMessage": c["last_message"] or "",
                    "lastMessageTime": int(c["last_message_time"] or 0),
                    "unreadCount": int(unread_row["cnt"] or 0) if unread_row else 0,
                }
            )
    return result


@router.get("/api/chats/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    request: Request,
    uid: str = Depends(get_current_uid),
):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY timestamp ASC",
            (conversation_id,),
        ).fetchall()
    messages = []
    for r in rows:
        msg = row_to_chat_message(r, request)
        if not msg["deletedFor"].get(uid, False):
            messages.append(msg)
    return messages


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

    ts = now_ms()
    msg_id = str(uuid.uuid4())
    text = (message_text or "").strip()
    media = (media_url or "").strip()
    msg_type = (media_type or "text").strip() if media else "text"

    if not text and not media:
        raise ValueError("Message cannot be empty")

    with get_db() as db:
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

        nid = str(uuid.uuid4())
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
        msg_dict = row_to_chat_message(msg_row, request, base_url)

    await manager.send_to_user(
        uid,
        {
            "event": "chat_message",
            "conversationId": conv_id,
            "message": msg_dict,
        },
    )
    await manager.send_to_user(
        receiver_id,
        {
            "event": "chat_message",
            "conversationId": conv_id,
            "notificationId": nid,
            "message": msg_dict,
        },
    )
    return msg_dict


async def mark_chat_message_seen_and_notify(uid: str, conversation_id: str, message_id: str) -> None:
    sender_id = ""
    with get_db() as db:
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
    if sender_id:
        await manager.send_to_users(
            [uid, sender_id],
            {"event": "message_seen", "conversationId": conversation_id, "messageId": message_id},
        )


async def toggle_chat_message_reaction_and_notify(
    uid: str,
    conversation_id: str,
    message_id: str,
    emoji: str,
) -> Dict[str, Any]:
    peers: List[str] = [uid]
    with get_db() as db:
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
    with get_db() as db:
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


@router.delete("/api/chats/{conversation_id}/messages/{message_id}")
async def delete_chat_message(
    conversation_id: str,
    message_id: str,
    forEveryone: bool = Query(default=False),
    uid: str = Depends(get_current_uid),
):
    peers: List[str] = [uid]
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? AND message_id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if not row:
            return {"success": True}
        peers = [row["sender_id"], row["receiver_id"]]
        if forEveryone:
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

    with get_db() as db:
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
        for m_uid in all_members:
            db.execute(
                "INSERT OR IGNORE INTO group_members(group_id, uid, is_admin, joined_at) VALUES (?, ?, ?, ?)",
                (gid, m_uid, 1 if m_uid == uid else 0, ts),
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
        g_dict = build_group_dict(db, g_row, request)

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
        return [build_group_dict(db, r, request) for r in rows]


@router.get("/api/groups/{group_id}")
def get_group_details(group_id: str, request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        row = db.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
        return build_group_dict(db, row, request)


@router.get("/api/groups/{group_id}/messages")
def list_group_messages(group_id: str, request: Request, uid: str = Depends(get_current_uid)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM group_messages WHERE group_id = ? ORDER BY timestamp ASC",
            (group_id,),
        ).fetchall()
    msgs = []
    for r in rows:
        m = row_to_chat_message(r, request)
        if not m["deletedFor"].get(uid, False):
            msgs.append(m)
    return msgs


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
    ts = now_ms()
    msg_id = str(uuid.uuid4())
    text = (message_text or "").strip()
    media = (media_url or "").strip()
    msg_type = (media_type or "text").strip() if media else "text"

    if not text and not media:
        raise ValueError("Group message cannot be empty")

    with get_db() as db:
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
        for member_id in members:
            if member_id != uid:
                nid = str(uuid.uuid4())
                member_nids[member_id] = nid
                db.execute(
                    """
                    INSERT INTO notifications(
                        notification_id, receiver_id, sender_id, sender_username, sender_profile_image,
                        type, group_id, title, text, timestamp, is_read, pushed_to_system
                    ) VALUES (?, ?, ?, ?, ?, 'group_chat', ?, ?, ?, ?, 0, 0)
                    """,
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
                    ),
                )

        row = db.execute("SELECT * FROM group_messages WHERE message_id = ?", (msg_id,)).fetchone()
        msg_dict = row_to_chat_message(row, request, base_url)
        group_name = group["name"] or "Group Chat"
        group_image = resolve_url(group["group_image"] or "", request, base_url)

    for member_id in set(members):
        if member_id:
            await manager.send_to_user(
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
    return msg_dict


@router.post("/api/groups/{group_id}/read")
def mark_group_read(group_id: str, uid: str = Depends(get_current_uid)):
    with get_db() as db:
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


@router.delete("/api/groups/{group_id}/messages/{message_id}")
async def delete_group_message(
    group_id: str,
    message_id: str,
    forEveryone: bool = Query(default=False),
    uid: str = Depends(get_current_uid),
):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM group_messages WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if not row:
            return {"success": True}
        members = _group_member_uids(db, group_id)
        if forEveryone:
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
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


@router.post("/api/groups/{group_id}/messages/{message_id}/reaction")
async def toggle_group_reaction(
    group_id: str,
    message_id: str,
    payload: ReactionRequest,
    uid: str = Depends(get_current_uid),
):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM group_messages WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        reactions = _safe_dict(row["reactions_json"])
        if reactions.get(uid) == payload.emoji:
            reactions.pop(uid, None)
        else:
            reactions[uid] = payload.emoji
        db.execute(
            "UPDATE group_messages SET reactions_json = ? WHERE message_id = ?",
            (json.dumps(reactions), message_id),
        )
        members = _group_member_uids(db, group_id)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"reactions": reactions}


@router.post("/api/groups/{group_id}/members")
async def add_group_members(
    group_id: str,
    payload: AddGroupMembersRequest,
    uid: str = Depends(get_current_uid),
):
    ts = now_ms()
    with get_db() as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        added_names = []
        for m_uid in payload.memberUids:
            u = db.execute("SELECT username FROM users WHERE uid = ?", (m_uid,)).fetchone()
            if u:
                db.execute(
                    "INSERT OR IGNORE INTO group_members(group_id, uid, is_admin, joined_at) VALUES (?, ?, 0, ?)",
                    (group_id, m_uid, ts),
                )
                added_names.append(f"@{u['username']}")
        if added_names:
            actor_name = actor["username"] if actor else ""
            sys_text = (
                f"@{actor_name} added {', '.join(added_names)}"
                if actor_name
                else f"Added {', '.join(added_names)} to the group"
            )
            _write_group_system_message(db, group_id, sys_text, ts)
        members = _group_member_uids(db, group_id)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


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
    with get_db() as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute("UPDATE groups SET name = ? WHERE group_id = ?", (new_name, group_id))
        uname = actor["username"] if actor else "Someone"
        _write_group_system_message(db, group_id, f"@{uname} changed group name to \"{new_name}\"", ts)
        members = _group_member_uids(db, group_id)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


@router.put("/api/groups/{group_id}/image")
async def update_group_image(
    group_id: str,
    payload: UpdateGroupImageRequest,
    uid: str = Depends(get_current_uid),
):
    ts = now_ms()
    with get_db() as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute("UPDATE groups SET group_image = ? WHERE group_id = ?", (payload.groupImage.strip(), group_id))
        uname = actor["username"] if actor else "Someone"
        _write_group_system_message(db, group_id, f"@{uname} updated group photo", ts)
        members = _group_member_uids(db, group_id)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True, "groupImage": payload.groupImage.strip()}


@router.post("/api/groups/{group_id}/admins/{member_id}")
async def promote_admin(group_id: str, member_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    with get_db() as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        target = db.execute("SELECT username FROM users WHERE uid = ?", (member_id,)).fetchone()
        db.execute(
            "UPDATE group_members SET is_admin = 1 WHERE group_id = ? AND uid = ?",
            (group_id, member_id),
        )
        a_name = actor["username"] if actor else "Admin"
        t_name = target["username"] if target else "member"
        _write_group_system_message(db, group_id, f"@{a_name} made @{t_name} a group admin", ts)
        members = _group_member_uids(db, group_id)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


@router.delete("/api/groups/{group_id}/admins/{member_id}")
async def dismiss_admin(group_id: str, member_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    with get_db() as db:
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        target = db.execute("SELECT username FROM users WHERE uid = ?", (member_id,)).fetchone()
        db.execute(
            "UPDATE group_members SET is_admin = 0 WHERE group_id = ? AND uid = ?",
            (group_id, member_id),
        )
        a_name = actor["username"] if actor else "Admin"
        t_name = target["username"] if target else "member"
        _write_group_system_message(db, group_id, f"@{a_name} dismissed @{t_name} as admin", ts)
        members = _group_member_uids(db, group_id)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


@router.delete("/api/groups/{group_id}/members/{member_id}")
async def remove_member(group_id: str, member_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    with get_db() as db:
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
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}


@router.post("/api/groups/{group_id}/leave")
async def leave_group(group_id: str, uid: str = Depends(get_current_uid)):
    ts = now_ms()
    with get_db() as db:
        members = _group_member_uids(db, group_id)
        actor = db.execute("SELECT username FROM users WHERE uid = ?", (uid,)).fetchone()
        db.execute("DELETE FROM group_members WHERE group_id = ? AND uid = ?", (group_id, uid))
        if actor:
            _write_group_system_message(db, group_id, f"@{actor['username']} left the group", ts)
    await manager.send_to_users(members, {"event": "group_updated", "groupId": group_id})
    return {"success": True}
