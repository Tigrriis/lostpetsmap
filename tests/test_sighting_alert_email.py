"""What the owner is told when someone logs a sighting on their report."""
from datetime import timedelta

from conftest import login, make_pet

import pets
from services.localtime import format_local, now_utc, parse_local_input, to_input_value


def test_the_alert_carries_a_readable_local_time(app, client, user, other_user,
                                                 monkeypatch):
    """It used to pass the raw form string — "2026-09-12T14:30" — straight
    into the email, where it read like a log line."""
    sent = {}
    monkeypatch.setattr(pets, "send_sighting_alert",
                        lambda to, label, url, note, when: sent.update(when=when))
    pet = make_pet(user)
    typed = to_input_value(now_utc() - timedelta(hours=2))

    login(client, email=other_user.email)
    client.post(f"/pets/{pet.id}/sightings",
                data={"seen_at": typed, "lat": str(pet.lat), "lng": str(pet.lng),
                      "note": "By the creek"}, follow_redirects=True)

    assert sent["when"] == format_local(parse_local_input(typed))
    assert "T" not in sent["when"]


def test_the_flash_only_claims_an_email_when_one_was_sent(app, client, user,
                                                          monkeypatch):
    """Logging a sighting on your own report sends nothing — the flash used
    to say the reporter had been emailed regardless."""
    calls = []
    monkeypatch.setattr(pets, "send_sighting_alert", lambda *a, **k: calls.append(a))
    pet = make_pet(user)

    login(client)                                   # the owner themselves
    body = client.post(f"/pets/{pet.id}/sightings",
                       data={"seen_at": to_input_value(now_utc()),
                             "lat": str(pet.lat), "lng": str(pet.lng)},
                       follow_redirects=True).get_data(as_text=True)

    assert calls == []
    assert "has been emailed" not in body
    assert "Sighting added" in body
