"""First-run setup wizard endpoints.

Lets a brand-new install get from "just booted" to "logged in admin with
a sane password, a photo root, and ML models on disk" through a guided
web flow, without anyone ever needing to:

  - know the seed admin/admin credentials,
  - SSH to the host to hand-edit config,
  - run scripts/install-ml-models.sh from a shell,
  - or remember the `/api/admin/...` URL shapes.

The flow is opt-in for veterans (they can hit /login.html directly and
log in as `admin`/`admin` the old way) and forced for first-timers (the
gallery + login page both read /api/setup/status on load and bounce to
/setup.html when an admin still carries the seed password).

Endpoints:
  GET  /api/setup/status            — anonymous probe.
  POST /api/setup/admin             — anonymous; replaces the seed
                                      admin's password + auto-logs in.
  POST /api/setup/ml-models         — admin; starts the model download
                                      subprocess in the background.
  GET  /api/setup/ml-models/status  — admin; per-file progress for
                                      the polling UI.

Subsequent steps (adding a photo root, etc.) reuse the existing
`/api/admin/...` endpoints — the wizard's only job is to walk the user
through them in order. Setup is "complete" when no admin still has the
seed password; root + ML models are recommended but not required to
leave the wizard.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    SEED_PASSWORD,
    SESSION_KEY,
    hash_password,
    require_admin,
    verify_password,
)
from ..models import Root, User
from ..paths import DATA_DIR, PROJECT_ROOT
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


# ---------- ML model download ---------------------------------------

# Manifest of every file scripts/install-ml-models.sh drops on disk +
# the minimum byte size that script considers "downloaded" (matches the
# `min` argument to its fetch() helper). Used by the status endpoint to
# decide which files are present without running the script.
_ML_FILES: list[tuple[str, int]] = [
    ("yolo/yolov8n.onnx",       8_000_000),
    ("clip/vision_quantized.onnx", 20_000_000),
    ("clip/text_quantized.onnx",   20_000_000),
    ("clip/tokenizer.json",          500_000),
    ("face/yunet.onnx",              200_000),
    ("face/sface.onnx",           30_000_000),
]


class _MLState:
    """Process-wide handle to the running install-ml-models subprocess.

    Single instance — there's only ever one install in flight, and a
    second POST while one is running just gets the already-running
    handle. Survives request boundaries via being a module-level
    singleton; reset on process restart, which is fine: the script
    is idempotent (fetch() skips files already big enough).
    """

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.return_code: Optional[int] = None
        self.tail: list[str] = []           # last ~40 stdout/stderr lines
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return  # already running; idempotent
            script = PROJECT_ROOT / "scripts" / "install-ml-models.sh"
            if not script.exists():
                raise FileNotFoundError(str(script))
            # Pass the data dir through the env so the script writes
            # under MYPHOTOS_DATA/models (matches container layout).
            env = os.environ.copy()
            env.setdefault("MYPHOTOS_DATA", str(DATA_DIR))
            self.proc = subprocess.Popen(
                ["bash", str(script)],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,                       # line-buffered
            )
            self.started_at = time.time()
            self.finished_at = None
            self.return_code = None
            self.tail = []
            threading.Thread(
                target=self._drain, name="ml-install-drain", daemon=True
            ).start()

    def _drain(self) -> None:
        """Pump the subprocess output so its pipe buffer can't fill and
        block the download, and keep the last ~40 lines for the UI."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip()
            with self._lock:
                self.tail.append(line)
                # Trim from the front — bounded so a chatty curl can't
                # blow up the process memory.
                if len(self.tail) > 80:
                    del self.tail[:-80]
        rc = proc.wait()
        with self._lock:
            self.return_code = rc
            self.finished_at = time.time()


_ml_state = _MLState()


class MLFile(BaseModel):
    name: str                    # relative to data/models/
    size: int                    # bytes on disk, 0 if missing
    min_bytes: int               # threshold used by install script
    done: bool                   # size >= min_bytes


class MLStatus(BaseModel):
    running: bool
    return_code: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    files: list[MLFile]
    all_done: bool
    log_tail: list[str]          # last lines of subprocess stdout


@router.get("/ml-models/status", response_model=MLStatus)
def ml_models_status(_user: User = Depends(require_admin)) -> MLStatus:
    files = _scan_ml_files()
    return MLStatus(
        running=_ml_state.is_running(),
        return_code=_ml_state.return_code,
        started_at=_ml_state.started_at,
        finished_at=_ml_state.finished_at,
        files=files,
        all_done=all(f.done for f in files),
        # Snapshot the tail so the caller doesn't see it mutate
        # underneath them — small list, copying is fine.
        log_tail=list(_ml_state.tail),
    )


@router.post("/ml-models", response_model=MLStatus)
def ml_models_start(_user: User = Depends(require_admin)) -> MLStatus:
    """Kick off the bundled install-ml-models.sh in the background.

    Idempotent — calling it while a previous run is still in flight
    just returns the current status. The script itself skips files
    already on disk at full size, so re-running on a partial download
    only fills in what's missing.
    """
    try:
        _ml_state.start()
    except FileNotFoundError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"install-ml-models.sh not found at {e}",
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Failed to start subprocess: {e}",
        )
    return ml_models_status(_user=_user)


def _scan_ml_files() -> list[MLFile]:
    out: list[MLFile] = []
    models_dir = Path(DATA_DIR) / "models"
    for rel, min_bytes in _ML_FILES:
        p = models_dir / rel
        size = 0
        if p.exists():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
        out.append(MLFile(
            name=rel, size=size, min_bytes=min_bytes, done=size >= min_bytes,
        ))
    return out


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
