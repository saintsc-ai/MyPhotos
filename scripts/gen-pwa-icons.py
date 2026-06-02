#!/usr/bin/env python3
"""Generate placeholder PWA / home-screen icons with Pillow.

Writes app/web/static/icons/{icon-192,icon-512,icon-maskable-512,
apple-touch-icon}.png — a simple dark "photo" mark (sun + mountains on a
light card). Replace with real artwork anytime by dropping PNGs of the
same names/sizes, or re-run this:

    .venv/Scripts/python scripts/gen-pwa-icons.py     # Windows
    .venv/bin/python      scripts/gen-pwa-icons.py     # Linux/macOS
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join("app", "web", "static", "icons")
os.makedirs(OUT, exist_ok=True)

BG = (17, 20, 24)        # #111418 — matches the dark UI
CARD = (223, 231, 240)   # light photo card
SUN = (242, 193, 78)     # warm sun
MTN = (58, 111, 176)     # accent blue mountains


def draw_icon(size: int, pad_frac: float) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG + (255,))
    d = ImageDraw.Draw(img)
    pad = int(size * pad_frac)
    x0, y0, x1, y1 = pad, pad, size - pad, size - pad
    w, h = x1 - x0, y1 - y0
    # photo card
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(w * 0.12), fill=CARD)
    # scene inset inside the card
    inset = int(w * 0.10)
    ix0, iy0, ix1, iy1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    iw, ih = ix1 - ix0, iy1 - iy0
    # sun
    sr = int(iw * 0.13)
    sx, sy = ix0 + int(iw * 0.22), iy0 + int(ih * 0.24)
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=SUN)
    # mountains
    base = iy1
    d.polygon([(ix0, base), (ix0 + int(iw * 0.42), iy0 + int(ih * 0.45)),
               (ix0 + int(iw * 0.84), base)], fill=MTN)
    d.polygon([(ix0 + int(iw * 0.45), base), (ix0 + int(iw * 0.72), iy0 + int(ih * 0.58)),
               (ix1, base)], fill=MTN)
    return img


def main() -> None:
    draw_icon(192, 0.12).save(os.path.join(OUT, "icon-192.png"))
    draw_icon(512, 0.12).save(os.path.join(OUT, "icon-512.png"))
    # maskable: keep content inside the ~80% safe zone
    draw_icon(512, 0.20).save(os.path.join(OUT, "icon-maskable-512.png"))
    # apple-touch-icon: iOS rounds the corners itself
    draw_icon(180, 0.12).save(os.path.join(OUT, "apple-touch-icon.png"))
    print(f"wrote icons to {OUT}/")


if __name__ == "__main__":
    main()
