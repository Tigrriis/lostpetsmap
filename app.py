"""
PetMap Tasmania — a public map of lost and found pets.

  /               the map (public)
  /pets/...       reports: create, view, edit, sightings, contact  (see pets.py)
  /mine           my reports
  /moderate       moderation queue (moderators)
  /login, /register, /account, /forgot-password, /reset-password/<token>

Anyone can browse. An account is needed to post a report, log a sighting, or
message a reporter — that is the whole authorisation model, plus a moderator
role on top for cleanup.

Module-level ``app`` rather than a factory, matching the rest of the Arete
Flask services, so ``gunicorn app:app`` in render.yaml works unchanged. The
tests build their own app via ``create_app()`` for isolation.
"""
import hashlib
import os
import threading

from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError, generate_csrf
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from auth import auth_bp
from extensions import csrf, db, login_manager, migrate
import mailer
from models import REPORT_TYPES, SPECIES, STATUSES
from moderation import moderation_bp
from pets import pets_bp
from services.localtime import format_local, humanise_age, to_local
from tracks import tracks_bp


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config.Config)
    if overrides:
        app.config.update(overrides)

    # Render terminates TLS at a proxy; trust it so url_for(_external=True)
    # produces https links in the emails we send.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(pets_bp)
    app.register_blueprint(tracks_bp)
    app.register_blueprint(moderation_bp)

    _register_asset_versioning(app)
    _register_template_helpers(app)
    _register_routes(app)
    _register_error_handlers(app)
    return app


# Content hash per static file, computed once per process. Keyed by filename;
# the files are a few tens of kilobytes, so hashing them on first use costs
# nothing measurable and saves stat-ing them on every request afterwards.
_ASSET_VERSIONS: dict[str, str] = {}


def _asset_version(app: Flask, filename: str) -> str | None:
    """Short content hash for a static file, or None if it cannot be read.

    Content rather than mtime: a redeploy rewrites every mtime, which would
    expire the whole cache each time even for files that never changed.
    """
    if not app.debug and filename in _ASSET_VERSIONS:
        return _ASSET_VERSIONS[filename]
    try:
        with open(os.path.join(app.static_folder, filename), "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:8]
    except OSError:
        # A missing asset is already a broken page; it should not also be a 500.
        return None
    _ASSET_VERSIONS[filename] = digest
    return digest


def _register_asset_versioning(app: Flask) -> None:
    """Append ?v=<hash> to every static URL, so a changed file gets a new one.

    Hooked into url_for rather than written into each template: every existing
    reference is covered, and so is every one added later.

    This is what makes the long cache lifetime below safe. Without it, a CSS fix
    is invisible to returning visitors until their copy expires — which behind a
    CDN can be hours after the deploy went out.
    """
    @app.url_defaults
    def add_version(endpoint, values):
        if endpoint != "static":
            return
        filename = values.get("filename")
        if not filename:
            return
        version = _asset_version(app, filename)
        if version:
            values["v"] = version


def _register_template_helpers(app: Flask) -> None:
    app.jinja_env.filters["localtime"] = format_local
    app.jinja_env.filters["ago"] = humanise_age
    app.jinja_env.filters["localdate"] = lambda v: format_local(v, "%-d %b %Y")

    @app.context_processor
    def inject_globals():
        return {
            "SITE_NAME": config.SITE_NAME,
            # Templates hide the "confirm your email" prompts when there is no
            # provider to send with — see auth.verified_required, which skips
            # the gate for the same reason.
            "MAIL_CONFIGURED": mailer.mail_is_configured(),
            "SPECIES": SPECIES,
            "REPORT_TYPES": REPORT_TYPES,
            "STATUSES": STATUSES,
            "csrf_token": generate_csrf,
            "to_local": to_local,
        }


def _register_routes(app: Flask) -> None:
    @app.route("/")
    def map_page():
        return render_template(
            "map.html",
            bounds=app.config["TAS_BOUNDS"],
            centre=app.config["TAS_CENTRE"],
            default_days=app.config["DEFAULT_ACTIVE_DAYS"],
            cell_m=app.config["COVERAGE_CELL_M"],
        )

    @app.route("/safety")
    def safety():
        """Scam and safety advice.

        Not decoration: lost-pet listings attract a specific, well-documented
        fraud — a stranger claims to have the animal and demands a transport
        fee up front. Anyone posting a report is linked here from the form.
        """
        return render_template("safety.html")

    @app.route("/healthz")
    def healthz():
        return {"ok": True}


def _register_error_handlers(app: Flask) -> None:
    def wants_json() -> bool:
        return request.path.startswith("/api/")

    @app.errorhandler(403)
    def forbidden(_exc):
        if wants_json():
            return jsonify({"error": "Not allowed."}), 403
        return render_template("error.html", code=403,
                               message="You don't have access to that."), 403

    @app.errorhandler(404)
    def not_found(_exc):
        if wants_json():
            return jsonify({"error": "Not found."}), 404
        return render_template("error.html", code=404,
                               message="That page or report doesn't exist."), 404

    @app.errorhandler(413)
    def too_large(_exc):
        limit = app.config["MAX_UPLOAD_MB"]
        if wants_json():
            return jsonify({"error": f"Upload larger than {limit:g} MB."}), 413
        return render_template("error.html", code=413,
                               message=f"That upload is bigger than {limit:g} MB. "
                                       "Photos straight from a phone are usually fine; "
                                       "a video is not."), 413

    @app.errorhandler(CSRFError)
    def csrf_error(exc):
        # Nearly always an expired session on a form left open overnight, so
        # say that rather than "CSRF token missing".
        if wants_json():
            return jsonify({"error": "Session expired — reload and try again."}), 400
        return render_template("error.html", code=400,
                               message="Your session expired while that page was open. "
                                       "Go back, reload, and try again."), 400

    @app.errorhandler(500)
    def server_error(_exc):
        if wants_json():
            return jsonify({"error": "Something went wrong."}), 500
        return render_template("error.html", code=500,
                               message="Something went wrong at our end."), 500


app = create_app()

# The schema is owned by Alembic. Migrations run two ways, belt and braces:
#   1. `python bootstrap_db.py` from the Render start command (the clean path).
#   2. Once, lazily, on the first request below.
# (2) exists because a start-command change in render.yaml only lands on a
# manual blueprint sync, while code deploys land on every push — so without it
# a migration added alongside a code change could sit unapplied.
_schema_lock = threading.Lock()
_schema_ready = False


@app.before_request
def _ensure_schema_once():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        try:
            from bootstrap_db import run_migrations
            run_migrations()
        except Exception:
            # Log loudly but don't wedge every request retrying a failure that
            # is almost certainly deterministic; the log is the signal to act.
            app.logger.exception("bootstrap_db: migration on first request failed")
        _schema_ready = True


if __name__ == "__main__":
    os.makedirs(os.path.join(config.BASE_DIR, "instance"), exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True, use_reloader=True)
