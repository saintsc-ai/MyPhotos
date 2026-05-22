"""Per-thread persistent exiftool subprocess (`-stay_open True`).

Cold-starting `exiftool` for every photo dominates indexing throughput on
RAW-heavy libraries: each invocation pays ~150–300 ms just to boot Perl
and load the ExifTool script before reading a single byte of the image.
With `-stay_open` we keep the interpreter resident, dropping repeat-call
cost to ~10 ms.

Design:
- One subprocess **per worker thread** (thread-local). Sharing one process
  across threads would force us to synchronise stdin/stdout reads, which
  would erase most of the win.
- Lazy start on first use; if the process dies (crash, OOM, etc.) the
  next call transparently spawns a new one.
- Only used for the metadata path (JSON output) — binary preview
  extraction for RAW still uses one-shot subprocess (rarer, and the
  binary-with-end-marker protocol is fiddly).

Threads should call `shutdown_thread()` on exit to terminate the child
process cleanly; the dispatcher's worker loop wires this up.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from typing import Optional

from ..external import exiftool_path

log = logging.getLogger(__name__)

_local = threading.local()
_READY = b"{ready}"


def _spawn() -> Optional[subprocess.Popen]:
    tool = exiftool_path()
    if not tool:
        return None
    try:
        return subprocess.Popen(
            [
                tool,
                "-stay_open", "True",
                "-@", "-",
                # File path and metadata strings are UTF-8 (NFC normalised
                # in our scanner). exiftool's default depends on the OS.
                "-common_args", "-charset", "utf8", "-charset", "filename=utf8",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as e:
        log.warning("exiftool stay_open spawn failed: %s", e)
        return None


def _get() -> Optional[subprocess.Popen]:
    proc = getattr(_local, "proc", None)
    if proc is not None and proc.poll() is None:
        return proc
    proc = _spawn()
    _local.proc = proc
    return proc


def _exec_text(args: list[str]) -> Optional[str]:
    """Send a single command to the persistent process, return its text
    output (captured up to the `{ready}` marker)."""
    proc = _get()
    if proc is None:
        return None
    try:
        cmd = ("\n".join(args) + "\n-execute\n").encode("utf-8")
        proc.stdin.write(cmd)
        proc.stdin.flush()
        chunks: list[bytes] = []
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("exiftool stdout closed unexpectedly")
            if line.rstrip(b"\r\n") == _READY:
                break
            chunks.append(line)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except (OSError, BrokenPipeError, RuntimeError) as e:
        log.warning("exiftool persistent pipe died (%s); will restart", e)
        try:
            proc.kill()
        except Exception:
            pass
        _local.proc = None
        return None


def fetch_metadata(path: str, tags: list[str]) -> Optional[dict]:
    """Run `exiftool -j -n <tags> <path>` via the persistent process.

    Returns the parsed dict (first element of exiftool's JSON array) or
    None on failure / unavailability.
    """
    args = ["-json", "-n"] + tags + [path]
    out = _exec_text(args)
    if out is None:
        return None
    try:
        data = json.loads(out)
        return data[0] if data else None
    except (json.JSONDecodeError, IndexError):
        return None


def shutdown_thread() -> None:
    """Terminate the per-thread exiftool subprocess. Safe to call repeatedly."""
    proc = getattr(_local, "proc", None)
    if proc and proc.poll() is None:
        try:
            proc.stdin.write(b"-stay_open\nFalse\n")
            proc.stdin.flush()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _local.proc = None
