"""Who removed a report is a fact worth keeping.

A moderator's removal carries their id and a reason. An owner pressing Remove
on a report that is already gone must not overwrite that record with their
own id — it is the one thing an appeal needs.
"""
from conftest import login, make_pet

from extensions import db
from models import Pet


def test_an_owner_cannot_overwrite_a_moderators_removal(app, client, user, moderator):
    pet = make_pet(user)

    login(client, email="mod@example.com")
    client.post(f"/moderate/pet/{pet.id}/remove", data={"reason": "Scam"},
                follow_redirects=True)
    client.get("/logout")

    login(client)                                   # the owner
    resp = client.post(f"/pets/{pet.id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert "already removed" in resp.get_data(as_text=True)

    pet = db.session.get(Pet, pet.id)
    assert pet.is_removed
    assert pet.removed_by_id == moderator.id
    assert pet.removed_reason == "Scam"


def test_an_owner_can_still_remove_their_own_live_report(app, client, user):
    pet = make_pet(user)
    login(client)
    client.post(f"/pets/{pet.id}/delete", follow_redirects=True)
    pet = db.session.get(Pet, pet.id)
    assert pet.is_removed
    assert pet.removed_by_id == user.id
    assert pet.removed_reason is None               # owners give no reason
