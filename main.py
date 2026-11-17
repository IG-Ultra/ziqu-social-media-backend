import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import get_db, init_db
from app.realtime import manager
from app.routers import admin, auth, chat, follows, media, moderation, notifications, posts, stories, users
from app.routers.chat import (
    broadcast_typing_indicator,
    mark_chat_message_seen_and_notify,
    persist_and_broadcast_chat_message,
    persist_and_broadcast_group_message,
    toggle_chat_message_reaction_and_notify,
)
from app.security import decode_token
from app.utils import get_websocket_base_url, now_ms

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ziqu.ws")
keep_alive_logger = logging.getLogger("ziqu.keepalive")

_discovered_external_url: str = ""


async def _keep_alive_loop() -> None:
    # Wait briefly after boot so Uvicorn is already listening before the first ping
    await asyncio.sleep(10)
    interval = settings.KEEP_ALIVE_INTERVAL_SECONDS
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        while True:
            target_base = (
                settings.KEEP_ALIVE_URL
                or _discovered_external_url
                or settings.PUBLIC_BASE_URL
                or f"http://127.0.0.1:{settings.PORT}"
            ).rstrip("/")
            ping_url = f"{target_base}/ping"
            try:
                resp = await client.get(ping_url, headers={"User-Agent": "ZIQU-KeepAlive/1.0"})
                keep_alive_logger.debug("Keep-alive ping %s -> %s", ping_url, resp.status_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                keep_alive_logger.debug("Keep-alive ping error (%s): %s", ping_url, exc)
            await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    keep_alive_task: Optional[asyncio.Task] = None
    if settings.KEEP_ALIVE_ENABLED:
        keep_alive_task = asyncio.create_task(_keep_alive_loop())
        keep_alive_logger.info(
            "Render anti-sleep keep-alive started (every %ss)",
            settings.KEEP_ALIVE_INTERVAL_SECONDS,
        )
    try:
        yield
    finally:
        if keep_alive_task is not None:
            keep_alive_task.cancel()
            try:
                await keep_alive_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="ZIQU Social Backend API",
    version="1.1.0",
    description="Self-hosted Python FastAPI backend with real-time WebSocket messaging for ZIQU (Render-ready)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(posts.router)
app.include_router(follows.router)
app.include_router(stories.router)
app.include_router(chat.router)
app.include_router(notifications.router)
app.include_router(moderation.router)
app.include_router(media.router)
app.include_router(admin.router)

app.mount("/media", StaticFiles(directory=str(settings.UPLOAD_DIR)), name="media")


@app.middleware("http")
async def _discover_public_url_middleware(request: Request, call_next):
    global _discovered_external_url
    if not _discovered_external_url and not settings.KEEP_ALIVE_URL:
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        if host and not host.startswith("127.0.0.1") and not host.startswith("localhost"):
            proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if ".onrender.com" in host or proto == "https":
                proto = "https"
            elif not proto:
                proto = "http"
            _discovered_external_url = f"{proto}://{host}".rstrip("/")
    return await call_next(request)


@app.api_route("/", methods=["GET", "HEAD"])
def root_status():
    return {
        "service": "ziqu-python-backend",
        "status": "ok",
        "websocket": "/ws",
        "health": "/health",
        "keepAlive": settings.KEEP_ALIVE_ENABLED,
        "keepAliveIntervalSeconds": settings.KEEP_ALIVE_INTERVAL_SECONDS,
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    stats = manager.active_stats()
    return {
        "status": "ok",
        "service": "ziqu-python-backend",
        **stats,
    }


@app.api_route("/ping", methods=["GET", "HEAD"])
def keep_alive_ping():
    return {
        "status": "awake",
        "serverTime": now_ms(),
    }


@app.api_route("/ws", methods=["GET", "HEAD"])
def websocket_http_info():
    return {
        "status": "ok",
        "endpoint": "/ws",
        "protocol": "websocket",
        "message": "Upgrade connection via wss://<host>/ws?token=<JWT>",
    }


def _extract_ws_token(websocket: WebSocket, query_token: Optional[str]) -> str:
    if query_token and query_token.strip():
        return query_token.strip()
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


async def _send_ws_ack(
    websocket: WebSocket,
    client_msg_id: str,
    status_str: str,
    extra: Optional[dict] = None,
) -> None:
    if not client_msg_id:
        return
    payload = {"event": "ws_ack", "clientMsgId": client_msg_id, "status": status_str}
    if extra:
        payload.update(extra)
    await websocket.send_text(json.dumps(payload))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    resolved_token = _extract_ws_token(websocket, token)
    uid = decode_token(resolved_token) if resolved_token else None
    if not uid:
        await websocket.close(code=4401)
        return

    with get_db() as db:
        u_row = db.execute("SELECT is_banned FROM users WHERE uid = ?", (uid,)).fetchone()
        if not u_row or u_row["is_banned"]:
            await websocket.close(code=4403)
            return

    ws_base_url = get_websocket_base_url(websocket.headers)
    await manager.connect(uid, websocket)
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "event": "connected",
                    "uid": uid,
                    "serverTime": now_ms(),
                }
            )
        )

        while True:
            raw = await websocket.receive_text()
            if raw == "ping":
                await websocket.send_text("pong")
                continue

            try:
                data = json.loads(raw)
            except Exception:
                continue

            if not isinstance(data, dict):
                continue

            action = str(data.get("action") or data.get("type") or "").strip().lower()
            client_msg_id = str(data.get("clientMsgId") or "").strip()

            if action == "ping":
                await websocket.send_text(json.dumps({"event": "pong", "serverTime": now_ms()}))

            elif action == "typing":
                conv_id = str(data.get("conversationId") or "").strip()
                is_typing = bool(data.get("isTyping", False))
                if conv_id:
                    await broadcast_typing_indicator(uid, conv_id, is_typing)

            elif action == "mark_seen":
                conv_id = str(data.get("conversationId") or "").strip()
                msg_id = str(data.get("messageId") or "").strip()
                if conv_id and msg_id:
                    await mark_chat_message_seen_and_notify(uid, conv_id, msg_id)

            elif action == "toggle_reaction":
                conv_id = str(data.get("conversationId") or "").strip()
                msg_id = str(data.get("messageId") or "").strip()
                emoji = str(data.get("emoji") or "").strip()
                if conv_id and msg_id and emoji:
                    try:
                        reactions = await toggle_chat_message_reaction_and_notify(uid, conv_id, msg_id, emoji)
                        await _send_ws_ack(websocket, client_msg_id, "ok", {"reactions": reactions})
                    except Exception as exc:
                        await _send_ws_ack(websocket, client_msg_id, "error", {"error": str(exc)})

            elif action == "presence":
                is_online = bool(data.get("isOnline", True))
                await manager.set_presence(uid, is_online)

            elif action == "send_message":
                try:
                    msg_dict = await persist_and_broadcast_chat_message(
                        uid=uid,
                        conversation_id=str(data.get("conversationId") or ""),
                        receiver_id=str(data.get("receiverId") or ""),
                        message_text=str(data.get("messageText") or ""),
                        media_url=str(data.get("mediaUrl") or ""),
                        media_type=str(data.get("mediaType") or "text"),
                        forwarded=bool(data.get("forwarded", False)),
                        reply_to_message_id=str(data.get("replyToMessageId") or ""),
                        reply_to_sender_username=str(data.get("replyToSenderUsername") or ""),
                        reply_to_text=str(data.get("replyToText") or ""),
                        base_url=ws_base_url,
                    )
                    await _send_ws_ack(websocket, client_msg_id, "ok", {"message": msg_dict})
                except Exception as exc:
                    await _send_ws_ack(websocket, client_msg_id, "error", {"error": str(exc)})

            elif action == "send_group_message":
                try:
                    msg_dict = await persist_and_broadcast_group_message(
                        uid=uid,
                        group_id=str(data.get("groupId") or ""),
                        message_text=str(data.get("messageText") or ""),
                        media_url=str(data.get("mediaUrl") or ""),
                        media_type=str(data.get("mediaType") or "text"),
                        reply_to_message_id=str(data.get("replyToMessageId") or ""),
                        reply_to_sender_username=str(data.get("replyToSenderUsername") or ""),
                        reply_to_text=str(data.get("replyToText") or ""),
                        base_url=ws_base_url,
                    )
                    await _send_ws_ack(websocket, client_msg_id, "ok", {"message": msg_dict})
                except Exception as exc:
                    await _send_ws_ack(websocket, client_msg_id, "error", {"error": str(exc)})

    except WebSocketDisconnect:
        await manager.disconnect(uid, websocket)
    except Exception as exc:
        logger.warning("WebSocket disconnected for %s: %s", uid, exc)
        await manager.disconnect(uid, websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
