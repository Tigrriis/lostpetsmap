"""Search-track geometry: storage, trimming, and coverage cells.

A search track — someone walking with their phone, or a drone flight
reconstructed from photo positions — is a list of ``[lat, lng, epoch_seconds]``
triples. This module is everything that turns those raw fixes into something
publishable.

Two ideas do most of the work:

**Trimming.** A track almost always starts and ends where the searcher parked
or lives. Both ends are cut *before storage*, so the app never holds the exact
copy in the first place. Hiding it only at render time would leave the real
thing one bug away from public.

**Cells.** Public coverage is a set of grid cells, not the GPS line. It answers
the question people actually have ("has this street been covered?"), it merges
across searchers for free, and it does not publish a trace of anyone's
movements. The grid is state-wide and keyed to a fixed reference latitude, so
two searchers walking the same street snap to the same cells rather than to two
interleaved sets.
"""
from __future__ import annotations

import json
import math
import zlib
from typing import Iterable, Sequence

METRES_PER_DEG_LAT = 111_320.0

# Points are rounded before storage: 6 dp is ~11 cm, far finer than consumer
# GPS, and it keeps the compressed blob small.
COORD_DP = 6

Point = Sequence[float]      # [lat, lng, epoch_seconds]
Cell = tuple[int, int]       # (ix, iy) on the shared grid


# ── Storage ────────────────────────────────────────────────────────────────
# JSON + zlib rather than an encoded polyline: polylines carry no timestamps,
# and "when was this searched?" is half the point of the feature. The blob is
# roughly 10 KB for a two-hour walk, which is cheap enough not to optimise.

def encode_points(points: Iterable[Point]) -> bytes:
    compact = [[round(p[0], COORD_DP), round(p[1], COORD_DP), int(p[2])] for p in points]
    return zlib.compress(json.dumps(compact, separators=(",", ":")).encode("utf-8"), 6)


def decode_points(blob: bytes | None) -> list[list[float]]:
    if not blob:
        return []
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except (zlib.error, ValueError):
        # A corrupt blob must not take down the page it appears on. An empty
        # track renders as "no coverage", which is honest about what we have.
        return []


def encode_cells(cells: Iterable[Cell]) -> bytes:
    ordered = sorted(set(cells))
    return zlib.compress(json.dumps(ordered, separators=(",", ":")).encode("utf-8"), 6)


def decode_cells(blob: bytes | None) -> list[Cell]:
    if not blob:
        return []
    try:
        return [(int(c[0]), int(c[1])) for c in json.loads(zlib.decompress(blob).decode("utf-8"))]
    except (zlib.error, ValueError, TypeError, IndexError):
        return []


# ── Distance ───────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))


def path_length_m(points: Sequence[Point]) -> float:
    return sum(_haversine_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
               for i in range(1, len(points)))


# ── Trimming ───────────────────────────────────────────────────────────────

def trim_ends(points: Sequence[Point], trim_m: float,
              max_fraction: float = 0.25) -> list[list[float]]:
    """Drop the first and last ``trim_m`` of path, by distance travelled.

    A short walk would vanish entirely under a fixed trim, so the cut is capped
    at ``max_fraction`` of the total length per end — half the path always
    survives. Below three points there is nothing meaningful to trim towards,
    so the track comes back empty rather than as a misleading fragment.
    """
    if len(points) < 3:
        return []

    total = path_length_m(points)
    if total <= 0:
        return []
    cut = min(trim_m, total * max_fraction)
    if cut <= 0:
        return [list(p) for p in points]

    # Cumulative distance at each point.
    cumulative = [0.0]
    for i in range(1, len(points)):
        cumulative.append(cumulative[-1] +
                          _haversine_m(points[i - 1][0], points[i - 1][1],
                                       points[i][0], points[i][1]))

    keep = [i for i, d in enumerate(cumulative) if cut <= d <= total - cut]
    if len(keep) < 2:
        return []
    return [list(points[i]) for i in keep]


# ── Coverage grid ──────────────────────────────────────────────────────────

def _grid_steps(cell_m: float, ref_lat: float) -> tuple[float, float]:
    """Cell size in degrees. Fixed reference latitude keeps the grid uniform.

    Deriving the longitude step from each point's own latitude would give a
    grid whose columns drift with latitude, so two searchers a few kilometres
    apart would land on cells that do not line up.
    """
    dlat = cell_m / METRES_PER_DEG_LAT
    dlng = cell_m / (METRES_PER_DEG_LAT * math.cos(math.radians(ref_lat)))
    return dlat, dlng


def cell_for(lat: float, lng: float, cell_m: float, ref_lat: float) -> Cell:
    dlat, dlng = _grid_steps(cell_m, ref_lat)
    return (math.floor(lng / dlng), math.floor(lat / dlat))


def cells_for_path(points: Sequence[Point], cell_m: float, ref_lat: float) -> set[Cell]:
    """Every cell the path passes through, including ones only crossed.

    Consecutive fixes can be tens of metres apart on foot and hundreds from a
    drone, so each segment is walked in half-cell steps. Marking only the fixes
    themselves would leave holes in a coverage map — reporting a street as
    unsearched when somebody walked straight down it.
    """
    covered: set[Cell] = set()
    if not points:
        return covered

    covered.add(cell_for(points[0][0], points[0][1], cell_m, ref_lat))
    step = max(cell_m / 2.0, 1.0)

    for i in range(1, len(points)):
        lat1, lng1 = points[i - 1][0], points[i - 1][1]
        lat2, lng2 = points[i][0], points[i][1]
        span = _haversine_m(lat1, lng1, lat2, lng2)
        steps = int(span // step)
        for s in range(1, steps + 1):
            f = s / (steps + 1)
            covered.add(cell_for(lat1 + (lat2 - lat1) * f,
                                 lng1 + (lng2 - lng1) * f, cell_m, ref_lat))
        covered.add(cell_for(lat2, lng2, cell_m, ref_lat))

    return covered


def cell_bounds(cell: Cell, cell_m: float, ref_lat: float) -> list[float]:
    """``[south, west, north, east]`` for a cell — what Leaflet draws."""
    dlat, dlng = _grid_steps(cell_m, ref_lat)
    ix, iy = cell
    return [iy * dlat, ix * dlng, (iy + 1) * dlat, (ix + 1) * dlng]


def cells_in_bbox(cells: Iterable[Cell], bbox, cell_m: float, ref_lat: float) -> list[Cell]:
    """Filter cells to those overlapping a ``(south, west, north, east)`` box."""
    dlat, dlng = _grid_steps(cell_m, ref_lat)
    south, west, north, east = bbox
    ix_lo, ix_hi = math.floor(west / dlng), math.floor(east / dlng)
    iy_lo, iy_hi = math.floor(south / dlat), math.floor(north / dlat)
    return [c for c in cells if ix_lo <= c[0] <= ix_hi and iy_lo <= c[1] <= iy_hi]


def bbox_of(points: Sequence[Point]) -> tuple[float, float, float, float] | None:
    """``(south, west, north, east)`` of a path, or None if it has no points."""
    if not points:
        return None
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return (min(lats), min(lngs), max(lats), max(lngs))
