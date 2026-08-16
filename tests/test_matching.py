"""Standalone sightings, and an owner claiming one as their pet.

Two halves. A sighting no longer has to belong to a report — "loose dog on
Elizabeth St, couldn't catch it" had nowhere to go before. And the owner of a
missing pet can claim such a sighting, a sighting on someone else's report, or
a found report, as possibly theirs.

The invariant worth protecting: claiming *links*, it never moves or edits.
Whoever logged the sighting still owns it and it stays where they put it.
"""
from datetime import timedelta

import pytest
from conftest import login, make_pet, make_user

from extensions import db
from models import Pet, PetLink, Sighting
from services.localtime import now_utc


def standalone(user, lat=-42.8615, lng=147.3045, species="cat", when=None, **kw):
    s = Sighting(pet_id=None, user_id=user.id, lat=lat, lng=lng,
                 seen_at=when or now_utc(), species=species, **kw)
    db.session.add(s)
    db.session.commit()
    return s


# ── Standalone sightings ───────────────────────────────────────────────────

def test_a_sighting_can_belong_to_no_report(app, user):
    s = standalone(user, description="Ginger tabby, no collar")
    assert s.pet_id is None
    assert Sighting.query.filter(Sighting.pet_id.is_(None)).count() == 1


def test_posting_one_through_the_form(app, client, user):
    login(client)
    resp = client.post("/sightings/new", data={
        "species": "cat", "seen_at": "2026-08-16T09:00",
        "lat": "-42.8615", "lng": "147.3045",
        "description": "Ginger tabby", "note": "Ran under a fence.",
    }, follow_redirects=True)

    assert "Sighting posted" in resp.get_data(as_text=True)
    s = Sighting.query.one()
    assert s.pet_id is None and s.species == "cat"


@pytest.mark.parametrize("missing,field", [
    ({"species": ""}, "species"),
    ({"lat": "", "lng": ""}, "pin"),
    ({"seen_at": ""}, "time"),
])
def test_the_form_validates(app, client, user, missing, field):
    login(client)
    data = {"species": "cat", "seen_at": "2026-08-16T09:00",
            "lat": "-42.8615", "lng": "147.3045"}
    data.update(missing)
    client.post("/sightings/new", data=data, follow_redirects=True)
    assert Sighting.query.count() == 0, f"should reject a missing {field}"


def test_a_pin_outside_tasmania_is_refused(app, client, user):
    login(client)
    client.post("/sightings/new", data={
        "species": "cat", "seen_at": "2026-08-16T09:00",
        "lat": "-33.87", "lng": "151.21"}, follow_redirects=True)
    assert Sighting.query.count() == 0


# ── The shortlist ──────────────────────────────────────────────────────────

def test_matches_shortlist_by_species_distance_and_time(app, client, user, other_user):
    pet = make_pet(user, species="cat")          # New Town
    near = standalone(other_user, species="cat")                       # ~100 m
    wrong_species = standalone(other_user, species="dog")
    far = standalone(other_user, lat=-41.4391, lng=147.1358)           # Launceston
    old = standalone(other_user, when=pet.last_seen_at - timedelta(days=30))

    login(client)
    body = client.get(f"/pets/{pet.id}/matches").get_data(as_text=True)
    ids = [s.id for s in Sighting.query.all()]

    from pets import _match_candidates
    with app.test_request_context():
        found = _match_candidates(pet)
    shortlisted = {s.id for s in found["sightings"]}

    assert near.id in shortlisted
    assert wrong_species.id not in shortlisted, "different animal"
    assert far.id not in shortlisted, "200 km away"
    assert old.id not in shortlisted, "a month before it went missing"


def test_found_reports_are_shortlisted(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    found = make_pet(other_user, report_type="found", species="cat", name=None,
                     description="Ginger tabby, friendly.")
    from pets import _match_candidates
    with app.test_request_context():
        assert found.id in {f.id for f in _match_candidates(pet)["founds"]}


def test_the_pets_own_sightings_are_not_offered(app, client, user):
    pet = make_pet(user, species="cat")
    own = Sighting(pet_id=pet.id, user_id=user.id, lat=pet.lat, lng=pet.lng,
                   seen_at=now_utc())
    db.session.add(own)
    db.session.commit()

    from pets import _match_candidates
    with app.test_request_context():
        assert own.id not in {s.id for s in _match_candidates(pet)["sightings"]}


def test_matches_are_owner_only(app, client, user, other_user):
    pet = make_pet(other_user)
    login(client)
    assert client.get(f"/pets/{pet.id}/matches").status_code == 403


# ── Claiming ───────────────────────────────────────────────────────────────

def test_owner_can_claim_a_standalone_sighting(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    s = standalone(other_user)
    login(client)

    resp = client.post(f"/pets/{pet.id}/link",
                       data={"sighting_id": s.id}, follow_redirects=True)
    assert "Added to your report" in resp.get_data(as_text=True)
    assert PetLink.query.count() == 1


def test_claiming_never_moves_the_sighting(app, client, user, other_user):
    """It stays on whoever's report it was logged against, and stays theirs."""
    pet = make_pet(user, species="cat")
    theirs = make_pet(other_user, species="cat", name="Smudge")
    s = Sighting(pet_id=theirs.id, user_id=other_user.id,
                 lat=theirs.lat, lng=theirs.lng, seen_at=now_utc())
    db.session.add(s)
    db.session.commit()

    login(client)
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id},
                follow_redirects=True)

    refreshed = db.session.get(Sighting, s.id)
    assert refreshed.pet_id == theirs.id, "must not be reassigned"
    assert refreshed.user_id == other_user.id, "must not change hands"


def test_a_claimed_sighting_shows_on_the_report(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    standalone(other_user, note="Seen under a car on Pedder St")
    s = Sighting.query.one()
    login(client)
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id},
                follow_redirects=True)

    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "Seen under a car on Pedder St" in body
    assert "unlink" in body, "the owner needs a way to undo a wrong guess"


def test_a_claimed_entry_is_labelled_as_linked(app, client, user, other_user):
    """A claim is a guess; the timeline should not read it as a fresh report."""
    pet = make_pet(user, species="cat")
    s = standalone(other_user, note="Seen under a car")
    login(client)
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id}, follow_redirects=True)

    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "Linked by owner" in body


def test_a_moderators_link_is_not_credited_to_the_owner(app, client, user,
                                                        other_user, moderator):
    pet = make_pet(other_user, species="cat")
    s = standalone(user)
    login(client, email="mod@example.com")
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id}, follow_redirects=True)

    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "Linked by a moderator" in body
    assert "Linked by owner" not in body


def test_the_reports_own_sightings_carry_no_label(app, client, user):
    """Only claims are qualified — an ordinary sighting stays unadorned."""
    pet = make_pet(user, species="cat")
    db.session.add(Sighting(pet_id=pet.id, user_id=user.id, lat=pet.lat,
                            lng=pet.lng, seen_at=now_utc(), note="I saw her myself"))
    db.session.commit()

    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "I saw her myself" in body
    assert "Linked by" not in body


def test_a_claimed_found_report_shows_as_a_sighting(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    found = make_pet(other_user, report_type="found", species="cat", name=None,
                     description="Ginger tabby handed to the vet.")
    login(client)
    client.post(f"/pets/{pet.id}/link", data={"linked_pet_id": found.id},
                follow_redirects=True)

    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "Ginger tabby handed to the vet." in body


def test_claiming_twice_is_harmless(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    s = standalone(other_user)
    login(client)
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id}, follow_redirects=True)
    resp = client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id},
                       follow_redirects=True)

    assert "already linked" in resp.get_data(as_text=True)
    assert PetLink.query.count() == 1


def test_a_link_needs_exactly_one_target(app, client, user, other_user):
    pet = make_pet(user)
    s = standalone(other_user)
    login(client)
    assert client.post(f"/pets/{pet.id}/link", data={}).status_code == 400
    assert client.post(f"/pets/{pet.id}/link",
                       data={"sighting_id": s.id, "linked_pet_id": pet.id}).status_code == 400


def test_only_the_owner_can_claim(app, client, user, other_user):
    pet = make_pet(other_user)
    s = standalone(other_user)
    login(client)
    assert client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id}).status_code == 403
    assert PetLink.query.count() == 0


def test_unlinking(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    s = standalone(other_user)
    login(client)
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id}, follow_redirects=True)
    link = PetLink.query.one()

    client.post(f"/links/{link.id}/delete", follow_redirects=True)
    assert PetLink.query.count() == 0
    assert db.session.get(Sighting, s.id) is not None, "the sighting itself survives"


def test_only_the_owner_can_unlink(app, client, user, other_user, moderator):
    pet = make_pet(other_user, species="cat")
    s = standalone(other_user)
    db.session.add(PetLink(pet_id=pet.id, sighting_id=s.id,
                           created_by_id=other_user.id))
    db.session.commit()
    link = PetLink.query.one()

    login(client)                       # neither owner nor moderator
    assert client.post(f"/links/{link.id}/delete").status_code == 403
    assert PetLink.query.count() == 1


def test_a_removed_sighting_drops_out_of_the_report(app, client, user, other_user):
    pet = make_pet(user, species="cat")
    s = standalone(other_user, note="Seen by the creek")
    login(client)
    client.post(f"/pets/{pet.id}/link", data={"sighting_id": s.id}, follow_redirects=True)

    s.is_removed = True
    db.session.commit()
    assert "Seen by the creek" not in client.get(f"/pets/{pet.id}").get_data(as_text=True)
