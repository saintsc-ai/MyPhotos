"""MyPhotos desktop app — PySide6 + QWebEngine viewer *and* local server manager.

Two things in one window:

1. **Viewer** — a Qt shell around the existing web frontend (QWebEngine
   handles auth/sessions). Point it at any MyPhotos server (remote NAS or
   the local one this app manages).

2. **Server manager** — start / stop / restart the three server processes
   (Web/API + indexing worker + ML worker) right from the desktop, watch
   their live logs, and see indexing progress (job queue + photo pipeline).
   This makes the desktop app a self-contained way to run MyPhotos on a
   Windows/Mac box without touching a terminal.

Minimise or close → the app keeps running in the system tray so the
managed workers don't die. Quit explicitly from the tray menu (which
stops every managed process first).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QProcess,
    QProcessEnvironment,
    QStandardPaths,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

try:  # stdlib on 3.11+, external on older
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None


APP_NAME = "MyPhotos"
HOME_PATH = "/"
IS_WINDOWS = sys.platform.startswith("win")


# ===================================================================
# config (server URL + local-server settings, per-user)
# ===================================================================

def _config_dir() -> Path:
    """%APPDATA%/MyPhotos on Windows, ~/.local/share/MyPhotos elsewhere."""
    base = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _config_path() -> Path:
    return _config_dir() / "config.json"


def load_config() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(d: dict) -> None:
    _config_path().write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- autodetect the project checkout + its venv python ----------

def autodetect_project_root() -> str:
    """Walk up from this file looking for the MyPhotos source tree
    (app/api/main.py + pyproject.toml). Works when run from source
    (desktop/app.py); returns '' for a frozen bundle where the source
    isn't alongside — the user sets it in the manager then."""
    here = Path(__file__).resolve()
    for base in [here.parent, *here.parents]:
        if (base / "app" / "api" / "main.py").exists() and (base / "pyproject.toml").exists():
            return str(base)
    return ""


def autodetect_python(root: str) -> str:
    """Prefer the project's own venv (it has FastAPI/onnxruntime/etc.).
    The desktop venv only has PySide6, so never default to that for the
    workers — fall back to the current interpreter only as a last resort."""
    if root:
        sub = "Scripts" if IS_WINDOWS else "bin"
        exe = "python.exe" if IS_WINDOWS else "python"
        cand = Path(root) / ".venv" / sub / exe
        if cand.exists():
            return str(cand)
    return sys.executable


def _read_local_toml(root: str) -> dict:
    """Best-effort read of config/local.toml for port + data_dir."""
    if not root or _toml is None:
        return {}
    p = Path(root) / "config" / "local.toml"
    if not p.exists():
        return {}
    try:
        with p.open("rb") as fh:
            return _toml.load(fh)
    except Exception:
        return {}


def detect_local_port(root: str) -> int:
    return int(_read_local_toml(root).get("server", {}).get("port", 8888) or 8888)


def detect_db_path(root: str) -> str:
    """Where catalog.db lives — honours [paths].data_dir override."""
    if not root:
        return ""
    data_dir = _read_local_toml(root).get("paths", {}).get("data_dir")
    base = Path(data_dir) if data_dir else Path(root) / "data"
    return str(base / "catalog.db")


def default_local_config() -> dict:
    root = autodetect_project_root()
    return {
        "project_root": root,
        "python": autodetect_python(root),
        "host": "127.0.0.1",
        "port": detect_local_port(root),
        "run_ml": True,
        "autostart": False,
    }


def is_local_url(url: str) -> bool:
    host = QUrl(url).host().lower()
    return host in ("127.0.0.1", "localhost", "::1", "")


def fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60}초"
    return f"{s // 3600}시간 {(s % 3600) // 60}분"


def prompt_server_url(parent=None, initial: str = "") -> str | None:
    text, ok = QInputDialog.getText(
        parent,
        f"{APP_NAME} — 서버 주소",
        "연결할 MyPhotos 서버 주소를 입력하세요\n"
        "(로컬 서버를 직접 운영하면 비워두면 자동으로 로컬을 가리킵니다)\n"
        "예: http://192.168.1.201:8888",
        text=initial or "http://192.168.1.201:8888",
    )
    if not ok:
        return None
    text = (text or "").strip().rstrip("/")
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    return text


# ===================================================================
# one managed server process (QProcess wrapper)
# ===================================================================

class ManagedProcess(QWidget):
    """A single supervised process + its own status row UI."""

    log = Signal(str)

    def __init__(self, key: str, label: str, args_fn, parent=None):
        super().__init__(parent)
        self.key = key
        self.label = label
        self._args_fn = args_fn          # () -> (program, [args], cwd, QProcessEnvironment)
        self._start_mono: float | None = None
        self._stopping = False
        self.last_exit_code: int | None = None

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._drain_output)
        self.proc.stateChanged.connect(lambda *_: self.refresh())
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

        self._build_row()
        self.refresh()

    # ---- UI row ----
    def _build_row(self) -> None:
        box = QHBoxLayout(self)
        box.setContentsMargins(8, 6, 8, 6)

        self.dot = QLabel("●")
        self.dot.setFixedWidth(16)
        box.addWidget(self.dot)

        name = QLabel(self.label)
        name.setMinimumWidth(110)
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        box.addWidget(name)

        self.status_lbl = QLabel("")
        self.status_lbl.setMinimumWidth(220)
        box.addWidget(self.status_lbl)

        box.addStretch(1)

        self.btn_start = QPushButton("시작")
        self.btn_stop = QPushButton("정지")
        self.btn_restart = QPushButton("재시작")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_restart.clicked.connect(self.restart)
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            b.setFixedWidth(72)
            box.addWidget(b)

    # ---- lifecycle ----
    def start(self) -> None:
        if self.proc.state() != QProcess.NotRunning:
            return
        try:
            program, args, cwd, env = self._args_fn()
        except Exception as e:  # bad config
            self.log.emit(f"[설정 오류] {e}")
            return
        if not program or not Path(program).exists():
            self.log.emit(f"[오류] python 실행 파일을 찾을 수 없습니다: {program}\n"
                          f"       서버 관리 화면의 '경로 설정'에서 프로젝트 venv python을 지정하세요.")
            return
        if not cwd or not Path(cwd).exists():
            self.log.emit(f"[오류] 프로젝트 폴더를 찾을 수 없습니다: {cwd}")
            return
        self.proc.setProgram(program)
        self.proc.setArguments(args)
        self.proc.setWorkingDirectory(cwd)
        self.proc.setProcessEnvironment(env)
        self._stopping = False
        self.last_exit_code = None
        self._start_mono = time.monotonic()
        self.log.emit(f"$ {program} {' '.join(args)}")
        self.proc.start()

    def stop(self) -> None:
        if self.proc.state() == QProcess.NotRunning:
            return
        self._stopping = True
        self.proc.terminate()
        if not self.proc.waitForFinished(3000):
            self.proc.kill()
            self.proc.waitForFinished(2000)

    def restart(self) -> None:
        self.stop()
        QTimer.singleShot(500, self.start)  # let the port free up

    def is_running(self) -> bool:
        return self.proc.state() == QProcess.Running

    # ---- status ----
    def status(self) -> tuple[str, str]:
        """(text, color)."""
        st = self.proc.state()
        if st == QProcess.Running:
            up = fmt_uptime(time.monotonic() - self._start_mono) if self._start_mono else ""
            return (f"실행 중 · PID {self.proc.processId()} · 업타임 {up}", "#2e9e44")
        if st == QProcess.Starting:
            return ("시작 중…", "#e8a33d")
        if not self._stopping and self.last_exit_code not in (None, 0):
            return (f"비정상 종료 (코드 {self.last_exit_code})", "#d64545")
        return ("정지됨", "#9aa0a6")

    def refresh(self) -> None:
        text, color = self.status()
        self.dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.status_lbl.setText(text)
        running = self.proc.state() != QProcess.NotRunning
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    # ---- process signals ----
    def _drain_output(self) -> None:
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            self.log.emit(line)

    def _on_finished(self, code: int, _status) -> None:
        self.last_exit_code = int(code)
        self.log.emit(f"[프로세스 종료] exit code = {code}")
        self.refresh()

    def _on_error(self, err) -> None:
        if err == QProcess.FailedToStart:
            self.log.emit("[오류] 프로세스를 시작하지 못했습니다 (python 경로/권한 확인).")
        self.refresh()


# ===================================================================
# controller — owns the three processes + builds their launch specs
# ===================================================================

class ServerController:
    def __init__(self, cfg_provider):
        self._cfg = cfg_provider  # () -> local config dict
        self.api = ManagedProcess("api", "Web / API", self._api_args)
        self.worker = ManagedProcess("worker", "인덱싱 워커", self._worker_args)
        self.ml = ManagedProcess("ml", "ML 워커", self._ml_args)
        self.procs = [self.api, self.worker, self.ml]

    # ---- shared launch environment ----
    def _env(self, python: str) -> QProcessEnvironment:
        env = QProcessEnvironment.systemEnvironment()
        # Make sure exiftool/ffmpeg are findable even when the GUI was
        # launched from Finder/Dock (which gives a minimal PATH), and put
        # the venv's bin dir first.
        extra = [str(Path(python).parent)]
        if sys.platform == "darwin":
            extra += ["/opt/homebrew/bin", "/usr/local/bin"]
        elif not IS_WINDOWS:
            extra += ["/usr/local/bin", "/usr/bin"]
        sep = ";" if IS_WINDOWS else ":"
        cur = env.value("PATH", "")
        env.insert("PATH", sep.join([e for e in extra if e] + ([cur] if cur else [])))
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONUTF8", "1")
        return env

    def _common(self):
        c = self._cfg()
        py = c.get("python") or sys.executable
        root = c.get("project_root") or ""
        return py, root, self._env(py)

    def _api_args(self):
        py, root, env = self._common()
        c = self._cfg()
        host = c.get("host", "127.0.0.1")
        port = str(c.get("port", 8888))
        return py, ["-m", "uvicorn", "app.api.main:app", "--host", host, "--port", port], root, env

    def _worker_args(self):
        py, root, env = self._common()
        return py, ["-m", "app.worker.main"], root, env

    def _ml_args(self):
        py, root, env = self._common()
        return py, ["-m", "app.worker_ml.main"], root, env

    # ---- bulk ops ----
    def start_all(self) -> None:
        self.api.start()
        # stagger the workers slightly so they don't all hit the DB at boot
        QTimer.singleShot(700, self.worker.start)
        if self._cfg().get("run_ml", True):
            QTimer.singleShot(1400, self.ml.start)

    def stop_all(self) -> None:
        for p in reversed(self.procs):
            p.stop()

    def restart_all(self) -> None:
        self.stop_all()
        QTimer.singleShot(800, self.start_all)

    def any_running(self) -> bool:
        return any(p.is_running() for p in self.procs)


# ===================================================================
# server manager screen
# ===================================================================

class ServerManagerWidget(QWidget):
    def __init__(self, controller: ServerController, cfg_provider, on_config_changed, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._cfg = cfg_provider
        self._on_config_changed = on_config_changed
        self._logs: dict[str, QPlainTextEdit] = {}
        self._build()

        # poll status + progress on a timer
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel("서버 관리")
        tf = title.font()
        tf.setPointSize(tf.pointSize() + 4)
        tf.setBold(True)
        title.setFont(tf)
        root.addWidget(title)

        # ---- config summary ----
        cfgbox = QGroupBox("로컬 서버 설정")
        cl = QHBoxLayout(cfgbox)
        self.cfg_lbl = QLabel("")
        self.cfg_lbl.setWordWrap(True)
        self.cfg_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cl.addWidget(self.cfg_lbl, 1)
        btn_paths = QPushButton("경로 설정")
        btn_paths.clicked.connect(self._edit_paths)
        cl.addWidget(btn_paths, 0, Qt.AlignTop)
        root.addWidget(cfgbox)

        # ---- global controls ----
        gl = QHBoxLayout()
        for label, slot in [
            ("▶ 전체 시작", self.controller.start_all),
            ("■ 전체 종료", self.controller.stop_all),
            ("↻ 전체 재시작", self.controller.restart_all),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            b.setMinimumHeight(34)
            gl.addWidget(b)
        gl.addStretch(1)
        root.addLayout(gl)

        # ---- process rows ----
        procbox = QGroupBox("프로세스")
        pv = QVBoxLayout(procbox)
        pv.setSpacing(0)
        for i, p in enumerate(self.controller.procs):
            if i:
                line = QFrame()
                line.setFrameShape(QFrame.HLine)
                line.setStyleSheet("color:#e0e0e0;")
                pv.addWidget(line)
            pv.addWidget(p)
        root.addWidget(procbox)

        # ---- progress ----
        prog = QGroupBox("인덱싱 진행 상태")
        pgrid = QGridLayout(prog)
        self.lbl_jobs = QLabel("작업 큐: —")
        self.lbl_photos = QLabel("사진: —")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        self.bar.setFormat("처리 완료 %p%")
        pgrid.addWidget(self.lbl_jobs, 0, 0)
        pgrid.addWidget(self.lbl_photos, 1, 0)
        pgrid.addWidget(self.bar, 2, 0)
        root.addWidget(prog)

        # ---- logs ----
        logbox = QGroupBox("로그")
        ll = QVBoxLayout(logbox)
        tabs = QTabWidget()
        for p in self.controller.procs:
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setMaximumBlockCount(5000)  # bound memory
            view.setStyleSheet("font-family: Menlo, Consolas, monospace; font-size: 11px;")
            self._logs[p.key] = view
            tabs.addTab(view, p.label)
            p.log.connect(lambda line, k=p.key: self._append_log(k, line))
        ll.addWidget(tabs)
        root.addWidget(logbox, 1)

        self._refresh_cfg_label()

    # ---- config editing ----
    def _refresh_cfg_label(self) -> None:
        c = self._cfg()
        py_ok = "✓" if c.get("python") and Path(c["python"]).exists() else "✗"
        root_ok = "✓" if c.get("project_root") and Path(c["project_root"]).exists() else "✗"
        self.cfg_lbl.setText(
            f"<b>프로젝트:</b> {root_ok} {c.get('project_root') or '(미설정)'}<br>"
            f"<b>Python:</b> {py_ok} {c.get('python') or '(미설정)'}<br>"
            f"<b>주소:</b> http://{c.get('host','127.0.0.1')}:{c.get('port',8888)}"
            f" &nbsp;·&nbsp; <b>ML 워커:</b> {'사용' if c.get('run_ml', True) else '사용 안 함'}"
        )

    def _edit_paths(self) -> None:
        c = self._cfg()
        root, ok = QInputDialog.getText(self, "프로젝트 폴더", "MyPhotos 소스 폴더 경로:",
                                        text=c.get("project_root", ""))
        if not ok:
            return
        py, ok = QInputDialog.getText(self, "Python", "프로젝트 venv의 python 경로:",
                                      text=c.get("python", ""))
        if not ok:
            return
        host, ok = QInputDialog.getText(self, "호스트", "바인딩 호스트:",
                                        text=c.get("host", "127.0.0.1"))
        if not ok:
            return
        port, ok = QInputDialog.getInt(self, "포트", "포트:", c.get("port", 8888), 1, 65535)
        if not ok:
            return
        c["project_root"] = root.strip()
        c["python"] = py.strip()
        c["host"] = host.strip() or "127.0.0.1"
        c["port"] = int(port)
        self._on_config_changed()
        self._refresh_cfg_label()

    # ---- live updates ----
    def _append_log(self, key: str, line: str) -> None:
        view = self._logs.get(key)
        if view is not None:
            view.appendPlainText(line)

    def _tick(self) -> None:
        for p in self.controller.procs:
            p.refresh()
        self._poll_progress()

    def _poll_progress(self) -> None:
        import sqlite3
        db = detect_db_path(self._cfg().get("project_root", ""))
        if not db or not os.path.exists(db):
            self.lbl_jobs.setText("작업 큐: (DB 없음 — 아직 초기화 전이거나 외부 DB 사용)")
            self.lbl_photos.setText("사진: —")
            return
        try:
            uri = Path(db).as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                jobs = dict(con.execute(
                    "SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
                total, exif_ok, thumb_ok, cls_ok = con.execute(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN exif_status='ok' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN thumb_status='ok' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN classify_status='ok' THEN 1 ELSE 0 END) "
                    "FROM photos").fetchone()
            finally:
                con.close()
        except Exception as e:
            self.lbl_jobs.setText(f"작업 큐: (읽기 실패: {e})")
            return

        queued = jobs.get("queued", 0)
        running = jobs.get("running", 0)
        done = jobs.get("done", 0)
        failed = jobs.get("failed", 0)
        self.lbl_jobs.setText(
            f"작업 큐:  대기 {queued}   ·   실행 중 {running}   ·   완료 {done}   ·   실패 {failed}"
        )
        self.lbl_photos.setText(
            f"사진:  총 {total or 0}   ·   EXIF {exif_ok or 0}   ·   썸네일 {thumb_ok or 0}"
            f"   ·   분류 {cls_ok or 0}"
        )
        pending = queued + running
        denom = pending + done
        self.bar.setValue(int(done * 100 / denom) if denom else 100)


# ===================================================================
# web viewer page — block /admin only for remote servers
# ===================================================================

class _RestrictedPage(QWebEnginePage):
    """Block /admin* navigation for *remote* servers (admin belongs in a
    real browser there). When the viewer points at the locally-managed
    server, allow it — a standalone local user needs root/user setup."""

    def __init__(self, profile, parent, allow_admin_fn):
        super().__init__(profile, parent)
        self._allow_admin = allow_admin_fn

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if (
            is_main_frame
            and url.path().lower().startswith("/admin")
            and not self._allow_admin()
        ):
            QMessageBox.information(
                self.view().window() if self.view() else None,
                "관리 페이지",
                "원격 서버의 관리 기능은 웹 브라우저에서 사용해주세요.\n"
                f"브라우저로 {url.toString()} 열어보세요.",
            )
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


# ===================================================================
# main window
# ===================================================================

class MainWindow(QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.local = cfg["local"]
        self.server_url = cfg["server_url"]
        self._really_quitting = False
        self._tray_hint_shown = False

        self.setWindowTitle(APP_NAME)
        self.resize(1320, 860)

        self.controller = ServerController(lambda: self.local)

        # persistent web profile (login cookies survive restarts)
        storage = _config_dir() / "qweb-storage"
        cache = _config_dir() / "qweb-cache"
        storage.mkdir(exist_ok=True)
        cache.mkdir(exist_ok=True)
        self.profile = QWebEngineProfile(APP_NAME, self)
        self.profile.setPersistentStoragePath(str(storage))
        self.profile.setCachePath(str(cache))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)

        self.page = _RestrictedPage(self.profile, self, lambda: is_local_url(self.server_url))
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        self.page.loadFinished.connect(self._inject_desktop_styles)
        self.page.titleChanged.connect(self._on_title_changed)

        self.manager = ServerManagerWidget(
            self.controller, lambda: self.local, self._persist_config, self
        )

        # central: stack [viewer, manager]
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.view)      # 0
        self.stack.addWidget(self.manager)   # 1
        self.setCentralWidget(self.stack)

        self._build_toolbar()
        self._build_tray()

        # reload the viewer automatically once the local API comes up
        self.controller.api.proc.stateChanged.connect(self._maybe_reload_on_api_up)

        # start on the manager screen if we're driving a local server,
        # otherwise jump straight to the gallery
        if is_local_url(self.server_url):
            self._show_manager()
            if self.local.get("autostart"):
                self.controller.start_all()
        else:
            self._show_viewer()
            self.go_home()

    # ---- toolbar ----
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_gallery = QAction("🖼 갤러리", self)
        self.act_gallery.setCheckable(True)
        self.act_gallery.triggered.connect(self._show_viewer)
        tb.addAction(self.act_gallery)

        self.act_manager = QAction("🖥 서버 관리", self)
        self.act_manager.setCheckable(True)
        self.act_manager.triggered.connect(self._show_manager)
        tb.addAction(self.act_manager)

        tb.addSeparator()
        for label, slot, tip in [
            ("←", self.view.back, "뒤로"),
            ("→", self.view.forward, "앞으로"),
            ("↻", self.view.reload, "새로고침"),
            ("⌂", self.go_home, "홈"),
        ]:
            act = QAction(label, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            tb.addAction(act)

        tb.addSeparator()
        change = QAction("서버 변경", self)
        change.setToolTip("연결 서버 주소를 바꿉니다 (재시작 없이 적용)")
        change.triggered.connect(self.change_server)
        tb.addAction(change)

        clear = QAction("쿠키 삭제", self)
        clear.setToolTip("로그인 세션을 지워서 다시 로그인하도록 합니다")
        clear.triggered.connect(self.clear_cookies)
        tb.addAction(clear)

    # ---- tray ----
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()
        menu.addAction("열기", self._show_normal)
        menu.addSeparator()
        menu.addAction("▶ 전체 시작", self.controller.start_all)
        menu.addAction("■ 전체 종료", self.controller.stop_all)
        menu.addAction("↻ 전체 재시작", self.controller.restart_all)
        menu.addSeparator()
        menu.addAction("종료", self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_normal()

    def _show_normal(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---- view switching ----
    def _show_viewer(self) -> None:
        self.stack.setCurrentIndex(0)
        self.act_gallery.setChecked(True)
        self.act_manager.setChecked(False)
        if not self.view.url().isValid() or self.view.url().isEmpty():
            self.go_home()

    def _show_manager(self) -> None:
        self.stack.setCurrentIndex(1)
        self.act_gallery.setChecked(False)
        self.act_manager.setChecked(True)

    # ---- viewer plumbing ----
    def go_home(self) -> None:
        self.view.load(QUrl(self.server_url + HOME_PATH))

    def _maybe_reload_on_api_up(self, state) -> None:
        if state == QProcess.Running and is_local_url(self.server_url):
            # give uvicorn a moment to bind, then load the gallery
            QTimer.singleShot(1500, self.go_home)

    def change_server(self) -> None:
        new = prompt_server_url(self, self.server_url)
        if new and new != self.server_url:
            self.server_url = new
            self._persist_config()
            self._show_viewer()
            self.go_home()

    def clear_cookies(self) -> None:
        if QMessageBox.question(
            self, "쿠키 삭제",
            "현재 로그인 세션을 모두 지웁니다. 다음 페이지부터 다시 로그인해야 합니다.\n계속할까요?",
        ) != QMessageBox.Yes:
            return
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        self.go_home()

    def _on_title_changed(self, title: str) -> None:
        self.setWindowTitle(f"{title} — {APP_NAME}" if title else APP_NAME)

    def _inject_desktop_styles(self, ok: bool) -> None:
        if not ok:
            return
        # Hide the in-page 관리 link only when admin is blocked (remote).
        if is_local_url(self.server_url):
            return
        self.page.runJavaScript(
            "(function(){var s=document.createElement('style');"
            "s.textContent='#link-admin{display:none !important}';"
            "document.head.appendChild(s);})();"
        )

    # ---- config persistence ----
    def _persist_config(self) -> None:
        self.cfg["server_url"] = self.server_url
        self.cfg["local"] = self.local
        save_config(self.cfg)

    # ---- window close / minimise → tray ----
    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange and self.isMinimized() and self.tray:
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def closeEvent(self, event):
        if self._really_quitting or not self.tray:
            self.controller.stop_all()
            event.accept()
            return
        event.ignore()
        self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        self.hide()
        if self.tray and not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(
                APP_NAME,
                "트레이에서 계속 실행 중입니다. 워커는 계속 동작합니다.\n"
                "완전히 종료하려면 트레이 아이콘 → 종료.",
                QSystemTrayIcon.Information,
                4000,
            )

    def _quit_app(self) -> None:
        running = self.controller.any_running()
        if running and QMessageBox.question(
            self, "종료",
            "실행 중인 서버/워커를 모두 정지하고 종료합니다. 계속할까요?",
        ) != QMessageBox.Yes:
            return
        self._really_quitting = True
        self.controller.stop_all()
        if self.tray:
            self.tray.hide()
        QApplication.quit()


# ===================================================================
# entry point
# ===================================================================

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    # closing the window must NOT quit — we keep running in the tray
    app.setQuitOnLastWindowClosed(False)

    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    cfg = load_config()
    # merge in any newly-added local-config keys on upgrade
    local = {**default_local_config(), **cfg.get("local", {})}
    cfg["local"] = local
    cfg.setdefault("server_url", f"http://{local['host']}:{local['port']}")
    save_config(cfg)

    win = MainWindow(cfg)
    # stop managed processes if the app is asked to quit by any path
    app.aboutToQuit.connect(win.controller.stop_all)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
