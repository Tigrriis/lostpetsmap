"""Re-blurring after the radius changes.

Lowering BLUR_RADIUS_M leaves existing reports at the old, wider offset,
because a stable offset is what stops repeated sampling of the public feed
from averaging back to the true point. These cover the two ways that gets
fixed: automatically on the next save, and on demand from the admin button.
"""
from conftest import login, make_pet, make_user

from extensions import db
from models import Pet, _offset_m


def test_lowering_the_radius_leaves_existing_reports_alone_until_asked(app, user):
    """The stable-offset rule still holds — nothing re-rolls by itself."""
    pet = make_pet(user, blur_location=True)
    original = (pet.public_lat, pet.public_lng)

    app.config["BLUR_RADIUS_M"] = 100.0
    # Merely reading, or saving something unrelated, must not move the point.
    pet.notes = None
    db.session.commit()
    assert (pet.public_lat, pet.public_lng) == original


def test_saving_a_report_self_heals_an_out_of_spec_offset(app, user):
    pet = make_pet(user, blur_location=True)          # blurred at the 250 m default
    # Force a wide offset, as a report created under the old radius would have.
    pet.public_lat = pet.lat + 0.002                  # ~220 m north
    db.session.commit()
    assert _offset_m(pet.lat, pet.lng, pet.public_lat, pet.public_lng) > 100

    pet.set_location(pet.lat, pet.lng, blur=True, radius_m=100.0)
    assert _offset_m(pet.lat, pet.lng, pet.public_lat, pet.public_lng) <= 100


def test_in_spec_offsets_are_not_disturbed(app, user):
    """Self-healing must not become a re-roll on every save."""
    pet = make_pet(user, blur_location=True)
    pet.set_location(pet.lat, pet.lng, blur=True, radius_m=100.0)
    settled = (pet.public_lat, pet.public_lng)

    for _ in range(5):
        pet.set_location(pet.lat, pet.lng, blur=True, radius_m=100.0)
        assert (pet.public_lat, pet.public_lng) == settled


def test_admin_button_reblurs_out_of_spec_reports(app, client, user, moderator):
    moderator.role = "admin"
    pet = make_pet(user, blur_location=True)
    pet.public_lat = pet.lat + 0.002
    db.session.commit()
    app.config["BLUR_RADIUS_M"] = 100.0

    login(client, email="mod@example.com")
    resp = client.post("/moderate/reblur", follow_redirects=True)

    assert "Re-blurred 1 report" in resp.get_data(as_text=True)
    fixed = db.session.get(Pet, pet.id)
    assert _offset_m(fixed.lat, fixed.lng, fixed.public_lat, fixed.public_lng) <= 100
    # The true location is untouched — only the published point moved.
    assert (fixed.lat, fixed.lng) == (pet.lat, pet.lng)


def test_reblur_is_a_no_op_when_everything_is_in_spec(app, client, moderator, user):
    moderator.role = "admin"
    make_pet(user, blur_location=True)
    db.session.commit()

    login(client, email="mod@example.com")
    resp = client.post("/moderate/reblur", follow_redirects=True)
    assert "Nothing to do" in resp.get_data(as_text=True)


def test_reblur_never_touches_unblurred_reports(app, client, moderator, user):
    """A report whose owner opted out of blurring must keep the exact point."""
    moderator.role = "admin"
    pet = make_pet(user, blur_location=False)
    exact = (pet.public_lat, pet.public_lng)
    app.config["BLUR_RADIUS_M"] = 100.0

    login(client, email="mod@example.com")
    client.post("/moderate/reblur", follow_redirects=True)

    fixed = db.session.get(Pet, pet.id)
    assert (fixed.public_lat, fixed.public_lng) == exact == (fixed.lat, fixed.lng)


def test_reblur_is_admin_only(app, client, user):
    login(client)
    assert client.post("/moderate/reblur").status_code == 403
