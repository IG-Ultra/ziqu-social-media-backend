"""
Pydantic v2 Data Validation & Serialization Schemas
"""

from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import List, Optional
from datetime import datetime


# --- Auth Schemas ---
class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=30, pattern="^[a-zA-Z0-9_.]+$")
    email: EmailStr
    password: str = Field(..., min_length=6)
    full_name: Optional[str] = None


class UserLogin(BaseModel):
    username_or_email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str


# --- User Schemas ---
class UserProfilePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: Optional[str] = None
    bio: Optional[str] = None
    profile_pic_url: Optional[str] = None
    is_verified: bool
    followers_count: int = 0
    following_count: int = 0
    posts_count: int = 0
    is_following: Optional[bool] = False


# --- Post & Media Schemas ---
class PostMediaCreate(BaseModel):
    media_url: str
    media_type: str = "image"
    aspect_ratio: Optional[str] = "1:1"
    duration_sec: Optional[int] = None


class PostMediaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    media_url: str
    media_type: str
    order_index: int
    aspect_ratio: Optional[str]


class PostCreate(BaseModel):
    caption: Optional[str] = None
    location: Optional[str] = None
    post_type: str = "post"  # 'post', 'carousel', or 'reel'
    media_items: List[PostMediaCreate]


class PostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author: UserProfilePublic
    caption: Optional[str]
    location: Optional[str]
    post_type: str
    likes_count: int
    comments_count: int
    created_at: datetime
    media: List[PostMediaResponse]
    is_liked: bool = False
    is_saved: bool = False


# --- Comment Schemas ---
class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    parent_comment_id: Optional[int] = None


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author: UserProfilePublic
    content: str
    parent_comment_id: Optional[int]
    likes_count: int
    created_at: datetime


# --- Story Schemas ---
class StoryCreate(BaseModel):
    media_url: str
    media_type: str = "image"
    caption: Optional[str] = None


class StoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author: UserProfilePublic
    media_url: str
    media_type: str
    caption: Optional[str]
    created_at: datetime
    expires_at: datetime
    is_viewed: bool = False


# --- Direct Message Schemas ---
class MessageCreate(BaseModel):
    receiver_id: int
    content: str
    media_url: Optional[str] = None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_id: int
    receiver_id: int
    content: str
    media_url: Optional[str]
    is_read: bool
    created_at: datetime
