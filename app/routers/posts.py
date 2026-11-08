"""
Posts Router: Create and retrieve posts and carousel media.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

posts_router = APIRouter()


@posts_router.post("/", response_model=schemas.PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    payload: schemas.PostCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not payload.media_items:
        raise HTTPException(status_code=400, detail="At least one media item is required.")

    post = models.Post(
        user_id=current_user.id,
        caption=payload.caption,
        location=payload.location,
        post_type=payload.post_type,
    )
    db.add(post)
    db.flush()  # Generates post.id

    for idx, item in enumerate(payload.media_items):
        media = models.PostMedia(
            post_id=post.id,
            media_url=item.media_url,
            media_type=item.media_type,
            aspect_ratio=item.aspect_ratio or "1:1",
            duration_sec=item.duration_sec,
            order_index=idx,
        )
        db.add(media)

    db.commit()
    db.refresh(post)
    return post


@posts_router.get("/{post_id}", response_model=schemas.PostResponse)
def get_post_details(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    
    is_liked = db.query(models.Like).filter(
        models.Like.post_id == post_id, 
        models.Like.user_id == current_user.id
    ).first() is not None

    is_saved = db.query(models.SavedPost).filter(
        models.SavedPost.post_id == post_id, 
        models.SavedPost.user_id == current_user.id
    ).first() is not None

    return {
        "id": post.id,
        "author": post.author,
        "caption": post.caption,
        "location": post.location,
        "post_type": post.post_type,
        "likes_count": post.likes_count,
        "comments_count": post.comments_count,
        "created_at": post.created_at,
        "media": post.media,
        "is_liked": is_liked,
        "is_saved": is_saved,
    }


@posts_router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this post")
    db.delete(post)
    db.commit()
