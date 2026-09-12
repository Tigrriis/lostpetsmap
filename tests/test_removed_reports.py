"""A removed report takes its sightings with it — everywhere, not just on its
own page.

Sighting.is_visible encodes the rule. These check that the places which reach
a sighting sideways — through a link, or the matches shortlist — ask it, rather
than only checking the sighting's own is_removed flag.
"""
from datetime import timedelta

from conftest import login, make_pet, make_user

from extensions import db
from models import PetLink, Sighting
from services.localtime import now_utc


def sighting_on(pet, reporter, **kw):
    s = Sighting(pet_id=pet.id, user_id=reporter.id, lat=pet.lat, lng=pet.lng,
                 seen_at=kw.pop("seen_at", now_utc()), note="Saw them", **kw)
    db.session.add(s)
    db.session.commit()
    return s


def remove(pet):
    pet.is_removed = True
    db.session.commit()


def test_a_linked_sighting_disappears_when_its_report_is_removed(
        app, client, user, other_user):
    mine = make_pet(user, species="cat")
    theirs = make_pet(other_user, species="cat", name="Smudge")
    s = sighting_on(theirs, other_user)
    db.session.add(PetLink(pet_id=mine.id, sighting_id=s.id, created_by_id=user.id))
    db.session.commit()

    login(client)
    assert "Saw them" in client.get(f"/pets/{mine.id}").get_data(as_text=True)

    remove(theirs)
    body = client.get(f"/pets/{mine.id}").get_data(as_text=True)
    assert "Saw them" not in body
    # and no "where it was posted" link to a page that now 404s
    assert f"/pets/{theirs.id}" not in body


def test_the_matches_shortlist_skips_sightings_on_removed_reports(
        app, client, user, other_user):
    mine = make_pet(user, species="cat")
    theirs = make_pet(other_user, species="cat", name="Smudge")
    s = sighting_on(theirs, other_user)

    from pets import _match_candidates
    with app.test_request_context():
        assert s.id in {x.id for x in _match_candidates(mine)["sightings"]}
    remove(theirs)
    with app.test_request_context():
        assert s.id not in {x.id for x in _match_candidates(mine)["sightings"]}


def test_a_sighting_on_a_removed_report_cannot_be_claimed(app, client, user,
                                                          other_user):
    mine = make_pet(user, species="cat")
    theirs = make_pet(other_user, species="cat", name="Smudge")
    s = sighting_on(theirs, other_user)
    remove(theirs)

    login(client)
    resp = client.post(f"/pets/{mine.id}/link", data={"sighting_id": s.id})
    assert resp.status_code == 404
    assert PetLink.query.count() == 0


def test_a_standalone_sighting_is_unaffected_by_all_this(app, client, user, other_user):
    """No report to inherit from; its own flag is the whole story."""
    mine = make_pet(user, species="cat")
    s = Sighting(pet_id=None, user_id=other_user.id, species="cat",
                 lat=mine.lat, lng=mine.lng, seen_at=now_utc())
    db.session.add(s)
    db.session.commit()
    assert s.is_visible

    from pets import _match_candidates
    with app.test_request_context():
        assert s.id in {x.id for x in _match_candidates(mine)["sightings"]}
