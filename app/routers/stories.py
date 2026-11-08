"""
Stories Router: 24-hour expiring media with viewer tracking.
"""

from datetime import datetime
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

stories_router = APIRouter()


@stories_router.get("/", response_model=List[schemas.StoryResponse])
def get_active_stories(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns non-expired stories from followed users and self:
    WHERE expires_at > NOW()
    """
    now = datetime.utcnow()
    
    # Active stories from people current user follows + self
    stories = (
        db.query(models.Story)
        .filter(
            models.Story.expires_at > now,
            (
                (models.Story.user_id == current_user.id) |
                models.Story.user_id.in_(
                    db.query(models.Follow.following_id)
                    .filter(models.Follow.follower_id == current_user.id, models.Follow.status == "accepted")
                )
            )
        )
        .order_by(models.Story.created_at.desc())
        .all()
    )

    response = []
    for s in stories:
        is_viewed = db.query(models.StoryView).filter(
            models.StoryView.story_id == s.id,
            models.StoryView.viewer_id == current_user.id
        ).first() is not None
        response.append({
            "id": s.id,
            "author": s.author,
            "media_url": s.media_url,
            "media_type": s.media_type,
            "caption": s.caption,
            "created_at": s.created_at,
            "expires_at": s.expires_at,
            "is_viewed": is_viewed,
        })
    return response


@stories_router.post("/", response_model=schemas.StoryResponse, status_code=status.HTTP_201_CREATED)
def create_story(
    payload: schemas.StoryCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    story = models.Story(
        user_id=current_user.id,
        media_url=payload.media_url,
        media_type=payload.media_type,
        caption=payload.caption,
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    return {
        "id": story.id,
        "author": current_user,
        "media_url": story.media_url,
        "media_type": story.media_type,
        "caption": story.caption,
        "created_at": story.created_at,
        "expires_at": story.expires_at,
        "is_viewed": False,
    }


@stories_router.post("/{story_id}/view")
def record_story_view(
    story_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    view = db.query(models.StoryView).filter(
        models.StoryView.story_id == story_id,
        models.StoryView.viewer_id == current_user.id
    ).first()
    if not view:
        view = models.StoryView(story_id=story_id, viewer_id=current_user.id)
        db.add(view)
        db.commit()
    return {"status": "view_recorded"}
