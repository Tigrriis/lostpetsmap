"""Photo processing: resize, re-encode, and build a square thumbnail.

Photos live in the database (see models.PetPhoto), so keeping them small is not
cosmetic — it is what stops a few hundred reports from filling a free-tier
Postgres. A phone photo arrives at 3-6 MB and leaves here at roughly 150-300 KB.
"""
from __future__ import annotations

import io
from typing import NamedTuple, Optional

from PIL import Image, ImageOps

MAX_DIM = 1280          # longest edge of the stored image, pixels
THUMB_DIM = 240         # square thumbnail edge, for map popups and lists
JPEG_QUALITY = 82
THUMB_QUALITY = 78


class ProcessedImage(NamedTuple):
    data: bytes
    thumb: bytes
    mimetype: str


def _load(raw: bytes) -> Optional[Image.Image]:
    try:
        img = Image.open(io.BytesIO(raw))
        # Rotate to match the EXIF orientation flag, then drop the flag — a
        # browser that ignores EXIF would otherwise show phone photos sideways.
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception:
        # Pillow raises a wide variety of things on a malformed or hostile file
        # (and DecompressionBombError on a pixel-flood). None of them should
        # reach the user as a 500 — an unreadable upload is a validation error.
        return None
    return _flatten(img)


def _flatten(img: Image.Image) -> Image.Image:
    """Convert to RGB, compositing any transparency onto white."""
    if img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def _encode(img: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def process_photo(raw: bytes) -> Optional[ProcessedImage]:
    """Return the resized image plus a square thumbnail, or None if invalid."""
    if not raw:
        return None
    img = _load(raw)
    if img is None:
        return None

    full = img.copy()
    full.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

    # Centre-cropped square: a mixture of portrait and landscape photos in a
    # grid of thumbnails looks broken unless they share an aspect ratio.
    thumb = ImageOps.fit(img, (THUMB_DIM, THUMB_DIM), Image.LANCZOS, centering=(0.5, 0.4))

    return ProcessedImage(_encode(full, JPEG_QUALITY), _encode(thumb, THUMB_QUALITY), "image/jpeg")


def process_single(raw: bytes) -> Optional[tuple[bytes, str]]:
    """Resize-only variant for sighting photos, which have no thumbnail."""
    if not raw:
        return None
    img = _load(raw)
    if img is None:
        return None
    img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    return _encode(img, JPEG_QUALITY), "image/jpeg"
