"""Moderating sightings, including the standalone ones.

Making pet_id nullable turned every "the sighting's pet" assumption into a
latent crash. These cover the moderation paths that hit one.
"""
import pytest
from conftest import login, make_pet, make_user

from extensions import db
from models import Sighting
from services.localtime import now_utc


@pytest.fixture
def moderator(app):
    mod = make_user(email="mod@example.com")
    mod.role = "moderator"
    mod.email_verified = True
    db.session.commit()
    return mod


def standalone(reporter, **kw):
    """A sighting with no report behind it."""
    s = Sighting(pet_id=None, user_id=reporter.id, species="cat",
                 description="Tabby by the bins", lat=-42.8615, lng=147.3045,
                 seen_at=kw.pop("seen_at", now_utc()), **kw)
    db.session.add(s)
    db.session.commit()
    return s


def attached(reporter, pet, **kw):
    s = Sighting(pet_id=pet.id, user_id=reporter.id, note="Saw them run east",
                 lat=-42.8615, lng=147.3045, seen_at=now_utc(), **kw)
    db.session.add(s)
    db.session.commit()
    return s


# ── The queue itself ───────────────────────────────────────────────────────

def test_the_queue_renders_with_a_standalone_sighting(app, client, user,
                                                      moderator):
    """It used to 500: url_for(pet_detail, pet_id=None) raises BuildError."""
    standalone(user)
    login(client, email="mod@example.com")
    resp = client.get("/moderate?view=sightings")
    assert resp.status_code == 200
    assert "Tabby by the bins" in resp.get_data(as_text=True)


def test_the_queue_still_links_an_attached_sighting_to_its_report(
        app, client, user, moderator):
    pet = make_pet(user, name="Raven")
    attached(user, pet)
    login(client, email="mod@example.com")
    body = client.get("/moderate?view=sightings").get_data(as_text=True)
    assert f"/pets/{pet.id}" in body


# ── Removal ────────────────────────────────────────────────────────────────

def test_a_moderator_can_remove_a_standalone_sighting(app, client, user,
                                                      moderator):
    s = standalone(user)
    login(client, email="mod@example.com")
    resp = client.post(f"/moderate/sighting/{s.id}/remove",
                       data={"reason": "Spam"}, follow_redirects=True)
    assert resp.status_code == 200

    s = db.session.get(Sighting, s.id)
    assert s.is_removed
    assert s.removed_by_id == moderator.id
    assert s.removed_reason == "Spam"


def test_a_moderator_can_remove_an_attached_sighting(app, client, user,
                                                     moderator):
    pet = make_pet(user)
    s = attached(user, pet)
    login(client, email="mod@example.com")
    client.post(f"/moderate/sighting/{s.id}/remove", data={"reason": "Abuse"},
                follow_redirects=True)
    assert db.session.get(Sighting, s.id).is_removed


def test_a_removed_standalone_sighting_can_be_restored(app, client, user,
                                                       moderator):
    s = standalone(user, is_removed=True)
    login(client, email="mod@example.com")
    client.post(f"/moderate/sighting/{s.id}/restore", follow_redirects=True)
    assert not db.session.get(Sighting, s.id).is_removed


# ── The delete button on the sighting's own page ───────────────────────────

def test_delete_works_on_a_standalone_sighting(app, client, user, moderator):
    """It used to 500 on sighting.pet.user_id — there is no pet."""
    s = standalone(user)
    login(client, email="mod@example.com")
    resp = client.post(f"/sightings/{s.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(Sighting, s.id).is_removed


def test_the_reporter_can_delete_their_own_standalone_sighting(app, client, user):
    s = standalone(user)
    login(client)
    client.post(f"/sightings/{s.id}/delete", follow_redirects=True)
    assert db.session.get(Sighting, s.id).is_removed


def test_a_stranger_cannot_delete_a_standalone_sighting(app, client, user,
                                                        other_user):
    s = standalone(user)
    login(client, email=other_user.email)
    resp = client.post(f"/sightings/{s.id}/delete")
    assert resp.status_code == 403
    assert not db.session.get(Sighting, s.id).is_removed


# ── The controls have to actually render ───────────────────────────────────
#
# The route working is not the fix. A button that quietly fails to render
# looks exactly like a feature that was never built, which is how this hid.

def test_a_moderator_sees_remove_on_a_standalone_sighting_page(
        app, client, user, moderator):
    s = standalone(user)
    login(client, email="mod@example.com")
    body = client.get(f"/sightings/{s.id}").get_data(as_text=True)
    assert f"/sightings/{s.id}/delete" in body


def test_a_stranger_sees_no_remove_control(app, client, user, other_user):
    s = standalone(user)
    login(client, email=other_user.email)
    body = client.get(f"/sightings/{s.id}").get_data(as_text=True)
    assert f"/sightings/{s.id}/delete" not in body


def test_a_signed_out_visitor_sees_no_remove_control(app, client, user):
    s = standalone(user)
    body = client.get(f"/sightings/{s.id}").get_data(as_text=True)
    assert "/delete" not in body


def test_a_moderator_sees_remove_on_a_report_page(app, client, user, moderator):
    pet = make_pet(user)
    s = attached(user, pet)
    login(client, email="mod@example.com")
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert f"/sightings/{s.id}/delete" in body


def test_a_stranger_sees_no_remove_control_on_a_report_page(
        app, client, user, other_user):
    pet = make_pet(user)
    s = attached(user, pet)
    login(client, email=other_user.email)
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert f"/sightings/{s.id}/delete" not in body


def test_the_report_owner_can_still_delete_an_attached_sighting(
        app, client, user, other_user):
    pet = make_pet(user)
    s = attached(other_user, pet)
    login(client)                       # the pet's owner
    client.post(f"/sightings/{s.id}/delete", follow_redirects=True)
    assert db.session.get(Sighting, s.id).is_removed
