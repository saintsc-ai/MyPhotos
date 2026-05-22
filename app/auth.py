"""Session-cookie based auth.

Design choices:
- bcrypt directly (passlib has been flaky on recent bcrypt releases).
- Starlette's SessionMiddleware signs a small `user_id` cookie — no
  `sessions` table to garbage-collect.
- Seed `admin / admin` on first startup. The frontend nags to change it
  while the stored hash still matches "admin".
- One module file keeps the auth surface easy to audit.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from pydantic import Field

from .api.deps import get_db
from .models import Share, User
from .paths import DATA_DIR

SESSION_COOKIE = "myphotos_session"
SESSION_KEY = "user_id"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
SECRET_FILE = DATA_DIR / "session.secret"
SEED_PASSWORD = "admin"


# ----- secret / hashing -----

def get_session_secret() -> str:
    """Return the persistent secret used to sign session cookies.

    Generated on first run and stored in `data/session.secret` (gitignored
    along with the rest of `data/`). Rotating the secret invalidates every
    existing session.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        s = SECRET_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s
    s = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(s + "\n", encoding="utf-8")
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        # Windows / DSM share-permission setups can refuse chmod — best effort.
        pass
    return s


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def ensure_default_admin(db: Session) -> None:
    """Seed admin / admin if the users table is empty. Idempotent.

    Swallows exceptions so the API still boots when the migration hasn't
    been applied yet (login will then fail with a clear 500 until alembic
    upgrade head runs).
    """
    try:
        existing = db.execute(select(User).limit(1)).scalar_one_or_none()
    except Exception:
        return
    if existing is not None:
        return
    admin = User(
        username="admin",
        password_hash=hash_password(SEED_PASSWORD),
        is_admin=True,
    )
    db.add(admin)
    db.commit()


# ----- dependencies -----

def current_user(
    request: Request, db: Session = Depends(get_db)
) -> Optional[User]:
    uid = request.session.get(SESSION_KEY)
    if uid is None:
        return None
    return db.get(User, uid)


def require_auth(user: Optional[User] = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return user


def require_admin(user: User = Depends(require_auth)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
    return user


# ----- DTOs -----

class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    # True while the stored hash still matches the seed "admin" — the UI
    # uses this to nag for a password change after first login.
    must_change_password: bool


def _user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username,
        is_admin=u.is_admin,
        must_change_password=verify_password(SEED_PASSWORD, u.password_hash),
    )


# ----- routes -----

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(
    payload: LoginIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    u = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    if u is None or not verify_password(payload.password, u.password_hash):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "사용자명 또는 비밀번호가 올바르지 않습니다"
        )
    u.last_login_at = datetime.utcnow()
    db.commit()
    request.session[SESSION_KEY] = u.id
    return {"ok": True, "user": _user_out(u).model_dump()}


@router.post("/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(require_auth)) -> UserOut:
    return _user_out(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "현재 비밀번호가 맞지 않습니다"
        )
    if len(payload.new_password) < 4:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "새 비밀번호는 최소 4자 이상이어야 합니다"
        )
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


# ----- admin user management (router protected via require_admin in main.py) -----

class UserAdminOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$")
    password: str = Field(min_length=4, max_length=256)
    is_admin: bool = False


class UserPatchIn(BaseModel):
    password: Optional[str] = Field(default=None, min_length=4, max_length=256)
    is_admin: Optional[bool] = None


admin_users_router = APIRouter(prefix="/admin/users", tags=["admin", "users"])


def _count_admins(db: Session) -> int:
    return len(db.execute(select(User).where(User.is_admin.is_(True))).scalars().all())


@admin_users_router.get("", response_model=list[UserAdminOut])
def list_users(db: Session = Depends(get_db)) -> list[UserAdminOut]:
    rows = db.execute(select(User).order_by(User.id)).scalars().all()
    return [UserAdminOut.model_validate(u, from_attributes=True) for u in rows]


@admin_users_router.post("", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateIn, db: Session = Depends(get_db)
) -> UserAdminOut:
    existing = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 존재하는 사용자명입니다")
    u = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return UserAdminOut.model_validate(u, from_attributes=True)


@admin_users_router.patch("/{user_id}", response_model=UserAdminOut)
def update_user(
    user_id: int,
    payload: UserPatchIn,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
) -> UserAdminOut:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if payload.is_admin is not None and u.is_admin and not payload.is_admin:
        # Demoting an admin — refuse if it would leave zero admins, or if it's self.
        if u.id == current.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "자신의 관리자 권한은 해제할 수 없습니다"
            )
        if _count_admins(db) <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "마지막 관리자는 권한을 해제할 수 없습니다"
            )
    if payload.is_admin is not None:
        u.is_admin = payload.is_admin
    if payload.password is not None:
        u.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(u)
    return UserAdminOut.model_validate(u, from_attributes=True)


@admin_users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
) -> None:
    if user_id == current.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "자신의 계정은 삭제할 수 없습니다"
        )
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if u.is_admin and _count_admins(db) <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "마지막 관리자는 삭제할 수 없습니다"
        )
    # Detach any shares this user created so the FK doesn't block delete.
    orphans = db.execute(
        select(Share).where(Share.created_by_user_id == user_id)
    ).scalars().all()
    for s in orphans:
        s.created_by_user_id = None
    db.delete(u)
    db.commit()
