"""First-run setup wizard endpoints.

Lets a brand-new install get from "just booted" to "logged in admin with
a sane password and at least one photo root" through a guided web flow,
without anyone ever needing to:

  - know the seed admin/admin credentials,
  - SSH to the host to hand-edit config,
  - or remember the `/api/admin/...` URL shapes.

The flow is opt-in for veterans (they can hit /login.html directly and
log in as `admin`/`admin` the old way) and forced for first-timers (the
gallery + login page both read /api/setup/status on load and bounce to
/setup.html when an admin still carries the seed password).

Endpoints:
  GET  /api/setup/status   — anonymous. Tells the client which steps are
                             still pending.
  POST /api/setup/admin    — anonymous, but only allowed while at least
                             one admin still has the seed password.
                             Replaces that user's password + display_name
                             and logs the caller in (session cookie).

Subsequent steps (adding a photo root, etc.) reuse the existing
`/api/admin/...` endpoints — the wizard's only job is to walk the user
through them in order. Setup is "complete" when no admin still has the
seed password; root creation is recommended but not required to leave
the wizard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    SEED_PASSWORD,
    SESSION_KEY,
    hash_password,
    verify_password,
)
from ..models import Root, User
from .deps import get_db

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupStatus(BaseModel):
    needs_admin: bool = Field(
        description=(
            "True when at least one admin still has the seed password. "
            "The wizard MUST be completed before normal use — the seed "
            "is a public default and anyone on the network could log in."
        )
    )
    needs_root: bool = Field(
        description=(
            "True when zero enabled photo roots exist. The wizard offers "
            "to add one but doesn't force it — admin can also add roots "
            "later from the admin UI."
        )
    )
    complete: bool = Field(
        description="True when needs_admin is False (root is optional)."
    )


@router.get("/status", response_model=SetupStatus)
def setup_status(db: Session = Depends(get_db)) -> SetupStatus:
    """Anonymous probe. Cheap on a fresh install (one count + one read)."""
    needs_admin = _any_seed_admin(db)
    # Pre-bootstrap installs that haven't run ensure_default_admin yet
    # also need the wizard — User table empty.
    if not needs_admin:
        if db.execute(select(User).limit(1)).scalar_one_or_none() is None:
            needs_admin = True
    needs_root = db.execute(
        select(Root).where(Root.enabled.is_(True)).limit(1)
    ).scalar_one_or_none() is None
    return SetupStatus(
        needs_admin=needs_admin,
        needs_root=needs_root,
        complete=not needs_admin,
    )


class SetupAdminIn(BaseModel):
    # Required — what the wizard's password field commits to. Length
    # floor mirrors login UI; no upper bound (bcrypt truncates at 72).
    password: str = Field(min_length=8, max_length=128)
    # Optional — default keeps "관리자" (the seed display_name). The
    # username always stays "admin" so an existing session/bookmark
    # referencing it doesn't break.
    display_name: str | None = Field(default=None, max_length=64)


class SetupAdminOut(BaseModel):
    username: str
    display_name: str


@router.post("/admin", response_model=SetupAdminOut)
def setup_admin(
    body: SetupAdminIn,
    request: Request,
    db: Session = Depends(get_db),
) -> SetupAdminOut:
    """Replace the seed admin's password (and optionally display name).

    Anonymous on purpose — the user can't log in with the seed for
    obvious reasons, so requiring auth here would chicken-and-egg the
    whole flow. Locked behind `_any_seed_admin` so it doesn't double as
    an arbitrary password-reset endpoint after first use.

    Auto-logs the caller in so the next wizard step (root creation,
    which hits an admin-only endpoint) just works.
    """
    if not _any_seed_admin(db):
        # Either someone already finished setup or there are no admins
        # at all — neither case is something this endpoint should handle.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "초기 설정이 이미 완료되었습니다. 로그인 페이지로 이동하세요.",
        )

    # If the seed user got renamed away from "admin" somehow but kept
    # the seed password (manual SQL), match by hash rather than
    # username so the wizard still works.
    seed_admin = _find_seed_admin(db)
    if seed_admin is None:
        # Race: another tab finished the wizard between status() above
        # and now. Tell the client to refresh.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "초기 설정이 이미 완료되었습니다.",
        )

    seed_admin.password_hash = hash_password(body.password)
    if body.display_name and body.display_name.strip():
        seed_admin.display_name = body.display_name.strip()
    db.commit()
    db.refresh(seed_admin)

    # Auto-login. Same session shape POST /api/auth/login produces, so
    # the rest of the app treats this exactly like a normal login.
    request.session[SESSION_KEY] = seed_admin.id

    return SetupAdminOut(
        username=seed_admin.username,
        display_name=seed_admin.display_name or seed_admin.username,
    )


# ---------- helpers --------------------------------------------------


def _any_seed_admin(db: Session) -> bool:
    """True if at least one admin still has the seed password hash."""
    return _find_seed_admin(db) is not None


def _find_seed_admin(db: Session) -> User | None:
    """Iterate admins and return the first whose hash matches the seed
    password. Bcrypt verify is ~50 ms each so on a real install (1–2
    admins) the total cost is invisible; this only runs on the setup
    pages."""
    rows = db.execute(select(User).where(User.is_admin.is_(True))).scalars().all()
    for u in rows:
        if verify_password(SEED_PASSWORD, u.password_hash):
            return u
    return None
