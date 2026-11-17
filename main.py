import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.realtime import manager
from app.routers import auth, chat, follows, media, moderation, notifications, posts, stories, users
from app.security import decode_token

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="ZIQU Social Backend API",
    version="1.0.0",
    description="Self-hosted Python FastAPI backend for ZIQU Instagram-style Android app",
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

app.mount("/media", StaticFiles(directory=str(settings.UPLOAD_DIR)), name="media")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "ziqu-python-backend"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    uid = decode_token(token)
    if not uid:
        await websocket.close(code=4401)
        return
    await manager.connect(uid, websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await manager.disconnect(uid, websocket)
    except Exception:
        await manager.disconnect(uid, websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)
