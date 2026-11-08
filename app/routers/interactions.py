"""
Interactions Router: Likes, comments, saves, and atomic increment counters.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import update
from typing import List
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

interactions_router = APIRouter()


@interactions_router.post("/posts/{post_id}/like")
def toggle_like_post(
    post_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing_like = db.query(models.Like).filter(
        models.Like.user_id == current_user.id,
        models.Like.post_id == post_id
    ).first()

    if existing_like:
        db.delete(existing_like)
        # Atomic counter decrement
        db.execute(
            update(models.Post)
            .where(models.Post.id == post_id)
            .values(likes_count=models.Post.likes_count - 1)
        )
        db.commit()
        return {"action": "unliked", "post_id": post_id, "likes_count": post.likes_count - 1}
    else:
        new_like = models.Like(user_id=current_user.id, post_id=post_id)
        db.add(new_like)
        # Atomic counter increment
        db.execute(
            update(models.Post)
            .where(models.Post.id == post_id)
            .values(likes_count=models.Post.likes_count + 1)
        )
        db.commit()
        return {"action": "liked", "post_id": post_id, "likes_count": post.likes_count + 1}


@interactions_router.post("/posts/{post_id}/comments", response_model=schemas.CommentResponse)
def add_comment(
    post_id: int,
    payload: schemas.CommentCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    comment = models.Comment(
        post_id=post_id,
        user_id=current_user.id,
        content=payload.content,
        parent_comment_id=payload.parent_comment_id,
    )
    db.add(comment)
    db.execute(
        update(models.Post)
        .where(models.Post.id == post_id)
        .values(comments_count=models.Post.comments_count + 1)
    )
    db.commit()
    db.refresh(comment)
    return comment


@interactions_router.get("/posts/{post_id}/comments", response_model=List[schemas.CommentResponse])
def get_comments(
    post_id: int,
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Comment)
        .filter(models.Comment.post_id == post_id)
        .order_by(models.Comment.created_at.desc())
        .all()
    )


@interactions_router.post("/posts/{post_id}/save")
def toggle_save_post(
    post_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    saved = db.query(models.SavedPost).filter(
        models.SavedPost.user_id == current_user.id,
        models.SavedPost.post_id == post_id
    ).first()

    if saved:
        db.delete(saved)
        db.commit()
        return {"action": "unsaved", "post_id": post_id}
    else:
        new_save = models.SavedPost(user_id=current_user.id, post_id=post_id)
        db.add(new_save)
        db.commit()
        return {"action": "saved", "post_id": post_id}
