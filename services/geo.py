"""Geometry helpers: bounding boxes, distance, and Tasmania's extent.

Deliberately plain trigonometry rather than PostGIS. The map is one Australian
state and the workload is "give me every point in this rectangle" — a composite
B-tree index on (lat, lng) answers that fine, and it keeps the app runnable on
SQLite locally and on Render's stock Postgres with no extensions to provision.
If the dataset ever outgrows that, the migration path is PostGIS + a GiST index,
and only this module and the query in pets.py need to change.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional

EARTH_RADIUS_M = 6_371_000.0


class BBox(NamedTuple):
    south: float
    west: float
    north: float
    east: float

    def contains(self, lat: float, lng: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lng <= self.east


def parse_bbox(raw: Optional[str]) -> Optional[BBox]:
    """Parse a "south,west,north,east" query parameter.

    Returns None for anything malformed — the caller then serves the default
    extent rather than erroring, because a bad bbox from a client is not worth
    a 400 that blanks the user's map.
    """
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        return None
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError:
        return None
    if not (-90 <= south <= north <= 90) or not (-180 <= west <= 180) or not (-180 <= east <= 180):
        return None
    # A map panned across the antimeridian would give west > east. Tasmania
    # cannot do that, so treat it as malformed rather than splitting the query.
    if west > east:
        return None
    return BBox(south, west, north, east)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# The box is a prefilter, so it must never clip a point the exact circle would
# keep. Rounding in the degrees<->metres conversion can leave the edge a few
# picometres inside the radius; a hair of margin makes "the box encloses the
# circle" true outright rather than true-to-within-epsilon. Costs a handful of
# extra candidate rows that haversine then discards.
_BBOX_MARGIN = 1.000_001


def bbox_around(lat: float, lng: float, radius_m: float) -> BBox:
    """A square bounding box enclosing the circle of ``radius_m`` about a point.

    Used to narrow a radius search to something the index can answer; the exact
    circle is then applied with ``haversine_m`` over the (much smaller) result.
    """
    radius_m *= _BBOX_MARGIN
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    coslat = math.cos(math.radians(lat))
    dlng = math.degrees(radius_m / (EARTH_RADIUS_M * coslat)) if abs(coslat) > 1e-6 else 180.0
    return BBox(lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def within_bounds(lat: float, lng: float, bounds) -> bool:
    """Is the point inside the configured ``[[s, w], [n, e]]`` extent?"""
    (south, west), (north, east) = bounds
    return south <= lat <= north and west <= lng <= east


def parse_latlng(lat_raw, lng_raw) -> Optional[tuple[float, float]]:
    """Coerce two form values to a coordinate pair, or None if they aren't one."""
    try:
        lat, lng = float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if math.isnan(lat) or math.isnan(lng):
        return None
    return lat, lng
