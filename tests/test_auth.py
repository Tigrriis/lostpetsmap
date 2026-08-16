"""Accounts: registration, sign-in, and the password-reset token."""
from conftest import login, make_user

from auth import _make_reset_token, _user_from_reset_token
from extensions import db
from models import ROLE_ADMIN, User


def test_register_and_sign_in(app, client):
    client.post("/register", data={"email": "New@Example.com ",
                                   "password": "correcthorse",
                                   "display_name": "Sam"}, follow_redirects=True)
    user = User.query.one()
    assert user.email == "new@example.com"      # normalised
    assert user.display_name == "Sam"
    assert user.check_password("correcthorse")
    assert user.password_hash != "correcthorse"


def test_first_registered_account_becomes_admin(app, client):
    """Bootstrap: without this a fresh deployment has no way to appoint one."""
    resp = client.post("/register", data={"email": "founder@example.com",
                                          "password": "correcthorse"},
                       follow_redirects=True)
    founder = User.query.filter_by(email="founder@example.com").one()
    assert founder.role == ROLE_ADMIN
    assert founder.is_admin and founder.is_moderator
    assert "site admin" in resp.get_data(as_text=True)
    # And the moderation queue is actually reachable, which is the point.
    assert client.get("/moderate").status_code == 200


def test_later_accounts_are_ordinary_users(app, client):
    client.post("/register", data={"email": "founder@example.com",
                                   "password": "correcthorse"}, follow_redirects=True)
    client.get("/logout")
    resp = client.post("/register", data={"email": "second@example.com",
                                          "password": "correcthorse"},
                       follow_redirects=True)

    second = User.query.filter_by(email="second@example.com").one()
    assert second.role == "user"
    assert not second.is_moderator
    assert "site admin" not in resp.get_data(as_text=True)
    assert client.get("/moderate").status_code == 403


def test_existing_users_block_the_admin_claim(app, client, user):
    """No admin, but accounts already exist — a newcomer must not seize the role.

    The `user` fixture puts an ordinary account in the table first. Keying the
    claim on the lowest id (rather than "no admin exists") is what stops this.
    """
    client.post("/register", data={"email": "opportunist@example.com",
                                   "password": "correcthorse"}, follow_redirects=True)
    newcomer = User.query.filter_by(email="opportunist@example.com").one()
    assert newcomer.role == "user"
    assert User.query.filter_by(role=ROLE_ADMIN).count() == 0


def test_admin_claim_is_inert_once_an_admin_exists(app, client, moderator):
    """Even if the existing admin is not the lowest id, no second claim fires."""
    moderator.role = ROLE_ADMIN
    db.session.commit()

    client.post("/register", data={"email": "later@example.com",
                                   "password": "correcthorse"}, follow_redirects=True)
    assert User.query.filter_by(role=ROLE_ADMIN).count() == 1


def test_short_password_is_refused(app, client):
    client.post("/register", data={"email": "a@b.com", "password": "short"},
                follow_redirects=True)
    assert User.query.count() == 0


def test_duplicate_email_is_refused(app, client, user):
    client.post("/register", data={"email": "finder@example.com",
                                   "password": "anotherpassword"}, follow_redirects=True)
    assert User.query.count() == 1


def test_banned_user_cannot_sign_in(app, client, user):
    user.is_banned = True
    db.session.commit()
    resp = login(client)
    assert "suspended" in resp.get_data(as_text=True).lower()


def test_forgot_password_says_the_same_thing_either_way(app, client, user):
    """Otherwise the endpoint tells an attacker which addresses are registered."""
    known = client.post("/forgot-password", data={"email": "finder@example.com"},
                        follow_redirects=True).get_data(as_text=True)
    unknown = client.post("/forgot-password", data={"email": "nobody@example.com"},
                          follow_redirects=True).get_data(as_text=True)
    assert "a reset link is on its way" in known
    assert "a reset link is on its way" in unknown


def test_reset_token_is_single_use(app, user):
    token = _make_reset_token(user)
    assert _user_from_reset_token(token) is user

    user.set_password("a-brand-new-password")
    db.session.commit()
    # The hash changed, so the fingerprint baked into the token no longer matches.
    assert _user_from_reset_token(token) is None


def test_reset_token_rejects_tampering(app, user):
    token = _make_reset_token(user)
    assert _user_from_reset_token(token + "x") is None
    assert _user_from_reset_token("not-a-token") is None


def test_login_next_only_accepts_relative_paths(app, client, user):
    """An open redirect here would make a convincing phishing link."""
    resp = client.post("/login?next=https://evil.example/x",
                       data={"email": "finder@example.com", "password": "hunter2hunter2"})
    assert "evil.example" not in resp.headers["Location"]

    # That attempt succeeded, so drop the session — an already-authenticated
    # POST short-circuits to the map and would not exercise `next` at all.
    client.get("/logout")

    resp = client.post("/login?next=/mine",
                       data={"email": "finder@example.com", "password": "hunter2hunter2"})
    assert resp.headers["Location"].endswith("/mine")


def test_public_name_never_leaks_the_email_domain(app):
    user = make_user(email="someone@privatedomain.example")
    assert user.public_name == "someone"
    assert "privatedomain" not in user.public_name
