"""
Users & Follow Graph Router
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

users_router = APIRouter()


@users_router.get("/{username}", response_model=schemas.UserProfilePublic)
def get_user_profile(
    username: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    followers_cnt = db.query(func.count(models.Follow.follower_id)).filter(
        models.Follow.following_id == user.id, models.Follow.status == "accepted"
    ).scalar() or 0

    following_cnt = db.query(func.count(models.Follow.following_id)).filter(
        models.Follow.follower_id == user.id, models.Follow.status == "accepted"
    ).scalar() or 0

    posts_cnt = db.query(func.count(models.Post.id)).filter(models.Post.user_id == user.id).scalar() or 0

    is_following = db.query(models.Follow).filter(
        models.Follow.follower_id == current_user.id,
        models.Follow.following_id == user.id
    ).first() is not None

    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "bio": user.bio,
        "profile_pic_url": user.profile_pic_url,
        "is_verified": user.is_verified,
        "followers_count": followers_cnt,
        "following_count": following_cnt,
        "posts_count": posts_cnt,
        "is_following": is_following,
    }


@users_router.post("/{user_id}/follow")
def follow_or_unfollow_user(
    user_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")

    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    existing_follow = db.query(models.Follow).filter(
        models.Follow.follower_id == current_user.id,
        models.Follow.following_id == user_id
    ).first()

    if existing_follow:
        db.delete(existing_follow)
        db.commit()
        return {"action": "unfollowed", "target_user_id": user_id}
    else:
        status_val = "pending" if target_user.is_private else "accepted"
        new_follow = models.Follow(
            follower_id=current_user.id,
            following_id=user_id,
            status=status_val
        )
        db.add(new_follow)
        db.commit()
        return {"action": "followed", "status": status_val, "target_user_id": user_id}
