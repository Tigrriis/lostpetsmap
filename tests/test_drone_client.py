"""Drone import via client-parsed coordinates.

The browser reads each photo's EXIF and posts only the numbers, so a sortie
that would be gigabytes of imagery is a few kilobytes of JSON. These cover the
server half of that: the JSON contract, its validation of untrusted input, and
that the timestamp rule stays identical to the upload path.
"""
import json
from datetime import timezone

from conftest import login, make_pet

from extensions import db
from models import SearchTrack
from services.exif_track import fixes_from_client, to_points


def flight(n=6, lat0=-42.8700, lng0=147.3200):
    return [{"lat": lat0 + i * 0.002, "lng": lng0,
             "taken": f"2026:08:16 13:{i:02d}:00", "alt": 60}
            for i in range(n)]


# ── Parsing client fixes ───────────────────────────────────────────────────

def test_client_fixes_are_ordered_by_capture_time(app):
    out_of_order = [
        {"lat": -42.88, "lng": 147.33, "taken": "2026:08:16 13:05:00"},
        {"lat": -42.87, "lng": 147.33, "taken": "2026:08:16 13:01:00"},
        {"lat": -42.86, "lng": 147.33, "taken": "2026:08:16 13:03:00"},
    ]
    fixes = fixes_from_client(out_of_order).fixes
    assert [f.lat for f in fixes] == [-42.87, -42.86, -42.88]


def test_client_timestamps_are_read_as_tasmanian_local(app):
    """Same rule as the upload path — August is AEST, UTC+10."""
    fix = fixes_from_client([{"lat": -42.88, "lng": 147.33,
                              "taken": "2026:08:16 13:00:00"}]).fixes[0]
    assert fix.taken_at.astimezone(timezone.utc).hour == 3
    assert fix.taken_at.astimezone(timezone.utc).day == 16


def test_client_fixes_reject_junk_without_failing_the_batch(app):
    result = fixes_from_client([
        {"lat": -42.88, "lng": 147.33},
        {"lat": "not a number", "lng": 147.33},
        {"lng": 147.33},
        {"lat": 999, "lng": 147.33},
        "not even an object",
        {"lat": -42.87, "lng": 147.33},
    ])
    assert len(result.fixes) == 2
    assert len(result.skipped) == 4


def test_client_fixes_reject_a_non_list(app):
    assert fixes_from_client({"lat": 1}).fixes == []
    assert fixes_from_client(None).fixes == []


def test_untimed_fixes_keep_their_position(app):
    """A photo with no timestamp still carries a real coordinate."""
    fixes = fixes_from_client([
        {"lat": -42.88, "lng": 147.33, "taken": "2026:08:16 13:00:00"},
        {"lat": -42.87, "lng": 147.33},
    ]).fixes
    assert len(fixes) == 2
    points = to_points(fixes)
    assert points[1][2] == points[0][2]      # inherits the previous stamp


# ── The endpoint ───────────────────────────────────────────────────────────

def test_json_import_creates_a_flight(app, client, user):
    pet = make_pet(user)
    login(client)
    resp = client.post(f"/pets/{pet.id}/tracks/drone",
                       json={"fixes": flight(), "notes": "Thermal grid 60 m"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "never uploaded" in body["message"]

    track = SearchTrack.query.filter_by(source="drone").one()
    assert track.is_published
    assert track.cell_count > 0
    assert track.notes == "Thermal grid 60 m"


def test_json_import_derives_the_flight_window_from_photo_times(app, client, user):
    pet = make_pet(user)
    login(client)
    client.post(f"/pets/{pet.id}/tracks/drone", json={"fixes": flight(n=6)})

    track = SearchTrack.query.filter_by(source="drone").one()
    started = track.started_at.replace(tzinfo=timezone.utc) if track.started_at.tzinfo is None \
        else track.started_at
    finished = track.finished_at.replace(tzinfo=timezone.utc) if track.finished_at.tzinfo is None \
        else track.finished_at
    # 13:00 to 13:05 local, i.e. a five-minute window.
    assert (finished - started).total_seconds() == 300
    assert started.astimezone(timezone.utc).hour == 3


def test_json_import_needs_two_usable_fixes(app, client, user):
    pet = make_pet(user)
    login(client)
    resp = client.post(f"/pets/{pet.id}/tracks/drone", json={"fixes": flight(n=1)})
    assert resp.status_code == 400
    assert "at least two" in resp.get_json()["error"] or \
           "fewer than two" in resp.get_json()["error"]
    assert SearchTrack.query.count() == 0


def test_json_import_ignores_fixes_outside_tasmania(app, client, user):
    pet = make_pet(user)
    login(client)
    fixes = flight(n=3) + [{"lat": -33.87, "lng": 151.21, "taken": "2026:08:16 13:09:00"}]
    resp = client.post(f"/pets/{pet.id}/tracks/drone", json={"fixes": fixes})

    assert "1 were outside Tasmania" in resp.get_json()["message"]
    assert SearchTrack.query.one().max_lat < -42


def test_json_import_requires_login(app, client, user):
    pet = make_pet(user)
    resp = client.post(f"/pets/{pet.id}/tracks/drone", json={"fixes": flight()})
    assert resp.status_code in (302, 401)
    assert SearchTrack.query.count() == 0


def test_a_sortie_of_positions_stays_tiny(app, client, user):
    """The whole point: 300 frames of imagery become a few KB on the wire."""
    pet = make_pet(user)
    login(client)
    fixes = [{"lat": -42.87 + (i % 40) * 0.0004, "lng": 147.32 + (i // 40) * 0.0006,
              "taken": f"2026:08:16 13:{i // 60:02d}:{i % 60:02d}", "alt": 60}
             for i in range(300)]
    payload = json.dumps({"fixes": fixes})
    assert len(payload) < 40 * 1024, "300 positions should be well under 40 KB"

    resp = client.post(f"/pets/{pet.id}/tracks/drone", json={"fixes": fixes})
    assert resp.status_code == 200
    assert SearchTrack.query.one().cell_count > 0
