"""
Ziqu Social Media API (Instagram-like Backend)
Framework: FastAPI (Python 3.11+) + SQLAlchemy 2.0 + PostgreSQL
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import time
from contextlib import asynccontextmanager

from app.database import engine, Base
from app.routers.auth import auth_router
from app.routers.users import users_router
from app.routers.posts import posts_router
from app.routers.feed import feed_router
from app.routers.stories import stories_router
from app.routers.reels import reels_router
from app.routers.interactions import interactions_router
from app.routers.messages import messages_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create DB tables if not using Alembic migrations
    Base.metadata.create_all(bind=engine)
    print("🚀 [Ziqu Backend] PostgreSQL Database tables verified & connected.")
    yield
    print("🛑 [Ziqu Backend] Shutting down connection pools...")


app = FastAPI(
    title="Ziqu Backend API",
    description="Scalable Instagram-style Social Media Engine in Python & SQL (FastAPI + PostgreSQL)",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS Middleware (React / Flutter / React Native clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = f"{process_time:.4f}s"
    return response


# Include Modular Routers
app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(users_router, prefix="/api/v1/users", tags=["Users & Profiles"])
app.include_router(feed_router, prefix="/api/v1/feed", tags=["Home & Explore Feed"])
app.include_router(posts_router, prefix="/api/v1/posts", tags=["Posts & Carousels"])
app.include_router(reels_router, prefix="/api/v1/reels", tags=["Reels"])
app.include_router(stories_router, prefix="/api/v1/stories", tags=["Stories (24h TTL)"])
app.include_router(interactions_router, prefix="/api/v1/interactions", tags=["Likes, Comments & Saves"])
app.include_router(messages_router, prefix="/api/v1/messages", tags=["Direct Messages"])


@app.get("/", tags=["Health"])
def health_check():
    return {
        "status": "online",
        "app": "Ziqu Social Media Engine",
        "engine": "FastAPI + PostgreSQL",
        "version": "1.0.0",
        "docs": "/docs"
    }
