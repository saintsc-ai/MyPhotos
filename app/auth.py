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

import functools
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from pydantic import Field

from . import audit
from .api.deps import get_db
from .config import get_settings
from .models import Share, User
from .paths import DATA_DIR

SESSION_COOKIE = "myphotos_session"
SESSION_KEY = "user_id"
SESSION_PW_KEY = "pw"
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


PASSWORD_MIN_LEN = 8
_BCRYPT_MAX_BYTES = 72


def validate_new_password(password: str, username: str | None = None) -> None:
    """Policy for every path that sets a password (change, admin create /
    reset, setup wizard). 400 with a user-facing message on failure.

    The seed password is refused outright: an admin still on "admin" makes
    the anonymous setup wizard hand the account to whoever asks first.
    bcrypt 5 raises past 72 bytes (a Korean passphrase reaches that at 24
    chars), so cap there instead of returning a 500.
    """
    if len(password) < PASSWORD_MIN_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"비밀번호는 최소 {PASSWORD_MIN_LEN}자 이상이어야 합니다",
        )
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "비밀번호가 너무 깁니다 (UTF-8 기준 72바이트 이하)",
        )
    lowered = password.lower()
    if lowered == SEED_PASSWORD or (username and lowered == username.lower()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "사용자명이나 기본 비밀번호는 쓸 수 없습니다",
        )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


# Login against a missing user still runs one bcrypt so response time
# doesn't reveal which usernames exist.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode("utf-8")


@functools.lru_cache(maxsize=256)
def is_seed_hash(password_hash: str) -> bool:
    """True when the hash is of the seed password. Memoised by hash string
    (the answer for a given hash never changes) so the anonymous
    /api/setup/status probe and every /api/auth/me don't each burn a
    bcrypt — that was an unauthenticated CPU-exhaustion lever."""
    return verify_password(SEED_PASSWORD, password_hash)


def session_fingerprint(password_hash: str) -> str:
    """Short digest of the password hash stored in the session cookie.
    Changing the password changes the hash, so every cookie issued before
    the change stops validating — the signed cookie itself is stateless
    and would otherwise stay good for SESSION_MAX_AGE."""
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:16]


def start_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session[SESSION_KEY] = user.id
    request.session[SESSION_PW_KEY] = session_fingerprint(user.password_hash)


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
        display_name="관리자",
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
    u = db.get(User, uid)
    if u is None:
        return None
    # Cookie must have been issued against the current password; a
    # missing fingerprint (pre-upgrade cookie) also fails → one re-login.
    if request.session.get(SESSION_PW_KEY) != session_fingerprint(u.password_hash):
        return None
    return u


def require_auth(user: Optional[User] = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return user


def require_admin(user: User = Depends(require_auth)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin required")
    return user


# Per-user permission flag dependencies (P1). Each one wraps require_auth
# and rejects with 403 when the named flag is False. Admin bypasses
# the check entirely — admins have every flag implicitly.
#
# Usage in a router:
#
#     @router.post("/something")
#     def do_it(user: User = Depends(require_can_share), db = ...):
#         ...
#
# The helpers are pre-baked dependencies (not factories) so FastAPI can
# resolve them in one shot and Swagger displays them properly.

def _check_flag(user: User, attr: str, label: str) -> User:
    if user.is_admin:
        return user
    if not getattr(user, attr, False):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"권한 없음: {label}",
        )
    return user


def require_can_upload(user: User = Depends(require_auth)) -> User:
    return _check_flag(user, "can_upload", "사진 업로드")


def require_can_delete(user: User = Depends(require_auth)) -> User:
    return _check_flag(user, "can_delete", "사진 삭제")


def require_can_share(user: User = Depends(require_auth)) -> User:
    return _check_flag(user, "can_share", "공유링크 생성")


def require_can_edit_meta_others(user: User = Depends(require_auth)) -> User:
    return _check_flag(user, "can_edit_meta_others", "다른 사진 메타 편집")


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
    display_name: str
    is_admin: bool
    # Per-user permission flags echoed back so the frontend can hide
    # buttons the user can't actually use (upload card, delete chip,
    # share menu, etc.). Admin gets True for all four implicitly.
    can_upload: bool
    can_delete: bool
    can_share: bool
    can_edit_meta_others: bool
    # True while the stored hash still matches the seed "admin" — the UI
    # uses this to nag for a password change after first login.
    must_change_password: bool


def _user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        is_admin=u.is_admin,
        can_upload=bool(u.is_admin or u.can_upload),
        can_delete=bool(u.is_admin or u.can_delete),
        can_share=bool(u.is_admin or u.can_share),
        can_edit_meta_others=bool(u.is_admin or u.can_edit_meta_others),
        must_change_password=is_seed_hash(u.password_hash),
    )


# ----- routes -----

router = APIRouter(prefix="/auth", tags=["auth"])


# Per-IP login throttle. Sliding window of recent failed-login timestamps
# keyed by client IP — too many failures in the window → 429 until they
# fall off. Cheap in-process dict (single API process is the common case);
# for multi-worker deployments this drifts but each worker still applies
# the cap independently, which is enough to slow online brute force from
# unusable to "needs a botnet."
import collections as _collections
_LOGIN_FAIL_WINDOW_S = 300        # 5 minutes
_LOGIN_FAIL_MAX = 8               # per IP per window
_login_failures: dict[str, _collections.deque] = {}


_LOGIN_FAIL_MAX_IPS = 10_000      # bound memory under IP-rotating floods


def _client_ip(request: Request) -> str:
    # X-Forwarded-For is client-controlled: honour it only when an operator
    # has declared a trusted reverse proxy in front ([security]
    # trust_proxy_xff, same switch as the GeoIP gate). Trusting it on a
    # directly exposed uvicorn let a random header per request bypass the
    # per-IP cap entirely and forge the audit-log ip.
    if get_settings().security.trust_proxy_xff:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_throttle_check(ip: str) -> None:
    now = datetime.utcnow().timestamp()
    q = _login_failures.get(ip)
    if q is None:
        return
    # Drop expired entries.
    while q and (now - q[0]) > _LOGIN_FAIL_WINDOW_S:
        q.popleft()
    if len(q) >= _LOGIN_FAIL_MAX:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
        )


def _login_throttle_record_failure(ip: str) -> None:
    if ip not in _login_failures and len(_login_failures) >= _LOGIN_FAIL_MAX_IPS:
        # Evict entries whose newest failure is outside the window; if the
        # map is still full, drop the oldest-inserted one.
        now = datetime.utcnow().timestamp()
        for k in [k for k, q in _login_failures.items()
                  if not q or now - q[-1] > _LOGIN_FAIL_WINDOW_S]:
            del _login_failures[k]
        if len(_login_failures) >= _LOGIN_FAIL_MAX_IPS:
            del _login_failures[next(iter(_login_failures))]
    q = _login_failures.setdefault(ip, _collections.deque(maxlen=_LOGIN_FAIL_MAX * 2))
    q.append(datetime.utcnow().timestamp())


def _login_throttle_clear(ip: str) -> None:
    _login_failures.pop(ip, None)


@router.post("/login")
def login(
    payload: LoginIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    ip = _client_ip(request)
    _login_throttle_check(ip)
    sec = get_settings().security
    now = datetime.utcnow()
    u = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()

    # Account lockout: refuse (even with the right password) while locked.
    if u is not None and u.locked_until is not None and u.locked_until > now:
        audit.record(db, u, "auth.login_locked", "user", u.id, {"ip": ip})
        db.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "계정이 일시적으로 잠겼습니다. 잠시 후 다시 시도하세요.",
        )

    if u is None:
        verify_password(payload.password, _DUMMY_HASH)   # equalise timing
    if u is None or not verify_password(payload.password, u.password_hash):
        _login_throttle_record_failure(ip)
        locked_now = False
        if u is not None and sec.lockout_threshold > 0 and sec.lockout_minutes > 0:
            u.failed_login_count = (u.failed_login_count or 0) + 1
            if u.failed_login_count >= sec.lockout_threshold:
                u.locked_until = now + timedelta(minutes=sec.lockout_minutes)
                u.failed_login_count = 0   # reset the counter once locked
                locked_now = True
        audit.record(
            db, u, "auth.login_failed", "user",
            (u.id if u is not None else None),
            {"ip": ip, "username": payload.username, "locked": locked_now},
        )
        db.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "사용자명 또는 비밀번호가 올바르지 않습니다"
        )

    _login_throttle_clear(ip)
    u.failed_login_count = 0
    u.locked_until = None
    u.last_login_at = now
    audit.record(db, u, "auth.login_ok", "user", u.id, {"ip": ip})
    db.commit()
    # Session fixation defence: drop any pre-existing session payload
    # (a hostile actor who set the victim's cookie pre-login would
    # otherwise keep that cookie value post-login).
    start_session(request, u)
    return {"ok": True, "user": _user_out(u).model_dump()}


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    resp = JSONResponse({"ok": True})
    # Drop cached thumbnails/originals (HTTP cache + service-worker
    # Cache Storage) so the next person on a shared browser can't pull
    # this user's photos from cache.
    resp.headers["Clear-Site-Data"] = '"cache", "storage"'
    return resp


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(require_auth)) -> UserOut:
    return _user_out(user)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    # Same per-IP throttle as login: a stolen session must not be able to
    # brute-force the real password through this endpoint.
    ip = _client_ip(request)
    _login_throttle_check(ip)
    if not verify_password(payload.old_password, user.password_hash):
        _login_throttle_record_failure(ip)
        audit.record(db, user, "auth.password_change_failed", "user", user.id, {"ip": ip})
        db.commit()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "현재 비밀번호가 맞지 않습니다"
        )
    validate_new_password(payload.new_password, user.username)
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    # The new hash changes the session fingerprint, so every cookie issued
    # before this (other devices, a leaked one) stops working; re-issue
    # this browser's.
    start_session(request, user)
    return {"ok": True}


# ----- admin user management (router protected via require_admin in main.py) -----

class UserAdminOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_admin: bool
    can_upload: bool
    can_delete: bool
    can_share: bool
    can_edit_meta_others: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$")
    # Required real name — login `username` is ASCII-only, so this is how
    # the admin records who the account is for. Korean allowed.
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)   # policy: validate_new_password
    is_admin: bool = False
    # Per-user flags at create time. Default true so the admin doesn't
    # have to immediately PATCH a newly-created family member just to
    # let them comment/rate; admin can flip off later.
    can_upload: bool = True
    can_delete: bool = True
    can_share: bool = True
    can_edit_meta_others: bool = True


class UserPatchIn(BaseModel):
    password: Optional[str] = Field(default=None, min_length=1, max_length=256)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    is_admin: Optional[bool] = None
    can_upload: Optional[bool] = None
    can_delete: Optional[bool] = None
    can_share: Optional[bool] = None
    can_edit_meta_others: Optional[bool] = None


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
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "이름을 입력하세요")
    validate_new_password(payload.password, payload.username)
    u = User(
        username=payload.username,
        display_name=display_name,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
        can_upload=payload.can_upload,
        can_delete=payload.can_delete,
        can_share=payload.can_share,
        can_edit_meta_others=payload.can_edit_meta_others,
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
        validate_new_password(payload.password, u.username)
        u.password_hash = hash_password(payload.password)
    if payload.display_name is not None:
        new_name = payload.display_name.strip()
        if not new_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "이름을 입력하세요")
        u.display_name = new_name
    # Per-user permission flags. is_admin doesn't need any of these set —
    # admin bypasses every flag check — but persisting whatever value
    # the admin sent keeps the UI checkbox state honest if they later
    # demote the user.
    if payload.can_upload is not None:
        u.can_upload = payload.can_upload
    if payload.can_delete is not None:
        u.can_delete = payload.can_delete
    if payload.can_share is not None:
        u.can_share = payload.can_share
    if payload.can_edit_meta_others is not None:
        u.can_edit_meta_others = payload.can_edit_meta_others
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
