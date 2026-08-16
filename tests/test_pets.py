"""Report lifecycle: create, validate, filter, edit, moderate."""
from datetime import datetime, timedelta, timezone

from conftest import login, make_pet, make_user

from extensions import db
from models import Pet


def _form(**overrides):
    data = {
        "report_type": "missing",
        "species": "cat",
        "name": "Raven",
        "colour": "black and white",
        "description": "Maine coon, no collar.",
        "locality": "New Town",
        "last_seen_at": (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
        "lat": "-42.8610617",
        "lng": "147.304103",
        "blur_location": "1",
    }
    data.update(overrides)
    return data


# ── Access ────────────────────────────────────────────────────────────────

def test_map_is_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/pets").status_code == 200


def test_posting_requires_an_account(client):
    resp = client.get("/pets/new")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_create_report(app, client, user):
    login(client)
    resp = client.post("/pets/new", data=_form(), follow_redirects=True)
    assert resp.status_code == 200

    pet = Pet.query.one()
    assert pet.name == "Raven"
    assert pet.user_id == user.id
    assert pet.status == "active"
    assert pet.blur_location is True


# ── Validation ────────────────────────────────────────────────────────────

def test_pin_is_required(app, client, user):
    login(client)
    client.post("/pets/new", data=_form(lat="", lng=""), follow_redirects=True)
    assert Pet.query.count() == 0


def test_pin_outside_tasmania_is_rejected(app, client, user):
    """Sydney. Silently accepting it would put a marker off the map for good."""
    login(client)
    resp = client.post("/pets/new", data=_form(lat="-33.87", lng="151.21"),
                       follow_redirects=True)
    assert Pet.query.count() == 0
    assert "outside Tasmania" in resp.get_data(as_text=True)


def test_future_sighting_is_rejected(app, client, user):
    login(client)
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    client.post("/pets/new", data=_form(last_seen_at=future), follow_redirects=True)
    assert Pet.query.count() == 0


def test_found_report_drops_the_name(app, client, user):
    """You cannot know a stray's name, so the field is not carried over."""
    login(client)
    client.post("/pets/new",
                data=_form(report_type="found", name="Fluffy", description="Tabby, friendly."),
                follow_redirects=True)
    pet = Pet.query.one()
    assert pet.report_type == "found"
    assert pet.name is None


def test_missing_report_needs_a_name_or_description(app, client, user):
    login(client)
    client.post("/pets/new", data=_form(name="", description=""), follow_redirects=True)
    assert Pet.query.count() == 0


def test_last_seen_is_read_as_tasmanian_local_time(app, client, user):
    """A wall-clock string with no offset must not be treated as UTC."""
    login(client)
    client.post("/pets/new", data=_form(last_seen_at="2026-01-15T09:00"), follow_redirects=True)
    pet = Pet.query.one()

    stored = pet.last_seen_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    # January is AEDT, UTC+11 — 09:00 local is 22:00 UTC on the 14th.
    assert stored.astimezone(timezone.utc).hour == 22
    assert stored.astimezone(timezone.utc).day == 14


# ── Feed filters ──────────────────────────────────────────────────────────

def test_feed_filters_by_type_and_species(app, client, user):
    make_pet(user, report_type="missing", species="cat")
    make_pet(user, report_type="found", species="dog", name=None,
             description="Brown kelpie, no collar.")

    assert len(client.get("/api/pets").get_json()["features"]) == 2
    assert len(client.get("/api/pets?type=found").get_json()["features"]) == 1
    assert len(client.get("/api/pets?species=cat").get_json()["features"]) == 1
    assert len(client.get("/api/pets?species=cat&species=dog").get_json()["features"]) == 2


def test_feed_filters_by_bbox(app, client, user):
    make_pet(user, lat=-42.8610617, lng=147.304103)         # Hobart
    make_pet(user, lat=-41.4332, lng=147.1441, name="Bo")   # Launceston

    hobart = "-43.1,146.9,-42.6,147.6"
    features = client.get(f"/api/pets?bbox={hobart}").get_json()["features"]
    assert len(features) == 1
    assert features[0]["properties"]["name"] == "Raven"


def test_feed_excludes_old_reports_by_default(app, client, user):
    make_pet(user, last_seen_at=datetime.now(timezone.utc) - timedelta(days=400))
    assert client.get("/api/pets").get_json()["features"] == []
    assert len(client.get("/api/pets?days=0").get_json()["features"]) == 1


def test_feed_shows_active_only_by_default(app, client, user):
    pet = make_pet(user)
    pet.status = "reunited"
    db.session.commit()

    assert client.get("/api/pets").get_json()["features"] == []
    assert len(client.get("/api/pets?status=reunited").get_json()["features"]) == 1
    assert len(client.get("/api/pets?status=all").get_json()["features"]) == 1


def test_feed_search_matches_free_text(app, client, user):
    make_pet(user, breed="Maine Coon")
    assert len(client.get("/api/pets?q=maine").get_json()["features"]) == 1
    assert client.get("/api/pets?q=labrador").get_json()["features"] == []


def test_malformed_bbox_is_ignored_not_fatal(app, client, user):
    make_pet(user)
    resp = client.get("/api/pets?bbox=garbage")
    assert resp.status_code == 200
    assert len(resp.get_json()["features"]) == 1


# ── Ownership ─────────────────────────────────────────────────────────────

def test_only_the_owner_can_edit(app, client, user, other_user):
    pet = make_pet(other_user)
    login(client)                      # finder, not the owner
    assert client.get(f"/pets/{pet.id}/edit").status_code == 403
    assert client.post(f"/pets/{pet.id}/delete").status_code == 403


def test_owner_can_mark_reunited(app, client, user):
    pet = make_pet(user)
    login(client)
    client.post(f"/pets/{pet.id}/status", data={"status": "reunited"}, follow_redirects=True)
    assert db.session.get(Pet, pet.id).status == "reunited"
    assert db.session.get(Pet, pet.id).resolved_at is not None


def test_delete_is_soft_and_hides_the_report(app, client, user):
    pet = make_pet(user)
    login(client)
    client.post(f"/pets/{pet.id}/delete", follow_redirects=True)

    row = db.session.get(Pet, pet.id)
    assert row is not None and row.is_removed is True
    client.get("/logout")
    assert client.get(f"/pets/{pet.id}").status_code == 404
    assert client.get("/api/pets").get_json()["features"] == []


def test_moderator_sees_and_restores_a_removed_report(app, client, user, moderator):
    pet = make_pet(user)
    login(client, email="mod@example.com")

    client.post(f"/moderate/pet/{pet.id}/remove", data={"reason": "spam"},
                follow_redirects=True)
    assert db.session.get(Pet, pet.id).is_removed is True
    assert client.get(f"/pets/{pet.id}").status_code == 200      # moderators can see it

    client.post(f"/moderate/pet/{pet.id}/restore", follow_redirects=True)
    assert db.session.get(Pet, pet.id).is_removed is False


def test_moderation_queue_is_closed_to_ordinary_users(app, client, user):
    login(client)
    assert client.get("/moderate").status_code == 403


def test_banned_user_cannot_post(app, client, user):
    login(client)
    user.is_banned = True
    db.session.commit()
    resp = client.get("/pets/new", follow_redirects=True)
    assert "suspended" in resp.get_data(as_text=True).lower()
    assert Pet.query.count() == 0
