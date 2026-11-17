import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, Set

from fastapi import WebSocket

from .database import get_db


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def set_presence(self, uid: str, is_online: bool) -> None:
        now = int(time.time() * 1000)
        with get_db() as db:
            db.execute(
                """
                INSERT INTO user_presence(uid, is_online, last_seen)
                VALUES(?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET is_online=excluded.is_online, last_seen=excluded.last_seen
                """,
                (uid, 1 if is_online else 0, now),
            )
        await self.broadcast_all(
            {
                "event": "presence",
                "uid": uid,
                "isOnline": is_online,
                "lastSeen": now,
            }
        )

    async def connect(self, uid: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections[uid].add(websocket)
        await self.set_presence(uid, True)

    async def disconnect(self, uid: str, websocket: WebSocket) -> None:
        still_online = False
        async with self._lock:
            if uid in self.active_connections:
                self.active_connections[uid].discard(websocket)
                if not self.active_connections[uid]:
                    del self.active_connections[uid]
                else:
                    still_online = True
        if not still_online:
            await self.set_presence(uid, False)

    def active_stats(self) -> Dict[str, int]:
        users = len(self.active_connections)
        sockets = sum(len(s) for s in self.active_connections.values())
        return {"activeUsers": users, "activeSockets": sockets}

    async def send_to_user(self, uid: str, payload: Dict[str, Any]) -> None:
        sockets = list(self.active_connections.get(uid, set()))
        if not sockets:
            return
        message = json.dumps(payload)
        dead = []
        for ws in sockets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.active_connections[uid].discard(ws)

    async def send_to_users(self, uids: Iterable[str], payload: Dict[str, Any]) -> None:
        for uid in set(uids):
            if uid:
                await self.send_to_user(uid, payload)

    async def broadcast_all(self, payload: Dict[str, Any]) -> None:
        all_uids = list(self.active_connections.keys())
        await self.send_to_users(all_uids, payload)


manager = ConnectionManager()
