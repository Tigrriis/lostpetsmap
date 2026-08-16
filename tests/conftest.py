"""Test fixtures: an in-memory app, a client, and factories for users and pets.

CSRF is disabled for tests (WTF_CSRF_ENABLED=False) so form posts don't have to
scrape a token out of every page. The protection itself is exercised by the
templates in the browser, not here.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from extensions import db as _db  # noqa: E402
from models import Pet, User  # noqa: E402


@pytest.fixture
def app():
    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",   # in-memory
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test-key",
        "SERVER_NAME": "petmap.test",
        "GOOGLE_MAPS_API_KEY": "",
    })
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return _db


def make_user(email="finder@example.com", password="hunter2hunter2", role="user") -> User:
    user = User(email=email, role=role)
    user.set_password(password)
    _db.session.add(user)
    _db.session.commit()
    return user


def login(client, email="finder@example.com", password="hunter2hunter2"):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=True)


def make_pet(user, **overrides) -> Pet:
    """A valid missing-cat report in New Town, unless overridden."""
    fields = {
        "report_type": "missing",
        "species": "cat",
        "name": "Raven",
        "colour": "black and white",
        "description": "Maine coon, no collar, microchipped.",
        "locality": "New Town",
        "last_seen_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    fields.update(overrides)
    lat = fields.pop("lat", -42.8610617)
    lng = fields.pop("lng", 147.304103)
    blur = fields.pop("blur_location", True)

    pet = Pet(user_id=user.id, **fields)
    pet.set_location(lat, lng, blur=blur, radius_m=250.0)
    _db.session.add(pet)
    _db.session.commit()
    return pet


@pytest.fixture
def user(app):
    return make_user()


@pytest.fixture
def other_user(app):
    return make_user(email="owner@example.com")


@pytest.fixture
def moderator(app):
    return make_user(email="mod@example.com", role="moderator")
