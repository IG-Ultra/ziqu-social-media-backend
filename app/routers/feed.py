"""
Instagram-Style Feed Algorithm Router
Fetches personalized content from followed accounts and recommended high-engagement posts.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

feed_router = APIRouter()


@feed_router.get("/", response_model=List[schemas.PostResponse])
def get_home_feed(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Optimized SQL Feed Query:
    1. Grabs posts from accounts the user follows (or user's own posts)
    2. Computes dynamic ranking score: (likes * 2 + comments * 5) / (hours_ago + 2)^1.5
    3. Pagination via OFFSET and LIMIT
    """
    offset = (page - 1) * limit

    # High-performance SQL query with followed user filter
    sql_query = text("""
        SELECT 
            p.id,
            p.user_id,
            p.caption,
            p.location,
            p.post_type,
            p.likes_count,
            p.comments_count,
            p.created_at,
            EXISTS(SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = :current_user_id) as is_liked,
            EXISTS(SELECT 1 FROM saved_posts sp WHERE sp.post_id = p.id AND sp.user_id = :current_user_id) as is_saved,
            (
                (p.likes_count * 2.0 + p.comments_count * 5.0) / 
                POWER(GREATEST(EXTRACT(EPOCH FROM (NOW() - p.created_at)) / 3600.0, 0.1) + 2.0, 1.5)
            ) as ranking_score
        FROM posts p
        WHERE p.user_id = :current_user_id
           OR p.user_id IN (
               SELECT following_id FROM follows WHERE follower_id = :current_user_id AND status = 'accepted'
           )
        ORDER BY ranking_score DESC, p.created_at DESC
        LIMIT :limit OFFSET :offset;
    """)

    results = db.execute(sql_query, {
        "current_user_id": current_user.id,
        "limit": limit,
        "offset": offset
    }).fetchall()

    feed_items = []
    for row in results:
        post_obj = db.query(models.Post).filter(models.Post.id == row.id).first()
        if post_obj:
            feed_items.append({
                "id": post_obj.id,
                "author": post_obj.author,
                "caption": post_obj.caption,
                "location": post_obj.location,
                "post_type": post_obj.post_type,
                "likes_count": post_obj.likes_count,
                "comments_count": post_obj.comments_count,
                "created_at": post_obj.created_at,
                "media": post_obj.media,
                "is_liked": bool(row.is_liked),
                "is_saved": bool(row.is_saved),
            })

    return feed_items


@feed_router.get("/explore", response_model=List[schemas.PostResponse])
def get_explore_grid(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=60),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Explore Grid Algorithm: Popular trending posts across public accounts."""
    offset = (page - 1) * limit
    posts = (
        db.query(models.Post)
        .join(models.User)
        .filter(models.User.is_private == False)
        .order_by((models.Post.likes_count * 2 + models.Post.comments_count * 3).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return posts
