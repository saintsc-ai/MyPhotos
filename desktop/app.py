"""MyPhotos desktop client — PySide6 + QWebEngine wrapper.

Thin Qt shell around the existing web frontend so a user can run
MyPhotos as a regular Windows app instead of opening a browser tab.
QWebEngine handles auth/sessions/everything — this file is just the
window chrome, the per-host server URL prompt, persistent cookie
storage, and a navigation guard that blocks /admin (admin features
stay in the browser, by user choice).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QToolBar,
)


APP_NAME = "MyPhotos"
HOME_PATH = "/"


# ---------- config (server URL, per-user) ----------

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


def prompt_server_url(parent=None, initial: str = "") -> str | None:
    text, ok = QInputDialog.getText(
        parent,
        f"{APP_NAME} — 서버 주소",
        "MyPhotos 서버 주소를 입력하세요\n(예: http://192.168.1.201:8888)",
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


# ---------- web page subclass: block /admin navigation ----------

class _RestrictedPage(QWebEnginePage):
    """Block navigation to /admin*. Admin features (root setup, user
    management, share purge, etc.) belong in a regular browser — the
    desktop shell focuses on the viewer experience."""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: D401
        if is_main_frame and url.path().lower().startswith("/admin"):
            QMessageBox.information(
                self.view().window() if self.view() else None,
                "관리 페이지",
                "관리 기능은 웹 브라우저에서 사용해주세요.\n"
                f"브라우저로 {url.toString()} 열어보세요.",
            )
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self, server_url: str):
        super().__init__()
        self.server_url = server_url
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)

        # Persistent profile — login cookies + cache survive restarts so
        # the user doesn't re-login every time the app launches.
        profile_storage = _config_dir() / "qweb-storage"
        profile_cache = _config_dir() / "qweb-cache"
        profile_storage.mkdir(exist_ok=True)
        profile_cache.mkdir(exist_ok=True)

        self.profile = QWebEngineProfile(APP_NAME, self)
        self.profile.setPersistentStoragePath(str(profile_storage))
        self.profile.setCachePath(str(profile_cache))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.ForcePersistentCookies
        )

        self.page = _RestrictedPage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)

        # Hide the 관리 link in the user-menu after each page load —
        # injected CSS, no changes to index.html needed.
        self.page.loadFinished.connect(self._inject_desktop_styles)
        # Reflect the current page <title> in the window title bar so
        # the user can see which photo/album they're on at a glance.
        self.page.titleChanged.connect(self._on_title_changed)

        self._build_toolbar()
        self.go_home()

    def _build_toolbar(self) -> None:
        tb = QToolBar("Navigation", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        for label, slot, tip in [
            ("←",       self.view.back,     "뒤로 (Alt+Left)"),
            ("→",       self.view.forward,  "앞으로 (Alt+Right)"),
            ("↻",       self.view.reload,   "새로고침 (F5)"),
            ("⌂",       self.go_home,       "홈"),
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

    def go_home(self) -> None:
        self.view.load(QUrl(self.server_url + HOME_PATH))

    def change_server(self) -> None:
        new = prompt_server_url(self, self.server_url)
        if new and new != self.server_url:
            self.server_url = new
            save_config({"server_url": new})
            self.go_home()

    def clear_cookies(self) -> None:
        if QMessageBox.question(
            self,
            "쿠키 삭제",
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
        # CSS-only — hides the 관리 link in index.html's user-menu so
        # the desktop client stays a viewer. The link is still in the
        # DOM (admin endpoints exist on the server for browser users).
        self.page.runJavaScript(
            "(function(){"
            "var s=document.createElement('style');"
            "s.textContent='#link-admin{display:none !important}';"
            "document.head.appendChild(s);"
            "})();"
        )


# ---------- entry point ----------

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    # Icon — falls back silently if the file isn't bundled.
    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    cfg = load_config()
    server = cfg.get("server_url")
    if not server:
        server = prompt_server_url(initial="http://192.168.1.201:8888")
        if not server:
            return 0
        save_config({"server_url": server})

    win = MainWindow(server)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
