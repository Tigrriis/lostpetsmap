"""Account-level moderation: bulk removal, and suspension holding everywhere.

A suspended account is only suspended if every way in refuses it, and "remove
all" is only a cleanup button if it removes all.
"""
import pytest
from conftest import login, make_pet, make_user

from auth import _make_verify_token
from extensions import db
from models import Pet, SearchTrack, Sighting, User
from services import coverage
from services.localtime import now_utc


def spammer_with_everything(spammer):
    """One of each thing an account can post."""
    pet = make_pet(spammer, name="Spam")
    standalone = Sighting(pet_id=None, user_id=spammer.id, species="cat",
                          lat=-42.8615, lng=147.3045, seen_at=now_utc(),
                          description="totally real cat, pay me")
    attached = Sighting(pet_id=pet.id, user_id=spammer.id,
                        lat=-42.8615, lng=147.3045, seen_at=now_utc())
    track = SearchTrack(pet_id=pet.id, user_id=spammer.id, started_at=now_utc(),
                        finished_at=now_utc(), points=coverage.encode_points([]))
    db.session.add_all([standalone, attached, track])
    db.session.commit()
    return pet, standalone, attached, track


def test_remove_all_takes_down_sightings_and_tracks_too(app, client, moderator):
    """It removed reports only, leaving a spammer's standalone sightings on
    the main map after the button that said "all" had been pressed."""
    spammer = make_user(email="spam@example.com")
    pet, standalone, attached, track = spammer_with_everything(spammer)

    login(client, email="mod@example.com")
    resp = client.post(f"/moderate/user/{spammer.id}/remove_reports",
                       data={"reason": "Spam"}, follow_redirects=True)
    assert resp.status_code == 200

    for row in (pet, standalone, attached, track):
        row = db.session.get(type(row), row.id)
        assert row.is_removed, f"{type(row).__name__} {row.id} still live"
        assert row.removed_by_id == moderator.id
        assert row.removed_reason == "Spam"


def test_remove_all_clears_the_map_feed(app, client, moderator):
    spammer = make_user(email="spam@example.com")
    spammer_with_everything(spammer)
    assert len(client.get("/api/pets").get_json()["features"]) == 2   # pet + standalone

    login(client, email="mod@example.com")
    client.post(f"/moderate/user/{spammer.id}/remove_reports", follow_redirects=True)
    assert client.get("/api/pets").get_json()["features"] == []


def test_remove_all_reports_what_it_did(app, client, moderator):
    spammer = make_user(email="spam@example.com")
    spammer_with_everything(spammer)
    login(client, email="mod@example.com")
    body = client.post(f"/moderate/user/{spammer.id}/remove_reports",
                       follow_redirects=True).get_data(as_text=True)
    assert "1 report(s), 2 sighting(s) and 1 search track(s)" in body


def test_remove_all_leaves_other_accounts_alone(app, client, moderator, user):
    spammer = make_user(email="spam@example.com")
    spammer_with_everything(spammer)
    mine = make_pet(user, name="Innocent")

    login(client, email="mod@example.com")
    client.post(f"/moderate/user/{spammer.id}/remove_reports", follow_redirects=True)
    assert not db.session.get(Pet, mine.id).is_removed


# ── Suspension ─────────────────────────────────────────────────────────────

def test_a_verification_link_does_not_sign_in_a_suspended_account(app, client, user):
    """/login refuses a banned account; the confirm-email link used to log it
    straight in anyway."""
    user.is_banned = True
    db.session.commit()

    with app.test_request_context():
        token = _make_verify_token(user)
    resp = client.get(f"/verify-email/{token}", follow_redirects=True)

    assert resp.status_code == 200
    assert "suspended" in resp.get_data(as_text=True)
    # Not signed in: a page that needs a session bounces to login.
    assert client.get("/mine").status_code == 302


def test_a_verification_link_still_confirms_a_suspended_address(app, client, user):
    """The address is theirs whether or not the account may act; recording it
    keeps the audit honest for when they are reinstated."""
    user.is_banned = True
    db.session.commit()
    with app.test_request_context():
        token = _make_verify_token(user)
    client.get(f"/verify-email/{token}")
    assert db.session.get(User, user.id).email_verified


# ── Restore symmetry ───────────────────────────────────────────────────────

def test_restoring_a_sighting_clears_the_removal_reason(app, client, user, moderator):
    s = Sighting(pet_id=None, user_id=user.id, species="cat", lat=-42.86, lng=147.30,
                 seen_at=now_utc(), is_removed=True, removed_reason="Oops")
    db.session.add(s)
    db.session.commit()

    login(client, email="mod@example.com")
    client.post(f"/moderate/sighting/{s.id}/restore", follow_redirects=True)
    s = db.session.get(Sighting, s.id)
    assert not s.is_removed
    assert s.removed_reason is None
