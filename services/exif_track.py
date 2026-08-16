"""Reconstruct a drone flight path from the GPS in its photos.

The DJI Matrice 4T writes latitude, longitude and a capture timestamp into the
EXIF of every image. Reading those and joining them in time order gives a
rough flight path — not the true telemetry, but an honest record of where the
aircraft actually imaged, which for a search is arguably the more useful claim.

**No image data is retained.** Each upload is parsed for its EXIF header and
then discarded; only coordinates and timestamps are stored. That keeps a
300-photo sortie down to a few kilobytes instead of a gigabyte, and means the
site never holds aerial imagery of anybody's property.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import NamedTuple

from PIL import Image, ExifTags

# EXIF tag ids, resolved by name so this does not depend on magic numbers.
_TAG = {name: num for num, name in ExifTags.TAGS.items()}
_GPSTAG = {name: num for num, name in ExifTags.GPSTAGS.items()}


class PhotoFix(NamedTuple):
    lat: float
    lng: float
    taken_at: datetime | None
    altitude_m: float | None


class ExifResult(NamedTuple):
    fixes: list[PhotoFix]
    skipped: list[str]        # "filename: reason", for honest reporting


def _ratio(value) -> float:
    """EXIF rationals arrive as Fraction-likes, tuples, or plain numbers."""
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1]) if value[1] else 0.0
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _dms_to_degrees(dms, ref: str | None) -> float | None:
    """Convert EXIF degrees/minutes/seconds plus a N/S/E/W reference."""
    try:
        degrees, minutes, seconds = (_ratio(v) for v in dms)
    except (TypeError, ValueError):
        return None
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if ref and str(ref).upper().strip() in ("S", "W"):
        value = -value
    return value


def _parse_timestamp(exif) -> datetime | None:
    """DateTimeOriginal, as UTC.

    EXIF timestamps carry no timezone, and DJI writes local camera time. The
    aircraft flies in Tasmania, so it is interpreted as Tasmanian wall time —
    the same assumption the rest of the app makes about times people type in.
    """
    raw = exif.get(_TAG.get("DateTimeOriginal")) or exif.get(_TAG.get("DateTime"))
    if not raw:
        return None
    try:
        naive = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    from services.localtime import TZ
    return naive.replace(tzinfo=TZ).astimezone(timezone.utc)


def read_fix(raw: bytes) -> PhotoFix | None:
    """Pull one photo's position out of its EXIF, or None if it has none."""
    try:
        img = Image.open(io.BytesIO(raw))
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None

    try:
        gps = exif.get_ifd(_TAG["GPSInfo"])
    except Exception:
        return None
    if not gps:
        return None

    lat = _dms_to_degrees(gps.get(_GPSTAG["GPSLatitude"]), gps.get(_GPSTAG["GPSLatitudeRef"]))
    lng = _dms_to_degrees(gps.get(_GPSTAG["GPSLongitude"]), gps.get(_GPSTAG["GPSLongitudeRef"]))
    if lat is None or lng is None:
        return None
    # A camera with GPS enabled but no fix writes zeros rather than omitting
    # the tag, which would otherwise plant the flight off the coast of Africa.
    if abs(lat) < 1e-9 and abs(lng) < 1e-9:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None

    altitude = None
    if _GPSTAG["GPSAltitude"] in gps:
        altitude = _ratio(gps[_GPSTAG["GPSAltitude"]])
        if gps.get(_GPSTAG.get("GPSAltitudeRef")) in (1, b"\x01"):
            altitude = -altitude

    return PhotoFix(lat, lng, _parse_timestamp(exif), altitude)


def fixes_from_uploads(uploads) -> ExifResult:
    """Read every upload, keep the positions, discard the pixels.

    Files are read one at a time and never held together, so a large sortie
    does not need the whole set resident at once.
    """
    fixes: list[PhotoFix] = []
    skipped: list[str] = []

    for upload in uploads:
        name = getattr(upload, "filename", "") or "photo"
        try:
            raw = upload.read()
        except Exception:
            skipped.append(f"{name}: could not be read")
            continue
        if not raw:
            skipped.append(f"{name}: empty file")
            continue

        fix = read_fix(raw)
        del raw                      # drop the image bytes immediately
        if fix is None:
            skipped.append(f"{name}: no GPS in EXIF")
            continue
        fixes.append(fix)

    # Time order, so the polyline joins them in the order they were taken.
    # Photos with no timestamp go last rather than being dropped — they still
    # carry a real position, which is what the coverage cells need.
    timed = sorted((f for f in fixes if f.taken_at), key=lambda f: f.taken_at)
    untimed = [f for f in fixes if not f.taken_at]
    return ExifResult(timed + untimed, skipped)


def to_points(fixes: list[PhotoFix]) -> list[list[float]]:
    """``[lat, lng, epoch_seconds]`` triples, the shape a track stores.

    Photos with no usable timestamp inherit the previous one, so the sequence
    stays monotonic and duration maths does not go backwards.
    """
    points: list[list[float]] = []
    last_ts = 0
    for fix in fixes:
        ts = int(fix.taken_at.timestamp()) if fix.taken_at else last_ts
        last_ts = ts
        points.append([fix.lat, fix.lng, ts])
    return points
