"""Geometry and local-time helpers."""
from datetime import datetime, timezone

import pytest

from services.geo import bbox_around, haversine_m, parse_bbox, parse_latlng, within_bounds
from services.localtime import parse_local_input, to_input_value

TAS = [[-43.75, 143.70], [-39.50, 148.50]]


def test_parse_bbox_accepts_a_well_formed_box():
    box = parse_bbox("-43.1,146.9,-42.6,147.6")
    assert box.south == -43.1 and box.east == 147.6


@pytest.mark.parametrize("raw", [
    None, "", "1,2,3", "a,b,c,d", "1,2,3,4,5",
    "-42.6,146.9,-43.1,147.6",     # south > north
    "-43.1,147.6,-42.6,146.9",     # west > east (antimeridian wrap)
    "-200,0,200,0",                # out of range
])
def test_parse_bbox_rejects_junk(raw):
    assert parse_bbox(raw) is None


def test_haversine_matches_a_known_distance():
    # Hobart GPO to Launceston Post Office, ~164 km great-circle.
    metres = haversine_m(-42.8826, 147.3257, -41.4391, 147.1358)
    assert 160_000 < metres < 168_000


def test_bbox_around_encloses_the_circle():
    lat, lng, radius = -42.88, 147.33, 5000.0
    box = bbox_around(lat, lng, radius)

    assert box.contains(lat, lng)
    # The box must contain every point on the circle, so its half-height is at
    # least the radius.
    assert haversine_m(lat, lng, box.north, lng) >= radius
    assert haversine_m(lat, lng, lat, box.east) >= radius


def test_within_bounds():
    assert within_bounds(-42.88, 147.33, TAS)        # Hobart
    assert not within_bounds(-33.87, 151.21, TAS)    # Sydney
    assert not within_bounds(-41.29, 174.78, TAS)    # Wellington


@pytest.mark.parametrize("lat,lng", [("abc", "1"), (None, None), ("91", "0"), ("0", "181")])
def test_parse_latlng_rejects_junk(lat, lng):
    assert parse_latlng(lat, lng) is None


def test_parse_latlng_accepts_strings():
    assert parse_latlng("-42.88", "147.33") == (-42.88, 147.33)


def test_local_input_is_tasmanian_wall_time():
    # AEDT (UTC+11) in January.
    summer = parse_local_input("2026-01-15T09:00")
    assert summer.astimezone(timezone.utc).hour == 22
    assert summer.astimezone(timezone.utc).day == 14

    # AEST (UTC+10) in July.
    winter = parse_local_input("2026-07-15T09:00")
    assert winter.astimezone(timezone.utc).hour == 23
    assert winter.astimezone(timezone.utc).day == 14


def test_local_input_round_trips():
    assert to_input_value(parse_local_input("2026-03-02T17:45")) == "2026-03-02T17:45"


def test_local_input_rejects_junk():
    assert parse_local_input("not a date") is None
    assert parse_local_input("") is None
    assert parse_local_input(None) is None


def test_local_input_honours_an_explicit_offset():
    value = parse_local_input("2026-01-15T09:00+00:00")
    assert value == datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
