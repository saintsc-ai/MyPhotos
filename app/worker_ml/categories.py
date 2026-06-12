"""Curated CLIP zero-shot categories.

Each entry is (Korean tag, English prompt, similarity threshold). The
prompt is what CLIP actually scores against (CLIP was trained on English
captions, so English prompts give better matches than Korean ones), and
the threshold is the cosine-similarity cutoff above which we add the
tag to the photo.

Thresholds are conservative — better to miss some photos than to
mislabel obviously-wrong ones. Bumping a category's threshold down
gives more recall; bumping up gives more precision.

Future: expose this as an admin-editable table (settings tab → CLIP
categories) so the user can add '우리집', '바닷가' etc. with their
own prompts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClipCategory:
    name: str          # Korean tag label
    prompt: str        # English description CLIP scores against
    threshold: float   # cosine similarity cutoff (0..1)


CATEGORIES: list[ClipCategory] = [
    ClipCategory("풍경", "a landscape photograph of nature, mountains, sea, sky, or fields", 0.24),
    ClipCategory("음식", "a photograph of food, dishes, or meals on a table", 0.25),
    ClipCategory("셀카", "a selfie portrait of a person holding the camera", 0.26),
    ClipCategory("단체사진", "a group photo with several people posing together", 0.25),
    ClipCategory("문서", "a scan or screenshot of a document, receipt, or page of text", 0.27),
    ClipCategory("야경", "a night photograph of city lights, streetlamps, or stars", 0.24),
    ClipCategory("실내", "an indoor photograph taken inside a room or building", 0.23),
    ClipCategory("야외", "an outdoor photograph taken outside in nature or on the street", 0.23),
    ClipCategory("아이", "a photograph of a young child or baby", 0.25),
    ClipCategory("결혼", "a wedding photograph of bride and groom in formal attire", 0.27),
    ClipCategory("생일", "a birthday photograph with a cake and candles", 0.26),
    ClipCategory("바다", "a photograph of the ocean, beach, or seaside", 0.25),
    ClipCategory("산", "a mountain landscape photograph with peaks and valleys", 0.25),
    ClipCategory("꽃", "a close-up photograph of flowers or blossoms", 0.25),
    ClipCategory("스크린샷", "a screenshot of a computer screen or phone screen", 0.26),
]


# Mutually-exclusive category groups. Each category is scored independently
# against its own threshold, so a single photo can trip several scene labels
# at once — most often a near-tie like 실내 vs 야외 on a landscape, where both
# squeak past the cutoff. Within a group we keep only the single
# highest-scoring matched category and drop the rest; categories that appear
# in no group stay fully multi-label (a 바다 photo is still also 풍경, 야외).
#
# Default to the conflicts that are genuinely either/or:
#   실내/야외      — a shot is taken inside or outside, not both
#   셀카/단체사진   — one person holding the camera vs several posing
#   문서/스크린샷   — a scanned page vs a captured screen
#   바다/산        — seaside vs mountain terrain
#
# Overridable per-host via settings (ml.exclusive_category_groups). Keep this
# list as the shipped default — config.py imports it for MlConfig's default.
DEFAULT_EXCLUSIVE_GROUPS: list[list[str]] = [
    ["실내", "야외"],
    ["셀카", "단체사진"],
    ["문서", "스크린샷"],
    ["바다", "산"],
]
