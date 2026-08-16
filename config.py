"""Configuration.

Everything is driven by environment variables so the same code runs locally
(SQLite, no Google key, no mail) and on Render (Postgres + Google Geocoding +
Resend). Nothing here is a secret; secrets arrive from the environment.
"""
import os

try:
    from dotenv import load_dotenv  # optional: load a local .env in dev
    load_dotenv()
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _database_url() -> str:
    """Return a SQLAlchemy URL, normalising Render's Postgres scheme."""
    url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "petmap.db"))
    # Render exposes 'postgres://', SQLAlchemy needs 'postgresql://'
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PREFERRED_URL_SCHEME = "https"

    # Session cookies. Secure is off locally (http://localhost has no TLS) and
    # on in production; SameSite=Lax still allows the normal top-level GET
    # navigations while blocking cross-site form posts.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

    # ── Geocoding ──────────────────────────────────────────────────────────
    # Optional. The map pin is the source of truth for a pet's location, so the
    # app is fully usable with no key at all — geocoding only powers the
    # "find an address" convenience box that moves the pin for you.
    GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
    GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    GEOCODE_SUFFIX = ", Tasmania, Australia"

    # ── Map extent ─────────────────────────────────────────────────────────
    # Tasmania, with a little water around it. The map opens here and pins
    # cannot be dropped outside it — a report in Queensland is a mistake, not
    # a feature, and silently accepting one poisons the map.
    TAS_BOUNDS = [[-43.75, 143.70], [-39.50, 148.50]]
    TAS_CENTRE = [-42.15, 146.60]

    # ── Privacy ────────────────────────────────────────────────────────────
    # A missing pet's "last seen" point is very often the owner's home, so the
    # public map shows a randomised point within this radius unless the owner
    # opts out. The offset is generated once and stored: re-rolling it on every
    # edit would let anyone watching average the samples back to the true point.
    #
    # 100 m is a deliberate trade, chosen so the map is precise enough to be
    # worth searching from. It is roughly a street block — narrower than the
    # 250 m this started at, and correspondingly less concealing.
    BLUR_RADIUS_M = float(os.environ.get("BLUR_RADIUS_M", "100"))

    # ── Search coverage ────────────────────────────────────────────────────
    # Public search tracks are published as grid cells rather than as the raw
    # GPS line: "was this street covered?" is the question people actually
    # have, cells merge cleanly across several searchers, and a precise trace
    # of everyone's movements is not something this site needs to publish.
    # The owner of the report, the searcher, and moderators see the real line.
    #
    # Note the interaction with BLUR_RADIUS_M above: at 50 m the coverage grid
    # resolves finer than the location blur, so a concentrated search does hint
    # at where the pet actually went missing. That is an accepted trade, not an
    # oversight — raise this or lower the blur if it ever proves too revealing.
    COVERAGE_CELL_M = float(os.environ.get("COVERAGE_CELL_M", "50"))
    # Cells are laid out on a single state-wide grid keyed to this latitude, so
    # every track snaps to the same cells and coverage from different searchers
    # lines up instead of interleaving.
    COVERAGE_GRID_REF_LAT = -42.15

    # A track usually starts and ends at the searcher's car or front door.
    # Both ends are trimmed before anything is stored — never merely hidden at
    # render time, so there is no exact copy to leak later. Short walks trim by
    # fraction instead, so something always survives.
    TRACK_TRIM_M = 200.0
    TRACK_TRIM_MAX_FRACTION = 0.25

    TRACK_MAX_POINTS = 20_000          # ~28 h at one fix every 5 s
    TRACK_MAX_BATCH_POINTS = 500       # per append request
    TRACK_MAX_ACTIVE_PER_USER = 3      # unfinished tracks before we say no
    COVERAGE_MAX_CELLS = 20_000        # cells returned by one map request

    # ── Limits ─────────────────────────────────────────────────────────────
    MAX_PHOTOS_PER_PET = 4
    MAX_ACTIVE_PETS_PER_USER = 20      # anti-spam, generous for a real user
    MAX_CONTACTS_PER_HOUR = 10         # messages one account may relay
    MAX_SIGHTINGS_PER_HOUR = 20
    MAP_RESULT_LIMIT = 2000            # markers returned by one /api/pets call
    DEFAULT_ACTIVE_DAYS = 180          # reports older than this are hidden by default

    MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "16"))
    MAX_CONTENT_LENGTH = int(MAX_UPLOAD_MB * 1024 * 1024)


# ── Email (Resend) ──────────────────────────────────────────────────────────
# Without a key, mailer.py logs messages instead of sending them, so password
# reset and owner-contact both work locally (check the server log for the link).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "PetMap Tasmania <onboarding@resend.dev>")

SITE_NAME = os.environ.get("SITE_NAME", "PetMap Tasmania")
