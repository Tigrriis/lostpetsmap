"""Email verification.

The relay is the only way a finder reaches a reporter, so an address nobody
owns quietly breaks the service. Actions that put a message in someone else's
inbox are gated on a confirmed address; posting your own report is not, because
a pet on a road will not wait for an email round trip.
"""
import pytest
from conftest import login, make_pet, make_user

import mailer
from auth import _make_verify_token, _user_from_verify_token
from extensions import db
from models import User


@pytest.fixture
def with_mail(monkeypatch):
    """Pretend a provider is configured, without letting anything send.

    mailer.send_email still logs rather than calling out, because
    config.RESEND_API_KEY stays empty — only the capability flag is faked.
    """
    monkeypatch.setattr(mailer, "mail_is_configured", lambda: True)


# ── Tokens ─────────────────────────────────────────────────────────────────

def test_token_round_trip(app, user):
    assert _user_from_verify_token(_make_verify_token(user)) is user


def test_token_is_void_once_the_address_changes(app, user):
    """A link minted for one address must not confirm a different one."""
    token = _make_verify_token(user)
    user.email = "somewhere.else@example.com"
    db.session.commit()
    assert _user_from_verify_token(token) is None


def test_token_rejects_tampering(app, user):
    token = _make_verify_token(user)
    assert _user_from_verify_token(token + "x") is None
    assert _user_from_verify_token("nonsense") is None


# ── The flow ───────────────────────────────────────────────────────────────

def test_new_accounts_start_unverified(app, client):
    client.post("/register", data={"email": "new@example.com",
                                   "password": "correcthorse"}, follow_redirects=True)
    assert User.query.one().email_verified is False


def test_following_the_link_confirms_and_signs_in(app, client, user):
    token = _make_verify_token(user)
    resp = client.get(f"/verify-email/{token}", follow_redirects=True)

    assert "Email confirmed" in resp.get_data(as_text=True)
    refreshed = db.session.get(User, user.id)
    assert refreshed.email_verified is True
    assert refreshed.email_verified_at is not None
    # Confirming from the link proves the address, so it also signs them in.
    assert client.get("/mine").status_code == 200


def test_a_bad_link_says_so_without_confirming(app, client, user):
    resp = client.get("/verify-email/rubbish", follow_redirects=True)
    assert "invalid or has expired" in resp.get_data(as_text=True)
    assert db.session.get(User, user.id).email_verified is False


def test_resend_is_rate_limited(app, client, user, with_mail):
    login(client)
    first = client.post("/resend-verification", follow_redirects=True)
    assert "Confirmation link sent" in first.get_data(as_text=True)

    second = client.post("/resend-verification", follow_redirects=True)
    assert "moments ago" in second.get_data(as_text=True)


def test_resend_on_a_confirmed_address_is_a_no_op(app, client, user, with_mail):
    user.email_verified = True
    db.session.commit()
    login(client)
    resp = client.post("/resend-verification", follow_redirects=True)
    assert "already confirmed" in resp.get_data(as_text=True)


# ── What the gate blocks, and what it must not ─────────────────────────────

def test_unverified_cannot_message_a_reporter(app, client, user, other_user, with_mail):
    pet = make_pet(other_user)
    login(client)
    resp = client.post(f"/pets/{pet.id}/contact",
                       data={"message": "I think I saw them near the shops."},
                       follow_redirects=True)

    assert "Confirm your email address first" in resp.get_data(as_text=True)
    from models import ContactMessage
    assert ContactMessage.query.count() == 0


def test_unverified_cannot_log_a_sighting(app, client, user, other_user, with_mail):
    pet = make_pet(other_user)
    login(client)
    resp = client.post(f"/pets/{pet.id}/sightings",
                       data={"lat": "-42.88", "lng": "147.33",
                             "seen_at": "2026-08-16T09:00"}, follow_redirects=True)

    assert "Confirm your email address first" in resp.get_data(as_text=True)
    from models import Sighting
    assert Sighting.query.count() == 0


def test_verified_users_can_do_both(app, client, user, other_user, with_mail):
    user.email_verified = True
    db.session.commit()
    pet = make_pet(other_user)
    login(client)

    client.post(f"/pets/{pet.id}/sightings",
                data={"lat": "-42.88", "lng": "147.33",
                      "seen_at": "2026-08-16T09:00"}, follow_redirects=True)
    client.post(f"/pets/{pet.id}/contact",
                data={"message": "I think I saw them near the shops."},
                follow_redirects=True)

    from models import ContactMessage, Sighting
    assert Sighting.query.count() == 1
    assert ContactMessage.query.count() == 1


def test_posting_a_report_is_never_gated(app, client, user, with_mail):
    """A pet on a road will not wait for an email round trip."""
    from datetime import datetime, timedelta
    login(client)
    resp = client.post("/pets/new", data={
        "report_type": "missing", "species": "cat", "name": "Raven",
        "colour": "black", "description": "Shy.", "locality": "New Town",
        "last_seen_at": (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "lat": "-42.8610617", "lng": "147.304103", "blur_location": "1",
    }, follow_redirects=True)

    assert resp.status_code == 200
    from models import Pet
    assert Pet.query.count() == 1


def test_the_gate_is_skipped_when_no_provider_is_configured(app, client, user, other_user):
    """A missing API key must not lock everyone out of a gate they cannot pass.

    No `with_mail` fixture here, so mail_is_configured() is False.
    """
    assert mailer.mail_is_configured() is False
    pet = make_pet(other_user)
    login(client)
    client.post(f"/pets/{pet.id}/contact",
                data={"message": "I think I saw them near the shops."},
                follow_redirects=True)

    from models import ContactMessage
    assert ContactMessage.query.count() == 1


# ── The prompt ─────────────────────────────────────────────────────────────

def test_banner_appears_only_when_it_can_be_acted_on(app, client, user, with_mail):
    login(client)
    assert "Confirm your email" in client.get("/").get_data(as_text=True)

    user.email_verified = True
    db.session.commit()
    assert "Confirm your email" not in client.get("/").get_data(as_text=True)


def test_no_banner_without_a_mail_provider(app, client, user):
    login(client)
    assert "Confirm your email" not in client.get("/").get_data(as_text=True)
