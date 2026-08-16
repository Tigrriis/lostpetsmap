"""The privacy invariants — the ones that would be quietly wrong for months.

Location blurring and contact-detail exposure are the two places where a bug
does real harm to a real person rather than showing a wrong number, so they get
tests before anything else does.
"""
from conftest import login, make_pet, make_user

from models import blur_point
from services.geo import haversine_m


def test_blur_moves_the_public_point_but_keeps_the_real_one(app, user):
    pet = make_pet(user, blur_location=True)

    assert (pet.public_lat, pet.public_lng) != (pet.lat, pet.lng)
    offset = haversine_m(pet.lat, pet.lng, pet.public_lat, pet.public_lng)
    assert 0 < offset <= 250.0


def test_no_blur_publishes_the_exact_point(app, user):
    pet = make_pet(user, blur_location=False)
    assert (pet.public_lat, pet.public_lng) == (pet.lat, pet.lng)


def test_blur_offset_is_stable_across_saves(app, user):
    """Re-rolling the offset on every save would leak the true point.

    Anyone sampling the public feed repeatedly could average the samples back
    to the centre, so an unchanged location must keep an unchanged offset.
    """
    pet = make_pet(user, blur_location=True)
    first = (pet.public_lat, pet.public_lng)

    pet.set_location(pet.lat, pet.lng, blur=True, radius_m=250.0)
    assert (pet.public_lat, pet.public_lng) == first

    # Genuinely moving the pin must move the public point too.
    pet.set_location(-42.88, 147.32, blur=True, radius_m=250.0)
    assert (pet.public_lat, pet.public_lng) != first


def test_blur_points_are_spread_over_the_disc(app):
    """Uniform over area, not bunched at the centre."""
    lat, lng, radius = -42.86, 147.30, 250.0
    distances = [haversine_m(lat, lng, *blur_point(lat, lng, radius)) for _ in range(400)]

    assert max(distances) <= radius + 1e-6
    # With a uniform-over-area draw, half the points land beyond r/sqrt(2)
    # (~0.707r). A centre-bunched draw would put far fewer out there.
    outer = sum(1 for d in distances if d > radius * 0.707)
    assert 0.35 < outer / len(distances) < 0.65


def test_map_feed_never_serves_exact_coordinates(app, client, user):
    """Even to the owner: the feed is public and scrapeable."""
    pet = make_pet(user, blur_location=True)
    login(client)

    features = client.get("/api/pets").get_json()["features"]
    assert len(features) == 1
    lng, lat = features[0]["geometry"]["coordinates"]

    assert (lat, lng) == (pet.public_lat, pet.public_lng)
    assert (lat, lng) != (pet.lat, pet.lng)
    assert features[0]["properties"]["approximate"] is True


def test_detail_page_shows_exact_point_to_the_owner_only(app, client, user, other_user):
    pet = make_pet(user, blur_location=True)
    exact = f"{pet.lat}"

    # Anonymous
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert exact not in body

    # A different signed-in user
    login(client, email="owner@example.com")
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert exact not in body
    client.get("/logout")

    # The owner
    login(client)
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert exact in body


def test_email_addresses_are_never_rendered(app, client, user, other_user):
    pet = make_pet(other_user)          # owner@example.com posted it
    login(client)                       # finder@example.com is viewing

    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "owner@example.com" not in body
    # The viewer's own address may appear (it's theirs, in the contact form's
    # explanation of what the recipient will see).
    assert "finder@example.com" in body


def test_phone_hidden_unless_the_reporter_opted_in(app, client, user):
    pet = make_pet(user, contact_phone="0400 000 000", show_phone=False)
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "0400 000 000" not in body

    pet.show_phone = True
    from extensions import db
    db.session.commit()
    body = client.get(f"/pets/{pet.id}").get_data(as_text=True)
    assert "0400 000 000" in body
