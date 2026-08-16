"""Authentication: register, login, logout, password reset, account settings.

Adapted from the as3500design suite, minus the subscription gates — this site
has no paid tier. What it keeps is the signed, single-use password-reset token
and the neutral "if that email has an account" response, both of which exist so
the reset flow cannot be used to enumerate registered addresses.
"""
import hashlib
import hmac
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func

from extensions import db
import mailer
from mailer import send_email_verification, send_password_reset
from models import ROLE_ADMIN, User

auth_bp = Blueprint("auth", __name__)

RESET_TTL_SECONDS = 3600
RESET_SALT = "password-reset"
MIN_PASSWORD_LEN = 8

VERIFY_TTL_SECONDS = 48 * 3600
VERIFY_SALT = "email-verify"
VERIFY_RESEND_COOLDOWN_S = 60


def _safe_next(target: str) -> bool:
    """Only allow same-site relative redirects (guards against open redirect)."""
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.netloc and target.startswith("/") and not target.startswith("//")


def _claim_first_admin(user: User) -> bool:
    """Make the very first registered account the site admin.

    Roles are only grantable from /moderate, which requires an admin — so a
    fresh deployment has no way to appoint its first one without opening a
    shell. The first account to register claims it instead.

    ``user`` must already be flushed, because the decision is keyed on its id.
    Two guards, both necessary:

    * **No admin may already exist.** Once one does, this is inert forever, so
      a later registrant can never seize the role.
    * **This must be the lowest-numbered account.** Two registrations racing on
      an empty database would both see "no admin yet"; only one of them can
      hold the lowest id, so only one is promoted. It also means an existing
      population of ordinary users blocks the claim, which is the case that
      matters if the site somehow ran without an admin.

    Wiping the users table lets the next registrant bootstrap again. That is
    deliberate — it is the desired behaviour after a database reset, and it
    cannot be reached by a user, since accounts can be suspended but never
    deleted through the app.
    """
    if db.session.query(User.id).filter(User.role == ROLE_ADMIN).first() is not None:
        return False
    if db.session.query(func.min(User.id)).scalar() != user.id:
        return False
    user.role = ROLE_ADMIN
    return True


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("map_page"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        display_name = (request.form.get("display_name") or "").strip()[:80]
        if not email or "@" not in email:
            flash("Enter a valid email address.", "error")
        elif len(password) < MIN_PASSWORD_LEN:
            flash(f"Password must be at least {MIN_PASSWORD_LEN} characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered — sign in instead.", "error")
        else:
            user = User(email=email, display_name=display_name or None)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()          # assign the id the admin claim keys on
            promoted = _claim_first_admin(user)
            db.session.commit()

            if promoted:
                current_app.logger.warning(
                    "Bootstrapped %s as the site admin (first registered account).",
                    user.email)

            send_verification(user)

            login_user(user)
            if promoted:
                flash("Welcome — you're the first account here, so you're the site "
                      "admin. Moderation tools are in the top bar.", "success")
            else:
                flash("Welcome. You can post a lost or found pet now.", "success")
            if mailer.mail_is_configured():
                flash(f"Confirm your email — we've sent a link to {user.email}. "
                      "You can post straight away; confirming is what lets people "
                      "reach you.", "info")
            nxt = request.args.get("next")
            return redirect(nxt if _safe_next(nxt) else url_for("map_page"))
    return render_template("register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("map_page"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            if user.is_banned:
                # Say so plainly rather than pretending the password is wrong;
                # a banned user retrying forever helps nobody.
                flash("This account has been suspended. Contact the site admin.", "error")
                return render_template("login.html")
            login_user(user)
            nxt = request.args.get("next")
            return redirect(nxt if _safe_next(nxt) else url_for("map_page"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("map_page"))


@auth_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            current_user.display_name = (request.form.get("display_name") or "").strip()[:80] or None
            db.session.commit()
            flash("Profile updated.", "success")
        elif action == "password":
            current = request.form.get("current_password") or ""
            new = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not current_user.check_password(current):
                flash("That's not your current password.", "error")
            elif len(new) < MIN_PASSWORD_LEN:
                flash(f"Password must be at least {MIN_PASSWORD_LEN} characters.", "error")
            elif new != confirm:
                flash("Those passwords don't match.", "error")
            else:
                current_user.set_password(new)
                db.session.commit()
                flash("Password changed.", "success")
        return redirect(url_for("auth.account"))
    return render_template("account.html")


# ── Password reset ─────────────────────────────────────────────────────────
# Tokens are signed rather than stored, so no table and no cleanup job. The
# password hash is folded into the token, which makes it single-use.

def _reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=RESET_SALT)


def _password_fingerprint(user: User) -> str:
    """Short digest of the current password hash.

    Embedding this in the token makes it single-use: setting a new password
    changes the hash, so any token minted against the old one stops verifying.
    It also invalidates outstanding links whenever the password changes by any
    other route.
    """
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def _make_reset_token(user: User) -> str:
    return _reset_serializer().dumps({"uid": user.id, "fp": _password_fingerprint(user)})


def _user_from_reset_token(token: str):
    """Return the User a reset token is valid for, or None."""
    try:
        data = _reset_serializer().loads(token, max_age=RESET_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.get(User, data.get("uid"))
    if user is None:
        return None
    if not hmac.compare_digest(data.get("fp", ""), _password_fingerprint(user)):
        return None  # already used, or the password changed since it was issued
    return user


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("auth.account"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user:
            reset_url = url_for("auth.reset_password", token=_make_reset_token(user), _external=True)
            send_password_reset(user.email, reset_url, RESET_TTL_SECONDS // 60)
        # Always report the same outcome, so this can't be used to probe which
        # email addresses have accounts.
        flash("If that email has an account, a reset link is on its way.", "info")
        return redirect(url_for("auth.login"))
    return render_template("forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    user = _user_from_reset_token(token)
    if user is None:
        flash("That reset link is invalid or has expired — request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if len(password) < MIN_PASSWORD_LEN:
            flash(f"Password must be at least {MIN_PASSWORD_LEN} characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            user.set_password(password)
            db.session.commit()
            flash("Password updated — sign in with your new password.", "success")
            return redirect(url_for("auth.login"))

    return render_template("reset_password.html", token=token)


# ── Email verification ─────────────────────────────────────────────────────
# Signed, like the reset token, so there is no table and nothing to clean up.
# The address is baked into the token, which means a link minted for one email
# cannot confirm a different one after the account changes it.

def _verify_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=VERIFY_SALT)


def _make_verify_token(user: User) -> str:
    return _verify_serializer().dumps({"uid": user.id, "email": user.email})


def _user_from_verify_token(token: str):
    try:
        data = _verify_serializer().loads(token, max_age=VERIFY_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.get(User, data.get("uid"))
    if user is None:
        return None
    if not hmac.compare_digest(str(data.get("email", "")), user.email):
        return None            # address changed since the link was sent
    return user


def send_verification(user: User) -> bool:
    """Mint and send a confirmation link. False if it was too soon to resend."""
    now = datetime.now(timezone.utc)
    last = user.verification_sent_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < VERIFY_RESEND_COOLDOWN_S:
            return False

    user.verification_sent_at = now
    db.session.commit()
    send_email_verification(
        user.email,
        url_for("auth.verify_email", token=_make_verify_token(user), _external=True),
        VERIFY_TTL_SECONDS // 3600,
    )
    return True


@auth_bp.route("/verify-email/<token>")
def verify_email(token: str):
    user = _user_from_verify_token(token)
    if user is None:
        flash("That confirmation link is invalid or has expired. "
              "Sign in and request another.", "error")
        return redirect(url_for("auth.login"))

    if not user.email_verified:
        user.email_verified = True
        user.email_verified_at = datetime.now(timezone.utc)
        db.session.commit()

    # Confirming from the link is proof of address, so it is also a reasonable
    # moment to sign them in — it saves a step on the phone they opened it on.
    if not current_user.is_authenticated:
        login_user(user)
    flash("Email confirmed. You can message people and log sightings now.", "success")
    return redirect(url_for("map_page"))


@auth_bp.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    if current_user.email_verified:
        flash("That address is already confirmed.", "info")
    elif send_verification(current_user):
        flash(f"Confirmation link sent to {current_user.email}.", "success")
    else:
        flash("A link went out moments ago — check your inbox and spam folder.", "info")
    return redirect(request.referrer or url_for("auth.account"))


# ── Guards ─────────────────────────────────────────────────────────────────

def active_user_required(view):
    """Logged in and not banned.

    Ban is re-checked per request rather than only at login: a session issued
    before the ban would otherwise keep working until it expired.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.is_banned:
            flash("This account has been suspended.", "error")
            return redirect(url_for("map_page"))
        return view(*args, **kwargs)
    return wrapped


def verified_required(view):
    """Logged in, not banned, and holding a confirmed email address.

    For actions that put a message in somebody else's inbox. Posting your own
    report is deliberately *not* gated: someone whose dog is out on a road
    should not be waiting on an email round trip.

    Skipped entirely when no mail provider is configured. Otherwise a missing
    RESEND_API_KEY would lock every account out of these actions with no way to
    verify, which is a far worse failure than the spam this prevents.
    """
    @wraps(view)
    @active_user_required
    def wrapped(*args, **kwargs):
        if mailer.mail_is_configured() and not current_user.email_verified:
            message = ("Confirm your email address first — that's how people reach "
                       "you. Check your inbox, or request a new link from your account.")
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": message}), 403
            flash(message, "error")
            return redirect(request.referrer or url_for("auth.account"))
        return view(*args, **kwargs)
    return wrapped


def moderator_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_moderator:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped
