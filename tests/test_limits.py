"""Cost controls: the daily photo budget and the metered-geocoding budget.

Two different kinds of cost. Photos are the only bulky thing in a 1 GB
database at roughly 405 KB each; geocoding is the only call that bills per
request rather than per gigabyte.
"""
import io

from conftest import login, make_pet, make_user
from PIL import Image

import services.geocode as geocode_service
from extensions import db
from models import Pet, PetPhoto, Sighting, photos_uploaded_since


def jpeg(colour=(120, 90, 60), size=(400, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


def add_photos(pet, n):
    """Seed stored photos directly, as though uploaded earlier today."""
    for i in range(n):
        db.session.add(PetPhoto(pet_id=pet.id, data=b"x", thumb=b"y", sort_order=i))
    db.session.commit()


# ── Daily photo budget ─────────────────────────────────────────────────────

def test_counter_spans_reports_and_sightings(app, user):
    """One database, one budget — a sighting photo costs the same storage."""
    pet = make_pet(user)
    add_photos(pet, 3)
    db.session.add(Sighting(pet_id=pet.id, user_id=user.id, lat=-42.88, lng=147.33,
                            seen_at=pet.last_seen_at, photo=b"z",
                            photo_mimetype="image/jpeg"))
    db.session.commit()

    assert photos_uploaded_since(user.id) == 4


def test_counter_ignores_other_peoples_photos(app, user, other_user):
    add_photos(make_pet(other_user), 5)
    assert photos_uploaded_since(user.id) == 0


def test_sightings_without_a_photo_do_not_count(app, user):
    pet = make_pet(user)
    db.session.add(Sighting(pet_id=pet.id, user_id=user.id, lat=-42.88, lng=147.33,
                            seen_at=pet.last_seen_at))
    db.session.commit()
    assert photos_uploaded_since(user.id) == 0


def test_upload_is_refused_once_the_daily_cap_is_reached(app, client, user):
    app.config["MAX_PHOTOS_PER_DAY"] = 3
    pet = make_pet(user)
    add_photos(pet, 3)
    login(client)

    resp = client.post(f"/pets/{pet.id}/edit", data={
        "report_type": "missing", "species": "cat", "name": "Raven",
        "colour": "black", "description": "Shy.", "locality": "New Town",
        "last_seen_at": "2026-08-16T09:00",
        "lat": "-42.8610617", "lng": "147.304103", "blur_location": "1",
        "photos": (io.BytesIO(jpeg()), "extra.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert "daily limit" in resp.get_data(as_text=True)
    assert PetPhoto.query.count() == 3, "the extra photo must not be stored"


def test_hitting_the_cap_still_saves_the_report(app, client, user):
    """The photo is dropped; the report is not. A lost pet still gets posted."""
    app.config["MAX_PHOTOS_PER_DAY"] = 0
    login(client)
    client.post("/pets/new", data={
        "report_type": "missing", "species": "dog", "name": "Barney",
        "colour": "tan", "description": "Friendly.", "locality": "Kingston",
        "last_seen_at": "2026-08-16T09:00",
        "lat": "-42.9758", "lng": "147.3083", "blur_location": "1",
        "photos": (io.BytesIO(jpeg()), "a.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert Pet.query.count() == 1
    assert PetPhoto.query.count() == 0


def test_a_photo_problem_never_discards_the_edit(app, client, user):
    """Exceeding the per-report limit used to throw away the whole submission.

    The blocking/non-blocking split was inferred by matching message text, so
    any photo message other than "not a readable image" silently blocked the
    save — losing every other edit on the form with it.
    """
    app.config["MAX_PHOTOS_PER_DAY"] = 20
    pet = make_pet(user)
    add_photos(pet, app.config["MAX_PHOTOS_PER_PET"])       # already full
    login(client)

    resp = client.post(f"/pets/{pet.id}/edit", data={
        "report_type": "missing", "species": "cat", "name": "Raven",
        "colour": "black and white", "description": "Now with a red collar.",
        "locality": "New Town", "last_seen_at": "2026-08-16T09:00",
        "lat": "-42.8610617", "lng": "147.304103", "blur_location": "1",
        "photos": (io.BytesIO(jpeg()), "fifth.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert "photo limit" in resp.get_data(as_text=True)
    refreshed = db.session.get(Pet, pet.id)
    assert refreshed.description == "Now with a red collar.", \
        "the text edits must survive a rejected photo"
    assert len(refreshed.photos) == app.config["MAX_PHOTOS_PER_PET"]


def test_sighting_photo_respects_the_same_budget(app, client, user, other_user):
    app.config["MAX_PHOTOS_PER_DAY"] = 1
    pet = make_pet(other_user)
    add_photos(make_pet(user), 1)          # budget already spent on a report
    login(client)

    resp = client.post(f"/pets/{pet.id}/sightings", data={
        "lat": "-42.88", "lng": "147.33", "seen_at": "2026-08-16T09:00",
        "note": "Saw them by the creek.",
        "photo": (io.BytesIO(jpeg()), "sighting.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert "daily limit" in resp.get_data(as_text=True)
    sighting = Sighting.query.one()
    assert sighting.photo is None, "the sighting saves, minus the photo"


def test_photos_are_allowed_under_the_cap(app, client, user):
    app.config["MAX_PHOTOS_PER_DAY"] = 20
    login(client)
    client.post("/pets/new", data={
        "report_type": "missing", "species": "dog", "name": "Barney",
        "colour": "tan", "description": "Friendly.", "locality": "Kingston",
        "last_seen_at": "2026-08-16T09:00",
        "lat": "-42.9758", "lng": "147.3083", "blur_location": "1",
        "photos": (io.BytesIO(jpeg()), "a.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert PetPhoto.query.count() == 1


# ── Photo caching ──────────────────────────────────────────────────────────

def test_photos_cache_shorter_at_the_cdn_than_in_the_browser(app, client, user):
    """A shared cache keeps serving to new requesters after a removal.

    The browser TTL only affects someone who already has the image, so it can
    be long. The CDN TTL must be short enough that a moderator's removal takes
    effect in hours rather than a week.
    """
    pet = make_pet(user)
    add_photos(pet, 1)
    photo = PetPhoto.query.one()

    resp = client.get(f"/pets/{pet.id}/photo/{photo.id}")
    cache = resp.headers["Cache-Control"]

    assert "public" in cache
    browser = int(cache.split("max-age=")[1].split(",")[0])
    shared = int(cache.split("s-maxage=")[1].split(",")[0])
    assert shared < browser, "the CDN must expire before the browser does"
    assert shared <= 86400, "a removed report should not stay served for days"


def test_thumbnail_and_full_size_are_different_responses(app, client, user):
    """They share a path and differ only by query string, which is the cache key."""
    pet = make_pet(user)
    db.session.add(PetPhoto(pet_id=pet.id, data=b"full-image-bytes",
                            thumb=b"tiny", sort_order=0))
    db.session.commit()
    photo = PetPhoto.query.one()

    full = client.get(f"/pets/{pet.id}/photo/{photo.id}").data
    thumb = client.get(f"/pets/{pet.id}/photo/{photo.id}?size=thumb").data
    assert full != thumb


# ── Metered geocoding budget ───────────────────────────────────────────────

def test_slots_are_consumed_then_refused(app, user):
    for i in range(5):
        assert user.take_geocode_slot(5) is True, f"slot {i} should be free"
    assert user.take_geocode_slot(5) is False


def test_the_window_rolls_over(app, user):
    from datetime import timedelta, timezone as tz
    from models import _utcnow

    for _ in range(5):
        user.take_geocode_slot(5)
    assert user.take_geocode_slot(5) is False

    user.geocode_window_start = _utcnow() - timedelta(hours=1, minutes=1)
    assert user.take_geocode_slot(5) is True
    assert user.geocode_count == 1


def test_budget_is_per_account(app, user, other_user):
    for _ in range(5):
        user.take_geocode_slot(5)
    assert user.take_geocode_slot(5) is False
    assert other_user.take_geocode_slot(5) is True


def test_endpoint_refuses_once_the_budget_is_gone(app, client, user):
    app.config["MAX_GEOCODES_PER_HOUR"] = 2
    login(client)
    for _ in range(2):
        client.get("/api/geocode?address=Pedder+St+New+Town")

    resp = client.get("/api/geocode?address=Somewhere+Else")
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["ok"] is False
    assert "2 address lookups this hour" in body["message"]


def test_a_cached_address_costs_nothing(app, client, user, monkeypatch):
    """A repeat lookup never reaches Google, so it must not spend budget."""
    from models import GeocodeCache
    db.session.add(GeocodeCache(normalized_address="pedder st new town",
                                lat=-42.86, lng=147.30, formatted="Pedder St"))
    db.session.commit()

    calls = []
    monkeypatch.setattr(geocode_service, "geocode_detailed",
                        lambda addr: calls.append(addr))

    app.config["MAX_GEOCODES_PER_HOUR"] = 1
    login(client)
    for _ in range(5):
        resp = client.get("/api/geocode?address=Pedder St New Town")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    assert calls == [], "a cache hit must not call the geocoder"
    assert db.session.get(type(user), user.id).geocode_count == 0


def test_geocoding_requires_an_account(app, client):
    resp = client.get("/api/geocode?address=Pedder+St")
    assert resp.status_code in (302, 401)
