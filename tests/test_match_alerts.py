"""Alerting owners when an unidentified sighting turns up near their pet.

The counterpart to the matches page. Without this a sighting only reaches an
owner who happens to go looking, which is the wrong way round when the thing
that matters is hours.
"""
import time
from datetime import timedelta

import pytest
from conftest import login, make_pet, make_user

import mailer
from extensions import db
from models import Pet, Sighting
from services.localtime import now_utc


@pytest.fixture
def sent(monkeypatch):
    """Capture alerts instead of sending them, with mail treated as configured."""
    out = []
    monkeypatch.setattr(mailer, "mail_is_configured", lambda: True)
    monkeypatch.setattr(
        mailer, "send_match_alert",
        lambda to, pet_label, species, distance_km, when, description,
               sighting_url, matches_url: out.append(
                   {"to": to, "pet": pet_label, "km": distance_km,
                    "desc": description, "url": sighting_url}))
    # pets.py imports the module, so patching mailer covers the call site.
    import pets
    monkeypatch.setattr(pets.mailer, "send_match_alert", mailer.send_match_alert)
    monkeypatch.setattr(pets.mailer, "mail_is_configured", mailer.mail_is_configured)
    return out


def verified(user):
    user.email_verified = True
    db.session.commit()
    return user


def post_sighting(client, lat=-42.8615, lng=147.3045, species="cat", **extra):
    data = {"species": species, "seen_at": "2026-08-16T09:00",
            "lat": str(lat), "lng": str(lng), "description": "Black and white cat"}
    data.update(extra)
    return client.post("/sightings/new", data=data, follow_redirects=True)


def missing_cat(owner, **kw):
    """A missing cat, lost before the sighting so it could plausibly be it."""
    kw.setdefault("species", "cat")
    kw.setdefault("last_seen_at", now_utc() - timedelta(days=2))
    return make_pet(owner, **kw)


def test_a_nearby_owner_is_alerted(app, client, user, other_user, sent):
    verified(other_user)
    missing_cat(other_user)                    # New Town
    verified(user)
    login(client)
    post_sighting(client)                      # ~100 m away

    assert len(sent) == 1
    assert sent[0]["to"] == other_user.email
    assert sent[0]["km"] < 1


def test_the_flash_says_who_was_told(app, client, user, other_user, sent):
    verified(other_user); verified(user)
    missing_cat(other_user)
    login(client)
    resp = post_sighting(client)
    assert "the owner of a missing cat nearby has been emailed" in \
        resp.get_data(as_text=True)


def test_the_flash_counts_several_owners(app, client, user, sent):
    verified(user)
    for i in range(3):
        missing_cat(verified(make_user(email=f"owner{i}@example.com")))
    login(client)
    resp = post_sighting(client)
    assert "3 owners of missing cats nearby have been emailed" in \
        resp.get_data(as_text=True)


def test_a_distant_owner_is_not_alerted(app, client, user, other_user, sent):
    verified(other_user); verified(user)
    missing_cat(other_user, lat=-41.4391, lng=147.1358)     # Launceston
    login(client)
    post_sighting(client)
    assert sent == []


def test_a_different_species_is_not_alerted(app, client, user, other_user, sent):
    verified(other_user); verified(user)
    missing_cat(other_user, species="dog")
    login(client)
    post_sighting(client, species="cat")
    assert sent == []


def test_a_pet_lost_after_the_sighting_is_not_alerted(app, client, user,
                                                      other_user, sent):
    """It cannot be the animal someone saw before it went missing."""
    verified(other_user); verified(user)
    missing_cat(other_user, last_seen_at=now_utc() + timedelta(days=1))
    login(client)
    post_sighting(client)
    assert sent == []


def test_found_and_resolved_reports_are_not_alerted(app, client, user,
                                                    other_user, sent):
    verified(other_user); verified(user)
    missing_cat(other_user, report_type="found", name=None)
    reunited = missing_cat(other_user, name="Home")
    reunited.status = "reunited"
    db.session.commit()

    login(client)
    post_sighting(client)
    assert sent == []


def test_you_are_not_alerted_about_your_own_sighting(app, client, user, sent):
    verified(user)
    missing_cat(user)
    login(client)
    post_sighting(client)
    assert sent == []


def test_unverified_owners_are_not_emailed(app, client, user, other_user, sent):
    """An unverified address may not belong to the account holder."""
    verified(user)
    missing_cat(other_user)                    # other_user stays unverified
    login(client)
    post_sighting(client)
    assert sent == []


def test_banned_owners_are_not_emailed(app, client, user, other_user, sent):
    verified(other_user); verified(user)
    missing_cat(other_user)
    other_user.is_banned = True
    db.session.commit()

    login(client)
    post_sighting(client)
    assert sent == []


def test_nothing_is_sent_without_a_mail_provider(app, client, user, other_user,
                                                 monkeypatch):
    out = []
    monkeypatch.setattr(mailer, "send_match_alert", lambda *a, **k: out.append(1))
    verified(other_user); verified(user)
    missing_cat(other_user)
    login(client)
    post_sighting(client)
    assert out == []


# ── Budgets ────────────────────────────────────────────────────────────────

def test_a_pet_has_a_daily_alert_budget(app, client, user, other_user, sent):
    """Bounds what a bad actor posting sightings can do to one person."""
    app.config["MATCH_ALERT_MAX_PER_DAY"] = 2
    verified(other_user); verified(user)
    missing_cat(other_user)
    login(client)

    for i in range(4):
        post_sighting(client, lng=147.3045 + i * 0.0001)

    assert len(sent) == 2, "the third and fourth are over budget"


def test_the_budget_window_rolls_over(app, client, user, other_user, sent):
    app.config["MATCH_ALERT_MAX_PER_DAY"] = 1
    verified(other_user); verified(user)
    pet = missing_cat(other_user)
    login(client)

    post_sighting(client)
    assert len(sent) == 1

    db.session.get(Pet, pet.id).match_alert_window_start = (
        now_utc() - timedelta(hours=25))
    db.session.commit()

    post_sighting(client, lng=147.3046)
    assert len(sent) == 2


def test_one_sighting_does_not_fan_out_without_limit(app, client, user, sent):
    app.config["MATCH_ALERT_MAX_RECIPIENTS"] = 3
    verified(user)
    for i in range(6):
        owner = verified(make_user(email=f"owner{i}@example.com"))
        missing_cat(owner)

    login(client)
    post_sighting(client)
    assert len(sent) == 3


def test_a_slow_mail_provider_cannot_hang_the_request(app, client, user, sent,
                                                      monkeypatch):
    """Resend on a bad day must not cost the reporter a 502."""
    app.config["MATCH_ALERT_MAX_SECONDS"] = 0.05
    verified(user)
    for i in range(5):
        missing_cat(verified(make_user(email=f"owner{i}@example.com")))

    import pets
    real = pets.mailer.send_match_alert
    def slow(*a, **kw):
        real(*a, **kw)
        time.sleep(0.04)
    monkeypatch.setattr(pets.mailer, "send_match_alert", slow)

    login(client)
    resp = post_sighting(client)

    assert resp.status_code == 200
    assert 0 < len(sent) < 5, "it should stop early, not send all five"


def test_a_pet_skipped_at_the_deadline_keeps_its_budget(app, client, user, sent,
                                                        monkeypatch):
    """Nothing was sent on its behalf, so nothing should be spent."""
    app.config["MATCH_ALERT_MAX_SECONDS"] = 0.05
    verified(user)
    pets_ = [missing_cat(verified(make_user(email=f"o{i}@example.com")))
             for i in range(3)]

    import pets
    real = pets.mailer.send_match_alert
    monkeypatch.setattr(pets.mailer, "send_match_alert",
                        lambda *a, **kw: (real(*a, **kw), time.sleep(0.04)))

    login(client)
    post_sighting(client)

    spent = sum(db.session.get(Pet, p.id).match_alerts_sent for p in pets_)
    assert spent == len(sent), "budget spent must equal alerts actually sent"


def test_the_alert_carries_what_the_owner_needs(app, client, user, other_user, sent):
    verified(other_user); verified(user)
    missing_cat(other_user, name="Raven")
    login(client)
    post_sighting(client, description="Ginger tabby under a car")

    alert = sent[0]
    assert alert["pet"] == "Raven"
    assert "Ginger tabby under a car" in alert["desc"]
    assert "/sightings/" in alert["url"]
