import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fastapi import WebSocket
from starlette.concurrency import run_in_threadpool

from .database import get_db

_MAX_SOCKETS_PER_USER = 8
_FANOUT_BATCH_SIZE = 200
_WS_SEND_TIMEOUT_S = 1.5


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: Dict[str, List[WebSocket]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._coalesce_tasks: Dict[str, asyncio.Task] = {}
        self._coalesce_payloads: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _write_presence_sync(uid: str, is_online: bool, now: int) -> List[str]:
        with get_db(write=True) as db:
            db.execute(
                """
                INSERT INTO user_presence(uid, is_online, last_seen)
                VALUES(?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET is_online=excluded.is_online, last_seen=excluded.last_seen
                """,
                (uid, 1 if is_online else 0, now),
            )
            peer_rows = db.execute(
                """
                SELECT CASE WHEN user1_uid = ? THEN user2_uid ELSE user1_uid END AS peer_uid
                FROM conversations
                WHERE user1_uid = ? OR user2_uid = ?
                ORDER BY last_message_time DESC
                LIMIT 100
                """,
                (uid, uid, uid),
            ).fetchall()
            return [r["peer_uid"] for r in peer_rows if r["peer_uid"]]

    async def set_presence(self, uid: str, is_online: bool) -> None:
        now = int(time.time() * 1000)
        peers = await run_in_threadpool(self._write_presence_sync, uid, is_online, now)
        payload = {
            "event": "presence",
            "uid": uid,
            "isOnline": is_online,
            "lastSeen": now,
        }
        # Avoid O(N^2) global presence storms at large scale; fan out to conversation peers when large
        if len(self.active_connections) <= 200:
            self.schedule_broadcast(f"presence:{uid}", payload, delay_s=0.15)
        elif peers:
            await self.send_to_users(peers, payload)

    async def connect(self, uid: str, websocket: WebSocket) -> None:
        await websocket.accept()
        evict: Optional[WebSocket] = None
        async with self._lock:
            sockets = self.active_connections[uid]
            if websocket not in sockets:
                sockets.append(websocket)
            if len(sockets) > _MAX_SOCKETS_PER_USER:
                evict = sockets.pop(0)
        if evict is not None:
            try:
                await evict.close(code=4000, reason="Superseded by newer connection")
            except Exception:
                pass
        await self.set_presence(uid, True)

    async def disconnect(self, uid: str, websocket: WebSocket) -> None:
        still_online = False
        async with self._lock:
            sockets = self.active_connections.get(uid)
            if sockets:
                if websocket in sockets:
                    sockets.remove(websocket)
                if not sockets:
                    del self.active_connections[uid]
                else:
                    still_online = True
        if not still_online:
            await self.set_presence(uid, False)

    def active_stats(self) -> Dict[str, int]:
        users = len(self.active_connections)
        sockets = sum(len(s) for s in self.active_connections.values())
        return {
            "activeUsers": users,
            "onlineUsers": users,
            "activeSockets": sockets,
        }

    def online_uids(self) -> Set[str]:
        return {uid for uid, sockets in self.active_connections.items() if sockets}

    async def disconnect_user(
        self,
        uid: str,
        reason: str = "Your account has been suspended by an administrator.",
    ) -> None:
        async with self._lock:
            sockets = list(self.active_connections.pop(uid, []))

        if sockets:
            ban_msg = json.dumps({"event": "account_banned", "reason": reason})
            for ws in sockets:
                try:
                    await asyncio.wait_for(ws.send_text(ban_msg), timeout=_WS_SEND_TIMEOUT_S)
                except Exception:
                    pass
                try:
                    await ws.close(code=4403, reason="Account suspended")
                except Exception:
                    pass

        await self.set_presence(uid, False)

    async def _send_batch(self, targets: List[Tuple[str, WebSocket]], message: str) -> None:
        if not targets:
            return
        dead: List[Tuple[str, WebSocket]] = []

        async def _send_one(target_uid: str, ws: WebSocket) -> None:
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=_WS_SEND_TIMEOUT_S)
            except Exception:
                dead.append((target_uid, ws))

        for i in range(0, len(targets), _FANOUT_BATCH_SIZE):
            chunk = targets[i : i + _FANOUT_BATCH_SIZE]
            await asyncio.gather(*(_send_one(u, w) for u, w in chunk), return_exceptions=True)

        if dead:
            async with self._lock:
                for target_uid, ws in dead:
                    sockets = self.active_connections.get(target_uid)
                    if sockets and ws in sockets:
                        sockets.remove(ws)
                        if not sockets:
                            del self.active_connections[target_uid]

    async def send_to_user(self, uid: str, payload: Dict[str, Any]) -> None:
        sockets = list(self.active_connections.get(uid, ()))
        if not sockets:
            return
        message = json.dumps(payload)
        await self._send_batch([(uid, ws) for ws in sockets], message)

    async def send_to_users(self, uids: Iterable[str], payload: Dict[str, Any]) -> None:
        targets: List[Tuple[str, WebSocket]] = []
        seen_uids: Set[str] = set()
        for uid in uids:
            if not uid or uid in seen_uids:
                continue
            seen_uids.add(uid)
            sockets = self.active_connections.get(uid)
            if sockets:
                for ws in list(sockets):
                    targets.append((uid, ws))
        if not targets:
            return
        message = json.dumps(payload)
        await self._send_batch(targets, message)

    async def broadcast_all(self, payload: Dict[str, Any]) -> None:
        targets: List[Tuple[str, WebSocket]] = [
            (uid, ws)
            for uid, sockets in list(self.active_connections.items())
            for ws in list(sockets)
        ]
        if not targets:
            return
        message = json.dumps(payload)
        await self._send_batch(targets, message)

    def schedule_broadcast(self, coalesce_key: str, payload: Dict[str, Any], delay_s: float = 0.25) -> None:
        """Coalesce rapid global events (likes/views/follows/posts) off the request hot path."""
        self._coalesce_payloads[coalesce_key] = payload
        existing = self._coalesce_tasks.get(coalesce_key)
        if existing is not None and not existing.done():
            return

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay_s)
                latest_payload = self._coalesce_payloads.pop(coalesce_key, None)
                if latest_payload is not None:
                    await self.broadcast_all(latest_payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self._coalesce_tasks.pop(coalesce_key, None)

        try:
            loop = asyncio.get_running_loop()
            self._coalesce_tasks[coalesce_key] = loop.create_task(_runner())
        except RuntimeError:
            self._coalesce_payloads.pop(coalesce_key, None)


manager = ConnectionManager()

