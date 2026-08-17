import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .db import get_db, verify_password
from .models import Session, User


bearer = HTTPBearer(auto_error=False)


def create_session(db: DbSession, email: str, password: str) -> tuple[str, User]:
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    db.execute(delete(Session).where(Session.expires_at < datetime.now(timezone.utc)))
    token = secrets.token_urlsafe(40)
    db.add(
        Session(
            token=token,
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours),
        )
    )
    db.commit()
    return token, user


def user_from_token(db: DbSession, token: str | None) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    row = db.scalar(select(Session).where(Session.token == token))
    now = datetime.now(timezone.utc)
    if not row or row.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    return row.user


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: DbSession = Depends(get_db),
) -> User:
    return user_from_token(db, credentials.credentials if credentials else None)


def event_user(token: str = Query(default=""), db: DbSession = Depends(get_db)) -> User:
    return user_from_token(db, token)
