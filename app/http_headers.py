"""HTTP header helpers.

ASGI header values must be latin-1 encodable. A non-ASCII filename (e.g.
Korean) interpolated straight into a `Content-Disposition` value crashes
Starlette's `value.encode("latin-1")` with UnicodeEncodeError, so the whole
response fails to send — which is why Korean-named videos/downloads broke
even after a successful transcode. Build the header per RFC 6266 instead:
an ASCII-only `filename="…"` fallback plus the real name in
`filename*=UTF-8''…` (percent-encoded), which modern browsers prefer.
"""
from __future__ import annotations

from urllib.parse import quote


def _ascii_fallback(name: str) -> str:
    """Latin-1/ASCII-safe stand-in for `filename="…"`: keep printable ASCII
    (minus the quote/backslash that would break the quoted-string), replace
    everything else with '_'. Never empty."""
    out = "".join(
        c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name
    )
    return out or "file"


def content_disposition(disposition: str, filename: str) -> str:
    """Return a latin-1-safe `Content-Disposition` header value.

    `disposition` is "inline" or "attachment". The result carries both an
    ASCII fallback and the UTF-8 `filename*` form so non-ASCII names survive.
    """
    return (
        f"{disposition}; filename=\"{_ascii_fallback(filename)}\"; "
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
