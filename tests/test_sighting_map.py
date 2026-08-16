"""Standalone sightings on the main map feed.

They ride the same /api/pets response as reports: one bounding-box request
answers "what has been seen around here?", and `kind` tells the client which
is which.
"""
from datetime import timedelta

from conftest import login, make_pet, make_user

from extensions import db
from models import Sighting
from services.localtime import now_utc


def standalone(user, lat=-42.8615, lng=147.3045, species="cat", when=None, **kw):
    s = Sighting(pet_id=None, user_id=user.id, lat=lat, lng=lng,
                 seen_at=when or now_utc(), species=species, **kw)
    db.session.add(s)
    db.session.commit()
    return s


def kinds(payload):
    return [f["properties"]["kind"] for f in payload["features"]]


def test_standalone_sightings_appear_in_the_feed(app, client, user):
    make_pet(user)
    standalone(user, description="Ginger tabby")

    data = client.get("/api/pets").get_json()
    assert sorted(kinds(data)) == ["pet", "sighting"]


def test_a_sighting_attached_to_a_report_is_not_duplicated(app, client, user):
    """It already shows as part of that report; a second marker would mislead."""
    pet = make_pet(user)
    db.session.add(Sighting(pet_id=pet.id, user_id=user.id, lat=pet.lat,
                            lng=pet.lng, seen_at=now_utc()))
    db.session.commit()

    assert kinds(client.get("/api/pets").get_json()) == ["pet"]


def test_sightings_respect_the_bounding_box(app, client, user):
    standalone(user)                                        # Hobart
    standalone(user, lat=-41.4391, lng=147.1358)            # Launceston

    hobart = client.get("/api/pets?bbox=-43.1,146.9,-42.6,147.6").get_json()
    assert len(hobart["features"]) == 1


def test_sightings_respect_the_species_filter(app, client, user):
    standalone(user, species="cat")
    standalone(user, species="dog")

    assert len(client.get("/api/pets?species=cat").get_json()["features"]) == 1


def test_sightings_respect_the_date_window(app, client, user):
    standalone(user, when=now_utc() - timedelta(days=400))
    assert client.get("/api/pets").get_json()["features"] == []
    assert len(client.get("/api/pets?days=0").get_json()["features"]) == 1


def test_narrowing_to_a_report_type_hides_sightings(app, client, user):
    """Missing and found are report types; a sighting is neither."""
    make_pet(user, report_type="missing")
    standalone(user)

    assert kinds(client.get("/api/pets?type=missing").get_json()) == ["pet"]
    assert kinds(client.get("/api/pets?type=found").get_json()) == []
    assert "sighting" in kinds(client.get("/api/pets").get_json())


def test_removed_sightings_leave_the_map(app, client, user):
    s = standalone(user)
    assert len(client.get("/api/pets").get_json()["features"]) == 1
    s.is_removed = True
    db.session.commit()
    assert client.get("/api/pets").get_json()["features"] == []


def test_the_feature_carries_what_the_map_needs(app, client, user):
    standalone(user, description="Ginger tabby", note="Under a car")
    f = client.get("/api/pets").get_json()["features"][0]
    props = f["properties"]

    assert props["kind"] == "sighting"
    assert props["species_label"] == "Cat"
    assert props["description"] == "Ginger tabby"
    assert props["url"].startswith("/sightings/")
    assert props["approximate"] is False, "sightings are shown exactly, by design"


# ── The sighting's own page ────────────────────────────────────────────────

def test_a_standalone_sighting_has_a_public_page(app, client, user):
    s = standalone(user, description="Ginger tabby", note="Under a parked car")
    body = client.get(f"/sightings/{s.id}").get_data(as_text=True)

    assert "Ginger tabby" in body
    assert "Under a parked car" in body
    assert "isn't a found report" in body


def test_the_page_lists_nearby_missing_pets(app, client, user, other_user):
    pet = make_pet(other_user, species="cat", name="Raven")
    s = standalone(user, species="cat")

    body = client.get(f"/sightings/{s.id}").get_data(as_text=True)
    assert "Raven" in body


def test_a_sighting_on_a_report_redirects_to_it(app, client, user):
    pet = make_pet(user)
    s = Sighting(pet_id=pet.id, user_id=user.id, lat=pet.lat, lng=pet.lng,
                 seen_at=now_utc())
    db.session.add(s)
    db.session.commit()

    resp = client.get(f"/sightings/{s.id}")
    assert resp.status_code == 302
    assert f"/pets/{pet.id}" in resp.headers["Location"]


def test_a_removed_sighting_has_no_page(app, client, user):
    s = standalone(user)
    s.is_removed = True
    db.session.commit()
    assert client.get(f"/sightings/{s.id}").status_code == 404
