"""Search tracks end to end: recording, publication, privacy, drone import."""
import io
import json

from conftest import login, make_pet, make_user
from PIL import Image

from extensions import db
from models import SearchTrack
from services.coverage import decode_points


def walk_points(n=200, lat0=-42.8800, lng0=147.3300):
    """~4.4 km north-bound, the shape a real phone would send."""
    return [[lat0 + i * 0.0002, lng0, 1_760_000_000 + i * 10] for i in range(n)]


def record_track(client, pet_id, points=None, notes=""):
    """Start, append, and finish a track the way the browser does."""
    started = client.post(f"/pets/{pet_id}/tracks", json={"source": "on_foot"})
    track_id = started.get_json()["track_id"]
    client.post(f"/tracks/{track_id}/points", json={"points": points or walk_points()})
    client.post(f"/tracks/{track_id}/finish", json={"notes": notes})
    return track_id


# ── Lifecycle ──────────────────────────────────────────────────────────────

def test_record_and_publish_a_search(app, client, user):
    pet = make_pet(user)
    login(client)
    track_id = record_track(client, pet.id, notes="Checked the creek line")

    track = db.session.get(SearchTrack, track_id)
    assert track.is_published
    assert track.notes == "Checked the creek line"
    assert track.cell_count > 0
    assert track.distance_m > 4000          # measured on the untrimmed path
    assert track.min_lat is not None        # extent denormalised for the map


def test_points_outside_tasmania_are_dropped_not_fatal(app, client, user):
    pet = make_pet(user)
    login(client)
    started = client.post(f"/pets/{pet.id}/tracks", json={})
    track_id = started.get_json()["track_id"]

    resp = client.post(f"/tracks/{track_id}/points", json={"points": [
        [-42.88, 147.33, 1], [-33.87, 151.21, 2], ["junk", None, 3], [-42.881, 147.33, 4],
    ]})
    assert resp.status_code == 200
    assert resp.get_json()["accepted"] == 2


def test_a_running_track_is_private_until_finished(app, client, user, other_user):
    pet = make_pet(user)
    login(client)
    client.post(f"/pets/{pet.id}/tracks", json={})
    client.get("/logout")

    data = client.get(f"/pets/{pet.id}/tracks.geojson").get_json()
    assert data["cells"] == []
    assert data["tracks"] == []


def test_only_the_author_can_append(app, client, user, other_user):
    pet = make_pet(user)
    login(client)
    track_id = client.post(f"/pets/{pet.id}/tracks", json={}).get_json()["track_id"]
    client.get("/logout")

    login(client, email="owner@example.com")
    resp = client.post(f"/tracks/{track_id}/points", json={"points": [[-42.88, 147.33, 1]]})
    assert resp.status_code == 403


def test_a_search_too_short_to_map_publishes_nothing(app, client, user):
    pet = make_pet(user)
    login(client)
    started = client.post(f"/pets/{pet.id}/tracks", json={})
    track_id = started.get_json()["track_id"]
    client.post(f"/tracks/{track_id}/points",
                json={"points": [[-42.88, 147.33, 1], [-42.880001, 147.33, 2]]})
    resp = client.post(f"/tracks/{track_id}/finish", json={})

    assert resp.get_json()["published"] is False
    assert db.session.get(SearchTrack, track_id).cell_count == 0


def test_beacon_form_encoding_is_accepted(app, client, user):
    """A closing tab sends FormData, because sendBeacon can't set headers."""
    pet = make_pet(user)
    login(client)
    track_id = client.post(f"/pets/{pet.id}/tracks", json={}).get_json()["track_id"]

    resp = client.post(f"/tracks/{track_id}/points",
                       data={"points": json.dumps([[-42.88, 147.33, 1]])})
    assert resp.status_code == 200
    assert resp.get_json()["accepted"] == 1


# ── Privacy ────────────────────────────────────────────────────────────────

def test_public_gets_cells_but_never_the_line(app, client, user):
    pet = make_pet(user)
    login(client)
    record_track(client, pet.id)
    client.get("/logout")

    data = client.get(f"/pets/{pet.id}/tracks.geojson").get_json()
    assert len(data["cells"]) > 0                 # coverage is public
    assert data["lines"]["features"] == []        # the route is not
    assert len(data["tracks"]) == 1               # and the summary is


def test_searcher_owner_and_moderator_see_the_line(app, client, user, other_user, moderator):
    """The pet is other_user's; the search is user's."""
    pet = make_pet(other_user)
    login(client)                                  # the searcher
    record_track(client, pet.id)

    def line_count():
        return len(client.get(f"/pets/{pet.id}/tracks.geojson").get_json()["lines"]["features"])

    assert line_count() == 1                       # searcher
    client.get("/logout")

    login(client, email="owner@example.com")       # pet owner
    assert line_count() == 1
    client.get("/logout")

    login(client, email="mod@example.com")         # moderator
    assert line_count() == 1


def test_an_unrelated_signed_in_user_does_not_see_the_line(app, client, user):
    pet = make_pet(user)
    login(client)
    record_track(client, pet.id)
    client.get("/logout")

    make_user(email="nosy@example.com")
    login(client, email="nosy@example.com")
    data = client.get(f"/pets/{pet.id}/tracks.geojson").get_json()
    assert data["lines"]["features"] == []
    assert len(data["cells"]) > 0


def test_the_stored_line_is_already_trimmed(app, client, user):
    """Trimming happens on write, so the untrimmed path is never in the database.

    The trim is only ~50 m now, which removes the immediate vicinity of the
    start button and no more — it is a backstop, not concealment. The advice
    not to start at home is what actually protects an address; see
    test_recording_form_warns_against_starting_at_home.
    """
    pet = make_pet(user)
    login(client)
    points = walk_points()
    track_id = record_track(client, pet.id, points=points)

    stored = decode_points(db.session.get(SearchTrack, track_id).points)
    assert stored, "something should survive"
    assert stored[0][:2] != points[0][:2]
    assert stored[-1][:2] != points[-1][:2]

    from services.geo import haversine_m
    trim = app.config["TRACK_TRIM_M"]
    start_gap = haversine_m(points[0][0], points[0][1], stored[0][0], stored[0][1])
    end_gap = haversine_m(points[-1][0], points[-1][1], stored[-1][0], stored[-1][1])
    # At least the configured trim, allowing one fix of quantisation either way.
    assert start_gap >= trim * 0.9
    assert end_gap >= trim * 0.9


def test_recording_form_warns_against_starting_at_home(app, client, user):
    """The warning is the protection now, so its absence is a real regression."""
    pet = make_pet(user)
    login(client)
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)

    assert "Don't start recording at your home" in body
    assert "safety-warning" in body
    assert "Walk or drive to the" in body and "search area first" in body
    assert "Press stop before you head back" in body
    # The trim figure must come from config, not be written into the copy —
    # quoting a stale number here is how the warning starts lying.
    assert f"about {int(app.config['TRACK_TRIM_M'])} m is trimmed" in body


def test_main_map_coverage_never_returns_lines(app, client, user):
    pet = make_pet(user)
    login(client)
    record_track(client, pet.id)

    data = client.get("/api/coverage").get_json()       # still signed in as the searcher
    assert len(data["cells"]) > 0
    assert "lines" not in data
    assert "points" not in data


def test_coverage_respects_the_viewport(app, client, user):
    pet = make_pet(user)
    login(client)
    record_track(client, pet.id)

    everywhere = client.get("/api/coverage").get_json()["cells"]
    elsewhere = client.get("/api/coverage?bbox=-41.5,147.0,-41.4,147.2").get_json()["cells"]
    assert len(everywhere) > 0
    assert elsewhere == []


def test_removed_track_disappears_from_coverage(app, client, user):
    pet = make_pet(user)
    login(client)
    track_id = record_track(client, pet.id)
    assert len(client.get("/api/coverage").get_json()["cells"]) > 0

    client.post(f"/tracks/{track_id}/delete", json={})
    assert client.get("/api/coverage").get_json()["cells"] == []


# ── Drone import ───────────────────────────────────────────────────────────

def geotagged_jpeg(lat, lng, when="2026:08:16 10:00:00") -> bytes:
    """A small JPEG carrying GPS EXIF, like a photo off the drone's card.

    Pillow writes EXIF rationals from Fraction, not from (numerator,
    denominator) tuples — a tuple raises deep inside TiffImagePlugin.
    """
    from fractions import Fraction

    from PIL import ExifTags

    def dms(value):
        value = abs(value)
        d = int(value)
        m = int((value - d) * 60)
        s = (value - d - m / 60) * 3600
        return (Fraction(d, 1), Fraction(m, 1), Fraction(round(s * 10_000), 10_000))

    exif = Image.Exif()
    exif[ExifTags.Base.DateTimeOriginal] = when
    exif[ExifTags.IFD.GPSInfo] = {
        ExifTags.GPS.GPSLatitudeRef: "S" if lat < 0 else "N",
        ExifTags.GPS.GPSLatitude: dms(lat),
        ExifTags.GPS.GPSLongitudeRef: "W" if lng < 0 else "E",
        ExifTags.GPS.GPSLongitude: dms(lng),
        ExifTags.GPS.GPSAltitude: Fraction(60, 1),
    }
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 60, 90)).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_exif_reader_extracts_position(app):
    from services.exif_track import read_fix
    fix = read_fix(geotagged_jpeg(-42.8800, 147.3300))

    assert fix is not None
    assert abs(fix.lat - (-42.88)) < 1e-4
    assert abs(fix.lng - 147.33) < 1e-4
    assert fix.taken_at is not None
    assert fix.altitude_m == 60


def test_exif_reader_rejects_photos_without_gps(app):
    from services.exif_track import read_fix
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")
    assert read_fix(buf.getvalue()) is None
    assert read_fix(b"not an image") is None


def test_drone_import_builds_a_path_and_keeps_no_photos(app, client, user):
    pet = make_pet(user)
    login(client)

    photos = [(io.BytesIO(geotagged_jpeg(-42.8800 + i * 0.002, 147.3300,
                                         f"2026:08:16 10:{i:02d}:00")), f"DJI_{i}.jpg")
              for i in range(6)]
    resp = client.post(f"/pets/{pet.id}/tracks/drone",
                       data={"photos": photos, "notes": "Thermal grid, 60 m"},
                       content_type="multipart/form-data", follow_redirects=True)

    assert "Flight added from 6 photo positions" in resp.get_data(as_text=True)
    assert "photos themselves were not stored" in resp.get_data(as_text=True)

    track = SearchTrack.query.filter_by(source="drone").one()
    assert track.is_published
    assert track.cell_count > 0
    assert track.notes == "Thermal grid, 60 m"
    # Nothing on the model can hold an image, and none was written.
    assert not hasattr(track, "photo")
    assert track.points is not None and len(track.points) < 4096


def test_drone_import_needs_two_usable_positions(app, client, user):
    pet = make_pet(user)
    login(client)
    plain = io.BytesIO()
    Image.new("RGB", (8, 8)).save(plain, "JPEG")

    resp = client.post(f"/pets/{pet.id}/tracks/drone",
                       data={"photos": [(plain, "no_gps.jpg")]},
                       content_type="multipart/form-data", follow_redirects=True)
    assert "fewer than two of those photos had usable GPS" in resp.get_data(as_text=True)
    assert SearchTrack.query.count() == 0


def test_drone_flights_are_not_trimmed(app, client, user):
    """A flight's launch point is search information, not a home address.

    Trimming 200 m off each end would delete real coverage from a short
    sortie while protecting nothing — so drone imports keep their full path.
    """
    pet = make_pet(user)
    login(client)
    lats = [-42.8800 + i * 0.002 for i in range(4)]
    photos = [(io.BytesIO(geotagged_jpeg(lat, 147.3300, f"2026:08:16 10:{i:02d}:00")),
               f"DJI_{i}.jpg") for i, lat in enumerate(lats)]
    client.post(f"/pets/{pet.id}/tracks/drone", data={"photos": photos},
                content_type="multipart/form-data", follow_redirects=True)

    track = SearchTrack.query.filter_by(source="drone").one()
    stored = decode_points(track.points)
    assert len(stored) == 4
    assert abs(stored[0][0] - lats[0]) < 1e-4        # first photo kept
    assert abs(stored[-1][0] - lats[-1]) < 1e-4      # last photo kept


def test_drone_import_ignores_photos_outside_tasmania(app, client, user):
    pet = make_pet(user)
    login(client)
    photos = [
        (io.BytesIO(geotagged_jpeg(-42.8800, 147.3300, "2026:08:16 10:00:00")), "a.jpg"),
        (io.BytesIO(geotagged_jpeg(-42.8820, 147.3300, "2026:08:16 10:01:00")), "b.jpg"),
        (io.BytesIO(geotagged_jpeg(-33.8700, 151.2100, "2026:08:16 10:02:00")), "sydney.jpg"),
    ]
    resp = client.post(f"/pets/{pet.id}/tracks/drone", data={"photos": photos},
                       content_type="multipart/form-data", follow_redirects=True)

    assert "1 were outside Tasmania and ignored" in resp.get_data(as_text=True)
    assert SearchTrack.query.one().max_lat < -42
