"""Auth routes: login, password management, user CRUD (admin)."""

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from backend.database import get_db
from backend.models import User, UserStreamAssignment, generate_strong_password
from backend.schemas import (
    LoginRequest, TokenResponse, UserResponse, ChangePasswordRequest,
    UserCreateRequest, UserCreateResponse, UserUpdateRequest,
    UserResetPasswordResponse, UserListResponse,
)
from backend.auth import verify_password, hash_password, create_access_token, get_current_user

logger = logging.getLogger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id, username=user.username,
        is_active=user.is_active, is_admin=user.is_admin,
        must_change_password=user.must_change_password,
        auth_provider=user.auth_provider,
        created_at=user.created_at,
        assigned_stream_ids=[a.stream_id for a in user.assigned_streams],
    )


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == request.username).first()
    if user is None or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return TokenResponse(
        access_token=create_access_token(subject=user.username),
        must_change_password=user.must_change_password,
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return _user_to_response(current_user)


@router.post("/change-password")
def change_password(request: ChangePasswordRequest, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if verify_password(request.new_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="New password must be different")
    current_user.hashed_password = hash_password(request.new_password)
    current_user.must_change_password = False
    db.commit()
    return {"message": "Password changed successfully"}


# --- User management (admin only) ---

@router.get("/users", response_model=UserListResponse)
def list_users(db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    users = db.query(User).order_by(User.created_at.desc()).all()
    return UserListResponse(
        users=[_user_to_response(u) for u in users],
        total_count=len(users),
    )


@router.post("/users", response_model=UserCreateResponse, status_code=201)
def create_user(request: UserCreateRequest, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    if db.query(User).filter(User.username == request.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    # Generate a strong random password — displayed once to the admin
    generated_password = generate_strong_password()

    new_user = User(
        username=request.username,
        hashed_password=hash_password(generated_password),
        is_active=True,
        is_admin=request.is_admin,
        must_change_password=True,  # Force change on first login
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    logger.info("Admin %s created user %s (admin=%s)",
                current_user.username, request.username, request.is_admin)
    return UserCreateResponse(
        user=_user_to_response(new_user),
        generated_password=generated_password,
    )


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(user_id: int, body: UserUpdateRequest,
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    db.commit()
    db.refresh(user)
    return _user_to_response(user)


@router.post("/users/{user_id}/reset-password", response_model=UserResetPasswordResponse)
def reset_user_password(user_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_password = generate_strong_password()
    user.hashed_password = hash_password(new_password)
    user.must_change_password = True
    db.commit()
    logger.info("Admin %s reset password for user %s", current_user.username, user.username)
    return UserResetPasswordResponse(new_password=new_password)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    db.delete(user)
    db.commit()
    logger.info("Admin %s deleted user %s", current_user.username, user.username)
