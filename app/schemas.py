from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    email: str
    password: str
    username: str
    displayName: str = ""
    bio: str = ""
    profileImage: str = ""


class LoginRequest(BaseModel):
    identifier: str
    password: str


class ForgotPasswordRequest(BaseModel):
    identifier: str


class ResetPasswordRequest(BaseModel):
    identifier: str
    resetCode: str
    newPassword: str


class ResetPasswordByTokenRequest(BaseModel):
    token: str
    newPassword: str


class ChangeUsernameRequest(BaseModel):
    newUsername: str


class UpdateProfileRequest(BaseModel):
    displayName: str = ""
    bio: str = ""
    profileImage: Optional[str] = None


class BatchUsersRequest(BaseModel):
    uids: List[str] = Field(default_factory=list)


class PresenceUpdateRequest(BaseModel):
    isOnline: bool


class CreatePostRequest(BaseModel):
    postId: Optional[str] = None
    mediaUrl: str
    thumbnailUrl: str = ""
    mediaType: str = "image"
    caption: str = ""
    mood: str = ""
    isReel: bool = False


class PostMetricRequest(BaseModel):
    watchTimeMs: int = 0
    completed: bool = False
    rewatched: bool = False
    shared: bool = False


class CreateCommentRequest(BaseModel):
    commentText: str
    parentId: str = ""
    repliedToUserId: str = ""
    repliedToUsername: str = ""


class CreateStoryRequest(BaseModel):
    mediaUrl: str
    mediaType: str = "image"
    caption: str = ""
    sticker: str = ""
    textX: float = 0.5
    textY: float = 0.5
    textColor: str = "#FFFFFF"
    textScale: float = 1.0
    textRotation: float = 0.0
    originalPostId: str = ""
    originalAuthorUsername: str = ""


class StoryReactionRequest(BaseModel):
    emoji: str


class SendChatMessageRequest(BaseModel):
    receiverId: str
    messageText: str = ""
    mediaUrl: str = ""
    mediaType: str = "text"
    forwarded: bool = False
    replyToMessageId: str = ""
    replyToSenderUsername: str = ""
    replyToText: str = ""


class ReactionRequest(BaseModel):
    emoji: str


class TypingRequest(BaseModel):
    isTyping: bool


class CreateGroupRequest(BaseModel):
    name: str
    description: str = ""
    groupImage: str = ""
    memberUids: List[str] = Field(default_factory=list)


class SendGroupMessageRequest(BaseModel):
    messageText: str = ""
    mediaUrl: str = ""
    mediaType: str = "text"
    replyToMessageId: str = ""
    replyToSenderUsername: str = ""
    replyToText: str = ""


class AddGroupMembersRequest(BaseModel):
    memberUids: List[str] = Field(default_factory=list)


class UpdateGroupNameRequest(BaseModel):
    name: str


class UpdateGroupImageRequest(BaseModel):
    groupImage: str


class CreateNotificationRequest(BaseModel):
    receiverId: str
    type: str = "like"
    postId: str = ""
    postMediaUrl: str = ""
    commentId: str = ""
    text: str = ""
    conversationId: str = ""
    groupId: str = ""
    title: str = ""


class AckPendingPushRequest(BaseModel):
    notificationIds: List[str] = Field(default_factory=list)


class BlockUserRequest(BaseModel):
    blockedUsername: str = ""


class ReportRequest(BaseModel):
    reportedUid: str = ""
    reportedUsername: str = ""
    reason: str
    details: str = ""
    targetType: str = "user"
    targetId: str = ""
    alsoBlock: bool = False
