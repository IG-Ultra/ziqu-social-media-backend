
"""
Ziqu Social Media API
FastAPI + SQLAlchemy 2.0 + PostgreSQL
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

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
    # Serialize table creation across multiple workers.
    lock_id = 734829104

    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": lock_id}
        )
        connection.commit()

        try:
            Base.metadata.create_all(bind=connection)
            connection.commit()
            print("Ziqu PostgreSQL tables verified successfully.")

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": lock_id}
            )
            connection.commit()

    yield

    engine.dispose()
    print("Ziqu database connection pool closed.")


app = FastAPI(
    title="Ziqu Backend API",
    description="Instagram-style Social Media Backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{time.time() - start_time:.4f}s"
    return response


app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(users_router, prefix="/api/v1/users", tags=["Users & Profiles"])
app.include_router(feed_router, prefix="/api/v1/feed", tags=["Home & Explore Feed"])
app.include_router(posts_router, prefix="/api/v1/posts", tags=["Posts & Carousels"])
app.include_router(reels_router, prefix="/api/v1/reels", tags=["Reels"])
app.include_router(stories_router, prefix="/api/v1/stories", tags=["Stories"])
app.include_router(interactions_router, prefix="/api/v1/interactions", tags=["Interactions"])
app.include_router(messages_router, prefix="/api/v1/messages", tags=["Messages"])


@app.get("/", tags=["Health"])
def health_check():
    return {
        "status": "online",
        "app": "Ziqu Social Media Engine",
        "engine": "FastAPI + PostgreSQL",
        "version": "1.0.0",
        "docs": "/docs"
    }
