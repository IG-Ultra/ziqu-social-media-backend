"""
Reels Router: Infinite vertical video recommendation feed.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

reels_router = APIRouter()


@reels_router.get("/", response_model=List[schemas.PostResponse])
def get_reels_feed(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=20),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Reels Algorithm:
    Selects posts where post_type = 'reel', weighted by high play-rate and like ratios.
    """
    offset = (page - 1) * limit
    reels = (
        db.query(models.Post)
        .filter(models.Post.post_type == "reel")
        .order_by(desc(models.Post.likes_count * 3 + models.Post.comments_count * 5), desc(models.Post.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    result = []
    for r in reels:
        is_liked = db.query(models.Like).filter(
            models.Like.post_id == r.id, 
            models.Like.user_id == current_user.id
        ).first() is not None

        result.append({
            "id": r.id,
            "author": r.author,
            "caption": r.caption,
            "location": r.location,
            "post_type": "reel",
            "likes_count": r.likes_count,
            "comments_count": r.comments_count,
            "created_at": r.created_at,
            "media": r.media,
            "is_liked": is_liked,
            "is_saved": False,
        })
    return result
