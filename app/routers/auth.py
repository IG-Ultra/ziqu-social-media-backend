"""
Authentication Router: Register, Login, Token generation
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_password_hash, verify_password, create_access_token
from app import models, schemas

auth_router = APIRouter()


@auth_router.post("/register", response_model=schemas.TokenResponse, status_code=status.HTTP_201_CREATED)
def register_user(payload: schemas.UserRegister, db: Session = Depends(get_db)):
    # Check username existence
    if db.query(models.User).filter(models.User.username == payload.username.lower()).first():
        raise HTTPException(status_code=400, detail="Username is already taken.")

    # Check email existence
    if db.query(models.User).filter(models.User.email == payload.email.lower()).first():
        raise HTTPException(status_code=400, detail="Email is already registered.")

    hashed_pw = get_password_hash(payload.password)
    user = models.User(
        username=payload.username.lower(),
        email=payload.email.lower(),
        hashed_password=hashed_pw,
        full_name=payload.full_name,
        profile_pic_url=f"https://api.dicebear.com/7.x/avataaars/svg?seed={payload.username}",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(data={"sub": user.id, "username": user.username})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
    }


@auth_router.post("/login", response_model=schemas.TokenResponse)
def login_user(payload: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        (models.User.username == payload.username_or_email.lower()) | 
        (models.User.email == payload.username_or_email.lower())
    ).first()

    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Incorrect username/email or password"
        )

    token = create_access_token(data={"sub": user.id, "username": user.username})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
    }
