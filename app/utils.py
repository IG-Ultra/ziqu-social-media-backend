import datetime
import json
import math
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Request


from .config import settings

USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9._]{3,30}$")


def _safe_float(val: Any, default: float) -> float:
    try:
        f = float(val if val is not None else default)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, int):
        return val
    if isinstance(val, datetime.datetime):
        try:
            return int(val.timestamp() * 1000)
        except Exception:
            return default
    if isinstance(val, datetime.date):
        try:
            return int(datetime.datetime(val.year, val.month, val.day).timestamp() * 1000)
        except Exception:
            return default
    try:
        return int(val)
    except Exception:
        try:
            return int(float(val))
        except Exception:
            return default


def now_ms() -> int:
    return int(time.time() * 1000)


def get_request_base_url(request: Optional[Request] = None) -> str:
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL.rstrip("/")
    if request is None:
        return ""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host", "").strip()
    if host:
        proto = forwarded_proto or request.url.scheme or "http"
        if ".onrender.com" in host and proto == "http":
            proto = "https"
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def get_websocket_base_url(headers: Any) -> str:
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL.rstrip("/")
    forwarded_proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    forwarded_host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or (headers.get("host") or "").strip()
    if not host:
        return ""
    proto = forwarded_proto or ("https" if ".onrender.com" in host else "http")
    if ".onrender.com" in host and proto == "http":
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


_LOCAL_DEV_HOSTS = {"10.0.2.2", "127.0.0.1", "localhost"}
_VIDEO_EXT_RE = re.compile(r"\.(mp4|mov|webm|mkv|m4v|3gp)(\?.*)?$", re.IGNORECASE)


def _is_local_dev_host(host: str) -> bool:
    return (host or "").split(":", 1)[0].strip().lower() in _LOCAL_DEV_HOSTS


def resolve_url(url: str, request: Optional[Request] = None, base_url: Optional[str] = None) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    effective_base = (base_url or "").rstrip("/") or get_request_base_url(request)

    if url.startswith("http://") or url.startswith("https://"):
        lower = url.lower()
        if url.startswith("http://") and (".onrender.com" in lower or "res.cloudinary.com" in lower):
            return "https://" + url[len("http://"):]
        parsed = urlparse(url)
        if _is_local_dev_host(parsed.netloc) and parsed.path.startswith("/media/") and effective_base:
            base_host = urlparse(effective_base).netloc
            if not _is_local_dev_host(base_host):
                suffix = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
                return f"{effective_base}{suffix}"
        return url
    if url.startswith("/"):
        if effective_base:
            return f"{effective_base}{url}"
    return url


def derive_video_thumbnail_url(media_url: str, thumbnail_url: str = "") -> str:
    thumb = (thumbnail_url or "").strip()
    if thumb:
        return thumb
    media = (media_url or "").strip()
    if not media:
        return ""
    lower = media.lower()
    if "res.cloudinary.com" in lower and "/video/upload/" in lower:
        base_part, sep, query_part = media.partition("?")
        replaced = _VIDEO_EXT_RE.sub(".jpg", base_part)
        if replaced != base_part:
            return f"{replaced}{sep}{query_part}" if sep else replaced
    return ""


def row_to_user(row: sqlite3.Row, request: Optional[Request] = None) -> Dict[str, Any]:
    is_ver = bool(row["is_verified"])
    keys = row.keys()
    email_val = (row["email"] or "").strip().lower()
    db_admin = bool(row["is_admin"]) if "is_admin" in keys else False
    is_admin = db_admin or (email_val in settings.ADMIN_EMAIL_SET)
    is_banned = bool(row["is_banned"]) if "is_banned" in keys else False
    return {
        "uid": row["uid"],
        "username": row["username"],
        "usernameLower": row["username_lower"],
        "email": row["email"],
        "password": "",
        "displayName": row["display_name"] or "",
        "bio": row["bio"] or "",
        "profileImage": resolve_url(row["profile_image"] or "", request),
        "createdAt": _safe_int(row["created_at"], 0),
        "followersCount": max(0, _safe_int(row["followers_count"], 0)),
        "followingCount": max(0, _safe_int(row["following_count"], 0)),
        "postsCount": max(0, _safe_int(row["posts_count"], 0)),
        "isVerified": is_ver,
        "verifiedAccount": is_ver,
        "isAdmin": is_admin,
        "isBanned": is_banned,
        "fcmToken": "",
    }


def row_to_post(row: sqlite3.Row, request: Optional[Request] = None) -> Dict[str, Any]:
    is_ver = bool(row["is_verified"])
    media_resolved = resolve_url(row["media_url"] or "", request)
    raw_thumb = resolve_url(row["thumbnail_url"] or "", request)
    thumb = derive_video_thumbnail_url(media_resolved, raw_thumb)
    return {
        "postId": row["post_id"],
        "userId": row["user_id"],
        "username": row["username"] or "",
        "userProfileImage": resolve_url(row["user_profile_image"] or "", request),
        "mediaUrl": media_resolved,
        "thumbnailUrl": thumb,
        "thumbnail": thumb,
        "thumbnailUrlSnake": thumb,
        "thumbUrl": thumb,
        "thumb": thumb,
        "videoThumbnail": thumb,
        "poster": thumb,
        "posterUrl": thumb,
        "mediaType": row["media_type"] or "image",
        "caption": row["caption"] or "",
        "mood": row["mood"] or "",
        "timestamp": _safe_int(row["timestamp"], 0),
        "likesCount": max(0, _safe_int(row["likes_count"], 0)),
        "commentsCount": max(0, _safe_int(row["comments_count"], 0)),
        "sharesCount": max(0, _safe_int(row["shares_count"], 0)),
        "savesCount": max(0, _safe_int(row["saves_count"], 0)),
        "watchTimeMs": max(0, _safe_int(row["watch_time_ms"], 0)),
        "viewsCount": max(0, _safe_int(row["views_count"], 0)),
        "completionsCount": max(0, _safe_int(row["completions_count"], 0)),
        "rewatchCount": max(0, _safe_int(row["rewatch_count"], 0)),
        "isReel": bool(row["is_reel"]),
        "isVerified": is_ver,
        "verifiedAccount": is_ver,
    }


def row_to_comment(row: sqlite3.Row, request: Optional[Request] = None) -> Dict[str, Any]:
    is_ver = bool(row["is_verified"])
    return {
        "commentId": row["comment_id"],
        "postId": row["post_id"],
        "userId": row["user_id"],
        "username": row["username"] or "",
        "userProfileImage": resolve_url(row["user_profile_image"] or "", request),
        "commentText": row["comment_text"] or "",
        "timestamp": _safe_int(row["timestamp"], 0),
        "likesCount": max(0, _safe_int(row["likes_count"], 0)),
        "parentId": row["parent_id"] or "",
        "repliedToUserId": row["replied_to_user_id"] or "",
        "repliedToUsername": row["replied_to_username"] or "",
        "isVerified": is_ver,
        "verifiedAccount": is_ver,
    }


def row_to_story(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    sid = row["story_id"]
    viewers = [
        r["viewer_uid"]
        for r in db.execute(
            "SELECT viewer_uid FROM story_viewers WHERE story_id = ? ORDER BY timestamp DESC",
            (sid,),
        ).fetchall()
    ]
    reaction_rows = db.execute(
        "SELECT reactor_uid, reactor_username, reactor_profile_image, emoji FROM story_reactions WHERE story_id = ?",
        (sid,),
    ).fetchall()
    reactions = {r["reactor_uid"]: r["emoji"] for r in reaction_rows}
    reaction_usernames = {r["reactor_uid"]: r["reactor_username"] for r in reaction_rows}
    reaction_profiles = {
        r["reactor_uid"]: resolve_url(r["reactor_profile_image"] or "", request)
        for r in reaction_rows
    }
    is_ver = bool(row["is_verified"])
    return {
        "storyId": sid,
        "userId": row["user_id"],
        "username": row["username"],
        "userProfileImage": resolve_url(row["user_profile_image"] or "", request),
        "mediaUrl": resolve_url(row["media_url"] or "", request),
        "mediaType": row["media_type"] or "image",
        "timestamp": _safe_int(row["timestamp"], 0),
        "expiresAt": _safe_int(row["expires_at"], 0),
        "viewers": viewers,
        "isVerified": is_ver,
        "verifiedAccount": is_ver,
        "originalPostId": row["original_post_id"] or "",
        "originalAuthorUsername": row["original_author_username"] or "",
        "caption": row["caption"] or "",
        "sticker": row["sticker"] or "",
        "textX": _safe_float(row["text_x"], 0.5),
        "textY": _safe_float(row["text_y"], 0.5),
        "textColor": row["text_color"] or "#FFFFFF",
        "textScale": _safe_float(row["text_scale"], 1.0),
        "textRotation": _safe_float(row["text_rotation"], 0.0),
        "reactions": reactions,
        "reactionUsernames": reaction_usernames,
        "reactionProfiles": reaction_profiles,
    }


def _safe_json_dict(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def row_to_chat_message(
    row: sqlite3.Row,
    request: Optional[Request] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "messageId": row["message_id"],
        "senderId": row["sender_id"],
        "receiverId": row["receiver_id"],
        "senderUsername": row["sender_username"] or "",
        "senderProfileImage": resolve_url(row["sender_profile_image"] or "", request, base_url),
        "message": row["message"] or "",
        "mediaUrl": resolve_url(row["media_url"] or "", request, base_url),
        "timestamp": _safe_int(row["timestamp"], 0),
        "type": row["type"] or "text",
        "seen": bool(row["seen"]),
        "deleted": bool(row["deleted"]),
        "deletedFor": _safe_json_dict(row["deleted_for_json"]),
        "reactions": _safe_json_dict(row["reactions_json"]),
        "forwarded": bool(row["forwarded"]),
        "replyToMessageId": row["reply_to_message_id"] or "",
        "replyToSenderUsername": row["reply_to_sender_username"] or "",
        "replyToText": row["reply_to_text"] or "",
    }


def build_group_dict(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    gid = row["group_id"]
    member_rows = db.execute(
        """
        SELECT gm.uid, gm.is_admin, u.username, u.profile_image
        FROM group_members gm
        LEFT JOIN users u ON u.uid = gm.uid
        WHERE gm.group_id = ?
        """,
        (gid,),
    ).fetchall()
    admins: Dict[str, bool] = {}
    members: Dict[str, bool] = {}
    member_usernames: Dict[str, str] = {}
    member_profiles: Dict[str, str] = {}
    for mr in member_rows:
        uid = mr["uid"]
        members[uid] = True
        if mr["is_admin"]:
            admins[uid] = True
        member_usernames[uid] = mr["username"] or f"user_{uid[:4]}"
        member_profiles[uid] = resolve_url(mr["profile_image"] or "", request)

    return {
        "groupId": gid,
        "name": row["name"],
        "description": row["description"] or "",
        "groupImage": resolve_url(row["group_image"] or "", request),
        "adminId": row["admin_id"],
        "adminUsername": row["admin_username"] or "",
        "admins": admins,
        "members": members,
        "memberUsernames": member_usernames,
        "memberProfiles": member_profiles,
        "lastMessage": row["last_message"] or "",
        "lastMessageSenderId": row["last_message_sender_id"] or "",
        "lastMessageSenderName": row["last_message_sender_name"] or "",
        "lastMessageTime": _safe_int(row["last_message_time"], 0),
        "createdAt": _safe_int(row["created_at"], 0),
        "unreadCount": 0,
    }


def build_groups_batch(
    db: sqlite3.Connection,
    rows: List[sqlite3.Row],
    request: Optional[Request] = None,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    group_ids = [r["group_id"] for r in rows]
    members_by_group: Dict[str, List[sqlite3.Row]] = {gid: [] for gid in group_ids}

    for i in range(0, len(group_ids), 500):
        chunk = group_ids[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        m_rows = db.execute(
            f"""
            SELECT gm.group_id, gm.uid, gm.is_admin, u.username, u.profile_image
            FROM group_members gm
            LEFT JOIN users u ON u.uid = gm.uid
            WHERE gm.group_id IN ({placeholders})
            """,
            tuple(chunk),
        ).fetchall()
        for mr in m_rows:
            members_by_group.setdefault(mr["group_id"], []).append(mr)

    result: List[Dict[str, Any]] = []
    for row in rows:
        gid = row["group_id"]
        admins: Dict[str, bool] = {}
        members: Dict[str, bool] = {}
        member_usernames: Dict[str, str] = {}
        member_profiles: Dict[str, str] = {}
        for mr in members_by_group.get(gid, ()):
            muid = mr["uid"]
            members[muid] = True
            if mr["is_admin"]:
                admins[muid] = True
            member_usernames[muid] = mr["username"] or f"user_{muid[:4]}"
            member_profiles[muid] = resolve_url(mr["profile_image"] or "", request)

        result.append(
            {
                "groupId": gid,
                "name": row["name"],
                "description": row["description"] or "",
                "groupImage": resolve_url(row["group_image"] or "", request),
                "adminId": row["admin_id"],
                "adminUsername": row["admin_username"] or "",
                "admins": admins,
                "members": members,
                "memberUsernames": member_usernames,
                "memberProfiles": member_profiles,
                "lastMessage": row["last_message"] or "",
                "lastMessageSenderId": row["last_message_sender_id"] or "",
                "lastMessageSenderName": row["last_message_sender_name"] or "",
                "lastMessageTime": _safe_int(row["last_message_time"], 0),
                "createdAt": _safe_int(row["created_at"], 0),
                "unreadCount": 0,
            }
        )
    return result


def row_to_notification(row: sqlite3.Row, request: Optional[Request] = None) -> Dict[str, Any]:
    is_read = bool(row["is_read"])
    return {
        "notificationId": row["notification_id"],
        "receiverId": row["receiver_id"],
        "senderId": row["sender_id"],
        "senderUsername": row["sender_username"] or "",
        "senderProfileImage": resolve_url(row["sender_profile_image"] or "", request),
        "type": row["type"],
        "postId": row["post_id"] or "",
        "postMediaUrl": resolve_url(row["post_media_url"] or "", request),
        "commentId": row["comment_id"] or "",
        "text": row["text"] or "",
        "conversationId": row["conversation_id"] or "",
        "groupId": row["group_id"] or "",
        "title": row["title"] or "",
        "timestamp": _safe_int(row["timestamp"], 0),
        "isRead": is_read,
        "seen": is_read,
        "pushedToSystem": bool(row["pushed_to_system"]),
    }


def get_blocked_peer_ids(db: sqlite3.Connection, uid: str) -> List[str]:
    from .cache import blocked_peers_cache

    cached = blocked_peers_cache.get(uid)
    if cached is not None:
        return list(cached)

    rows = db.execute(
        """
        SELECT blocked_uid AS peer FROM blocks WHERE blocker_uid = ?
        UNION
        SELECT blocker_uid AS peer FROM blocks WHERE blocked_uid = ?
        """,
        (uid, uid),
    ).fetchall()
    peers = frozenset(r["peer"] for r in rows if r["peer"] and r["peer"] != uid)
    blocked_peers_cache.set(uid, peers, ttl_s=15.0)
    return list(peers)


def get_blocked_peer_set_cached(uid: str, db: Optional[sqlite3.Connection] = None) -> frozenset:
    from .cache import blocked_peers_cache
    from .database import get_db

    cached = blocked_peers_cache.get(uid)
    if cached is not None:
        return cached
    if db is not None:
        return frozenset(get_blocked_peer_ids(db, uid))
    with get_db() as conn:
        return frozenset(get_blocked_peer_ids(conn, uid))


def is_pair_blocked(db: sqlite3.Connection, uid1: str, uid2: str) -> bool:
    row = db.execute(
        """
        SELECT 1 FROM blocks
        WHERE (blocker_uid = ? AND blocked_uid = ?)
           OR (blocker_uid = ? AND blocked_uid = ?)
        LIMIT 1
        """,
        (uid1, uid2, uid2, uid1),
    ).fetchone()
    return row is not None
