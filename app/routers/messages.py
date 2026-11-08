"""
Direct Messages Router: 1-on-1 chats and conversation histories
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import List
from app.database import get_db
from app.auth import get_current_user
from app import models, schemas

messages_router = APIRouter()


@messages_router.post("/", response_model=schemas.MessageResponse)
def send_message(
    payload: schemas.MessageCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.receiver_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot send message to yourself")

    msg = models.DirectMessage(
        sender_id=current_user.id,
        receiver_id=payload.receiver_id,
        content=payload.content,
        media_url=payload.media_url,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@messages_router.get("/{other_user_id}", response_model=List[schemas.MessageResponse])
def get_conversation_history(
    other_user_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    messages = (
        db.query(models.DirectMessage)
        .filter(
            or_(
                and_(models.DirectMessage.sender_id == current_user.id, models.DirectMessage.receiver_id == other_user_id),
                and_(models.DirectMessage.sender_id == other_user_id, models.DirectMessage.receiver_id == current_user.id),
            )
        )
        .order_by(models.DirectMessage.created_at.asc())
        .limit(100)
        .all()
    )
    return messages
