"""Search-track geometry: trimming, the coverage grid, and storage."""
import math

import pytest

from services.coverage import (
    bbox_of, cell_bounds, cell_for, cells_for_path, cells_in_bbox,
    decode_cells, decode_points, encode_cells, encode_points, path_length_m,
    trim_ends,
)

CELL_M = 50.0
REF_LAT = -42.15


def walk(lat0, lng0, n, step_deg=0.0002):
    """A straight north-bound path of n points, ~22 m apart."""
    return [[lat0 + i * step_deg, lng0, 1_760_000_000 + i * 10] for i in range(n)]


# ── Storage ────────────────────────────────────────────────────────────────

def test_points_round_trip():
    points = walk(-42.88, 147.33, 5)
    assert decode_points(encode_points(points)) == [
        [round(p[0], 6), round(p[1], 6), int(p[2])] for p in points]


def test_corrupt_blobs_degrade_to_empty_rather_than_raising():
    """A bad blob must not take down the page it appears on."""
    assert decode_points(b"not zlib") == []
    assert decode_cells(b"not zlib") == []
    assert decode_points(None) == []


def test_cells_round_trip_and_dedupe():
    blob = encode_cells([(3, 4), (1, 2), (3, 4)])
    assert decode_cells(blob) == [(1, 2), (3, 4)]


# ── Trimming ───────────────────────────────────────────────────────────────

def test_trim_removes_both_ends():
    """The searcher's front door is at each end of the walk."""
    points = walk(-42.88, 147.33, 200)          # ~4.4 km
    trimmed = trim_ends(points, 200.0)

    assert len(trimmed) < len(points)
    assert trimmed[0] != points[0]
    assert trimmed[-1] != points[-1]

    from services.geo import haversine_m
    start_gap = haversine_m(points[0][0], points[0][1], trimmed[0][0], trimmed[0][1])
    end_gap = haversine_m(points[-1][0], points[-1][1], trimmed[-1][0], trimmed[-1][1])
    assert start_gap >= 180 and end_gap >= 180        # ~200 m, within one step


def test_short_walk_trims_by_fraction_not_flat_distance():
    """A 300 m walk must not vanish under a 200 m trim from each end.

    The fraction cap targets keeping half, but the cut snaps to whole points
    and always rounds towards discarding more — so with fixes ~22 m apart a
    290 m walk keeps around 111 m. Erring towards over-trimming is the right
    direction for a privacy control, so this asserts a floor, not an equality.
    """
    points = walk(-42.88, 147.33, 14)           # ~290 m
    trimmed = trim_ends(points, 200.0, max_fraction=0.25)

    assert trimmed, "a short walk should still publish something"
    assert len(trimmed) < len(points)
    assert path_length_m(trimmed) >= path_length_m(points) / 3


def test_trim_refuses_paths_too_small_to_be_meaningful():
    assert trim_ends([], 200.0) == []
    assert trim_ends([[-42.88, 147.33, 1]], 200.0) == []
    # A path that never moved has no ends to trim towards.
    assert trim_ends([[-42.88, 147.33, 1], [-42.88, 147.33, 2],
                      [-42.88, 147.33, 3]], 200.0) == []


def test_a_two_fix_path_still_survives_trimming():
    """A weak GPS lock can report a whole walk as two fixes.

    Snapping the cut to whole points discarded the entire path in that case,
    which is what left four production tracks with a measured distance and no
    coverage. The cut is interpolated within the segment instead.
    """
    # Two fixes, ~280 m apart — the shape of the track that failed.
    points = [[-42.8800, 147.3300, 1_760_000_000],
              [-42.8825, 147.3300, 1_760_000_660]]
    total = path_length_m(points)
    assert 250 < total < 300

    trimmed = trim_ends(points, 50.0)
    assert len(trimmed) >= 2, "a 280 m walk must publish something"
    assert path_length_m(trimmed) == pytest.approx(total - 100, abs=1.0)

    from services.geo import haversine_m
    assert haversine_m(points[0][0], points[0][1], trimmed[0][0], trimmed[0][1]) \
        == pytest.approx(50, abs=1.0)
    assert haversine_m(points[-1][0], points[-1][1], trimmed[-1][0], trimmed[-1][1]) \
        == pytest.approx(50, abs=1.0)


def test_two_fix_path_produces_coverage_cells():
    """The end the user actually sees: cells on the map, not an empty track."""
    points = [[-42.8800, 147.3300, 0], [-42.8825, 147.3300, 660]]
    cells = cells_for_path(trim_ends(points, 50.0), CELL_M, REF_LAT)
    assert len(cells) >= 3


def test_trim_never_returns_the_original_endpoints():
    """The property that actually matters, across a range of lengths."""
    for n in (5, 20, 100, 500):
        points = walk(-42.88, 147.33, n)
        trimmed = trim_ends(points, 200.0)
        if trimmed:
            assert trimmed[0] != points[0]
            assert trimmed[-1] != points[-1]


# ── Coverage grid ──────────────────────────────────────────────────────────

def test_nearby_points_share_a_cell():
    a = cell_for(-42.8800, 147.3300, CELL_M, REF_LAT)
    b = cell_for(-42.88003, 147.33003, CELL_M, REF_LAT)   # ~4 m away
    assert a == b


def test_points_a_few_hundred_metres_apart_do_not():
    a = cell_for(-42.8800, 147.3300, CELL_M, REF_LAT)
    b = cell_for(-42.8830, 147.3300, CELL_M, REF_LAT)     # ~330 m away
    assert a != b


def test_grid_is_shared_across_searchers():
    """Two people walking the same street must land on the same cells.

    Deriving the longitude step from each point's own latitude would give a
    grid that drifts, so coverage from different searchers would interleave
    instead of merging.
    """
    north = cells_for_path(walk(-41.44, 147.14, 20), CELL_M, REF_LAT)
    north_again = cells_for_path(walk(-41.44, 147.14, 20), CELL_M, REF_LAT)
    assert north == north_again

    # Cell edges are multiples of the step from the origin, everywhere.
    dlat = CELL_M / 111_320.0
    for lat in (-40.5, -42.15, -43.4):
        ix, iy = cell_for(lat, 147.0, CELL_M, REF_LAT)
        south = cell_bounds((ix, iy), CELL_M, REF_LAT)[0]
        assert math.isclose(south / dlat, round(south / dlat), abs_tol=1e-6)


def test_path_covers_cells_it_only_passes_through():
    """Consecutive fixes can straddle a cell; skipping it leaves a hole.

    A drone at 15 m/s photographing every few seconds jumps far more than one
    cell, and reporting an overflown street as unsearched would be wrong.
    """
    sparse = [[-42.8800, 147.3300, 0], [-42.8890, 147.3300, 60]]   # ~1 km apart
    cells = cells_for_path(sparse, CELL_M, REF_LAT)

    # ~1 km of 50 m cells is about 20; endpoints alone would give 2.
    assert len(cells) >= 15
    # And they form an unbroken column — no gaps.
    ys = sorted(c[1] for c in cells)
    assert ys == list(range(ys[0], ys[0] + len(ys)))


def test_cell_bounds_are_about_the_configured_size():
    bounds = cell_bounds(cell_for(-42.88, 147.33, CELL_M, REF_LAT), CELL_M, REF_LAT)
    south, west, north, east = bounds

    from services.geo import haversine_m
    height = haversine_m(south, west, north, west)
    width = haversine_m(south, west, south, east)
    assert 45 < height < 55
    assert 45 < width < 60          # widens slightly away from the reference lat


def test_cells_in_bbox_filters():
    cells = cells_for_path(walk(-42.88, 147.33, 100), CELL_M, REF_LAT)
    tight = cells_in_bbox(cells, (-42.881, 147.329, -42.879, 147.331), CELL_M, REF_LAT)
    assert 0 < len(tight) < len(cells)


def test_bbox_of():
    assert bbox_of([]) is None
    assert bbox_of([[-42.9, 147.3, 0], [-42.8, 147.4, 1]]) == (-42.9, 147.3, -42.8, 147.4)


@pytest.mark.parametrize("n", [0, 1])
def test_cells_for_tiny_paths_do_not_raise(n):
    cells_for_path(walk(-42.88, 147.33, n), CELL_M, REF_LAT)
