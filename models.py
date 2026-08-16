"""Database models for PetMap Tasmania.

The central record is ``Pet`` — one *report*, either "my pet is missing" or
"I found this animal". Everything else hangs off it: photos, sightings, and
relayed contact messages.

Two things in here are load-bearing and easy to get wrong later:

* **Exact vs public coordinates.** ``lat``/``lng`` are what the reporter
  actually pinned. ``public_lat``/``public_lng`` are what the world sees. For a
  missing pet those differ by a random offset, because "last seen" is usually
  the owner's front lawn. Serialise the public pair to anonymous callers, the
  exact pair only to the owner and moderators.
* **Soft deletion.** Nothing is ever hard-deleted by a moderator, so moderation
  is reversible and auditable. Every query that feeds a public surface must
  filter ``is_removed == False``.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db, login_manager

# ── Vocabularies ────────────────────────────────────────────────────────────
# Stored as short lower-case strings rather than enums: SQLite and Postgres
# disagree about native enums, and adding a species should not need a migration.

ROLE_USER = "user"
ROLE_MODERATOR = "moderator"
ROLE_ADMIN = "admin"

REPORT_MISSING = "missing"
REPORT_FOUND = "found"
REPORT_TYPES = {
    REPORT_MISSING: "Missing",
    REPORT_FOUND: "Found",
}

STATUS_ACTIVE = "active"
STATUS_REUNITED = "reunited"
STATUS_CLOSED = "closed"
STATUSES = {
    STATUS_ACTIVE: "Active",
    STATUS_REUNITED: "Reunited",
    STATUS_CLOSED: "Closed",
}

SPECIES = {
    "dog": "Dog",
    "cat": "Cat",
    "bird": "Bird",
    "rabbit": "Rabbit",
    "guinea_pig": "Guinea pig",
    "horse": "Horse",
    "reptile": "Reptile",
    "other": "Other",
}

SOURCE_FOOT = "on_foot"
SOURCE_VEHICLE = "vehicle"
SOURCE_DRONE = "drone"
TRACK_SOURCES = {
    SOURCE_FOOT: "On foot",
    SOURCE_VEHICLE: "By vehicle",
    SOURCE_DRONE: "Drone",
}

SEXES = {"unknown": "Unknown", "female": "Female", "male": "Male"}
SIZES = {"": "Not stated", "small": "Small", "medium": "Medium", "large": "Large"}
MICROCHIP = {"unknown": "Unknown", "yes": "Microchipped", "no": "Not microchipped"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime.

    SQLite hands back naive datetimes even for ``DateTime(timezone=True)``
    columns, so arithmetic against ``_utcnow()`` raises unless we normalise.
    Postgres returns aware values and this is a no-op.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _offset_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle metres. Local to avoid importing services from models."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))


def blur_point(lat: float, lng: float, radius_m: float) -> tuple[float, float]:
    """Return a point uniformly distributed in a disc of ``radius_m`` around it.

    sqrt() on the radius draw is what makes it *uniform over the area* — without
    it, points bunch towards the centre and the true location is still the most
    likely guess, which defeats the point of blurring at all.
    """
    theta = random.random() * 2.0 * math.pi
    r = radius_m * math.sqrt(random.random())
    dlat = (r * math.cos(theta)) / 111_320.0
    # Guard the pole case even though Tasmania is nowhere near it.
    coslat = math.cos(math.radians(lat))
    dlng = (r * math.sin(theta)) / (111_320.0 * coslat) if abs(coslat) > 1e-6 else 0.0
    return lat + dlat, lng + dlng


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    # Shown as the reporter's name on a pet page. Optional; falls back to the
    # part of the email before the @, never the full address.
    display_name = db.Column(db.String(80), nullable=True)

    # Email verification. The relay is the only way a finder reaches a reporter,
    # so an address nobody owns quietly breaks the core of the service — and an
    # unverified account is the cheap way to spam it. Actions that *reach other
    # people* are gated on this; posting your own report is not, because a lost
    # pet will not wait on an inbox.
    email_verified = db.Column(db.Boolean, nullable=False, default=False,
                               server_default=db.false())
    email_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    verification_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)

    role = db.Column(db.String(20), nullable=False, default=ROLE_USER, server_default=ROLE_USER)
    # db.false() rather than text("0"): SQLite accepts 0 for a boolean, Postgres
    # does not, and production is Postgres.
    is_banned = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    # Fixed-window counter for the one metered API this app calls. Two columns
    # rather than a log table on purpose: a row per lookup would grow without
    # bound and need pruning, to answer a question that only ever concerns the
    # last hour.
    geocode_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    geocode_window_start = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    pets = db.relationship("Pet", backref="reporter", lazy="dynamic",
                           foreign_keys="Pet.user_id")

    def take_geocode_slot(self, limit: int, window_hours: int = 1) -> bool:
        """Claim one metered lookup, or return False if the budget is spent.

        The caller commits. Only call this for lookups that will actually reach
        Google — a cache hit costs nothing and must not consume budget.
        """
        now = _utcnow()
        start = _as_aware(self.geocode_window_start)
        if start is None or (now - start) >= timedelta(hours=window_hours):
            self.geocode_window_start = now
            self.geocode_count = 0
        if self.geocode_count >= limit:
            return False
        self.geocode_count += 1
        return True

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_moderator(self) -> bool:
        return self.role in (ROLE_MODERATOR, ROLE_ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def public_name(self) -> str:
        return self.display_name or self.email.split("@")[0]

    def active_pet_count(self) -> int:
        return self.pets.filter_by(is_removed=False, status=STATUS_ACTIVE).count()


class Pet(db.Model):
    """One lost-or-found report."""

    __tablename__ = "pets"
    # Composite index on the coordinate pair: every map request is a bounding-box
    # scan, and without this it is a full table scan per pan.
    __table_args__ = (
        db.Index("ix_pets_bbox", "public_lat", "public_lng"),
        db.Index("ix_pets_visible", "is_removed", "status", "report_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    report_type = db.Column(db.String(16), nullable=False, index=True)   # missing | found
    status = db.Column(db.String(16), nullable=False, default=STATUS_ACTIVE,
                       server_default=STATUS_ACTIVE, index=True)

    # ── The animal ──
    species = db.Column(db.String(24), nullable=False, default="other")
    name = db.Column(db.String(80), nullable=True)        # missing pets only
    breed = db.Column(db.String(120), nullable=True)
    colour = db.Column(db.String(120), nullable=True)
    sex = db.Column(db.String(16), nullable=False, default="unknown", server_default="unknown")
    size = db.Column(db.String(16), nullable=True)
    microchipped = db.Column(db.String(16), nullable=False, default="unknown",
                             server_default="unknown")
    collar = db.Column(db.String(200), nullable=True)
    description = db.Column(db.Text, nullable=True)

    # ── Where and when ──
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    lat = db.Column(db.Float, nullable=False)          # exact, never public
    lng = db.Column(db.Float, nullable=False)
    public_lat = db.Column(db.Float, nullable=False)   # blurred when blur_location
    public_lng = db.Column(db.Float, nullable=False)
    blur_location = db.Column(db.Boolean, nullable=False, default=True,
                              server_default=db.true())
    address_raw = db.Column(db.String(300), nullable=True)   # what the user typed, if anything
    locality = db.Column(db.String(120), nullable=True, index=True)

    # ── Contact ──
    # The email is always the account's, and is never rendered. Phone is shown
    # only if the reporter ticks the box.
    contact_phone = db.Column(db.String(40), nullable=True)
    show_phone = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Fixed-window counter bounding "a sighting turned up near your pet" emails,
    # same shape as User.geocode_count. On the pet rather than the owner so one
    # busy report cannot silence alerts for another pet they also have out.
    match_alerts_sent = db.Column(db.Integer, nullable=False, default=0,
                                  server_default="0")
    match_alert_window_start = db.Column(db.DateTime(timezone=True), nullable=True)

    # ── Moderation (soft delete) ──
    is_removed = db.Column(db.Boolean, nullable=False, default=False,
                           server_default=db.false(), index=True)
    removed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    removed_reason = db.Column(db.String(500), nullable=True)
    removed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    removed_by = db.relationship("User", foreign_keys=[removed_by_id])
    photos = db.relationship("PetPhoto", backref="pet", lazy="select",
                             cascade="all, delete-orphan",
                             order_by="PetPhoto.sort_order")
    sightings = db.relationship("Sighting", backref="pet", lazy="dynamic",
                                cascade="all, delete-orphan")

    # ── Derived ──

    def set_location(self, lat: float, lng: float, blur: bool, radius_m: float) -> None:
        """Set the true point and refresh the public one only when it must.

        Re-rolling the offset on every save would leak the true location to
        anyone sampling the public feed repeatedly: the mean of many offsets
        converges on it. So the offset is regenerated only when the true point
        moves or the blur setting changes.
        """
        moved = (self.lat != lat) or (self.lng != lng)
        toggled = (self.blur_location != blur)
        # Self-heal when the stored offset no longer satisfies the configured
        # radius — which is what happens to every existing report the moment
        # BLUR_RADIUS_M is lowered. Checking here keeps "the public point is
        # within the current radius" true by construction, rather than only
        # until someone edits the setting.
        out_of_spec = (
            blur and self.public_lat is not None
            and _offset_m(lat, lng, self.public_lat, self.public_lng) > radius_m
        )
        self.lat, self.lng = lat, lng
        self.blur_location = blur
        if moved or toggled or out_of_spec or self.public_lat is None:
            if blur:
                self.public_lat, self.public_lng = blur_point(lat, lng, radius_m)
            else:
                self.public_lat, self.public_lng = lat, lng

    @property
    def label(self) -> str:
        """Short human name for the report, used in emails and page titles."""
        kind = SPECIES.get(self.species, "Animal")
        if self.report_type == REPORT_MISSING:
            return f"{self.name or kind} ({kind}, missing)" if self.name else f"Missing {kind.lower()}"
        return f"Found {kind.lower()}" + (f" — {self.locality}" if self.locality else "")

    @property
    def age_days(self) -> int:
        seen = _as_aware(self.last_seen_at)
        return max(0, (_utcnow() - seen).days) if seen else 0

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE and not self.is_removed

    @property
    def primary_photo(self):
        return self.photos[0] if self.photos else None

    def visible_sightings(self):
        return (self.sightings.filter_by(is_removed=False)
                .order_by(Sighting.seen_at.desc()))

    def take_match_alert_slot(self, limit: int, window_hours: int = 24) -> bool:
        """Claim one "sighting nearby" email, or False if today's budget is spent.

        The caller commits.
        """
        now = _utcnow()
        start = _as_aware(self.match_alert_window_start)
        if start is None or (now - start) >= timedelta(hours=window_hours):
            self.match_alert_window_start = now
            self.match_alerts_sent = 0
        if self.match_alerts_sent >= limit:
            return False
        self.match_alerts_sent += 1
        return True

    def can_edit(self, user) -> bool:
        """Owner or moderator. Anonymous users get False, not an exception."""
        if not user or not getattr(user, "is_authenticated", False):
            return False
        return user.id == self.user_id or user.is_moderator

    def to_feature(self, exact: bool = False) -> dict:
        """GeoJSON Feature for the map. ``exact`` is for the owner/moderator only."""
        lat = self.lat if exact else self.public_lat
        lng = self.lng if exact else self.public_lng
        photo = self.primary_photo
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                # Pets and standalone sightings share the map feed; `kind` is
                # what the client branches on. See Sighting.to_feature.
                "kind": "pet",
                "id": self.id,
                "report_type": self.report_type,
                "status": self.status,
                "species": self.species,
                "species_label": SPECIES.get(self.species, "Other"),
                "name": self.name,
                "breed": self.breed,
                "colour": self.colour,
                "locality": self.locality,
                "age_days": self.age_days,
                "approximate": bool(self.blur_location) and not exact,
                "thumb_url": f"/pets/{self.id}/photo/{photo.id}?size=thumb" if photo else None,
                "url": f"/pets/{self.id}",
            },
        }


class PetPhoto(db.Model):
    """A photo of the animal.

    Stored in the database rather than object storage: Render's filesystem is
    ephemeral, so anything written to disk vanishes on the next deploy. Images
    are resized to <=1280 px JPEG on upload (services/images.py), and a small
    square thumbnail is cached alongside so the map does not ship full-size
    photos into a popup.
    """

    __tablename__ = "pet_photos"

    id = db.Column(db.Integer, primary_key=True)
    pet_id = db.Column(db.Integer, db.ForeignKey("pets.id"), nullable=False, index=True)
    data = db.Column(db.LargeBinary, nullable=False)
    thumb = db.Column(db.LargeBinary, nullable=True)
    mimetype = db.Column(db.String(64), nullable=False, default="image/jpeg")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


class Sighting(db.Model):
    """Someone reports seeing an animal at a place.

    ``pet_id`` is nullable, which is the whole point of a *standalone* sighting:
    "loose dog on Elizabeth St, couldn't catch it". That is not a found report —
    the person does not have the animal — and it cannot be attached to a missing
    pet by someone who has no idea whose dog it is. Before this was nullable
    there was nowhere to put that observation at all.

    A standalone sighting can later be claimed by an owner who thinks it is
    their pet; see ``PetLink``. Claiming links it, and never moves it — the
    sighting still belongs to whoever logged it.
    """

    __tablename__ = "sightings"

    id = db.Column(db.Integer, primary_key=True)
    pet_id = db.Column(db.Integer, db.ForeignKey("pets.id"), nullable=True, index=True)
    # Only meaningful on a standalone sighting, where there is no report to
    # inherit it from. Lets the matcher shortlist by animal.
    species = db.Column(db.String(24), nullable=True)
    description = db.Column(db.String(500), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    lat = db.Column(db.Float, nullable=False)
    lng = db.Column(db.Float, nullable=False)
    seen_at = db.Column(db.DateTime(timezone=True), nullable=False)
    note = db.Column(db.String(1000), nullable=True)

    photo = db.Column(db.LargeBinary, nullable=True)
    photo_mimetype = db.Column(db.String(64), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, index=True)

    is_removed = db.Column(db.Boolean, nullable=False, default=False,
                           server_default=db.false(), index=True)
    removed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    removed_reason = db.Column(db.String(500), nullable=True)
    removed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    reporter = db.relationship("User", foreign_keys=[user_id])
    removed_by = db.relationship("User", foreign_keys=[removed_by_id])

    # Sightings are shown at their exact point on purpose: unlike a "last seen"
    # address, a street corner where a stray was spotted is not anybody's home.

    @property
    def has_photo(self) -> bool:
        return self.photo is not None

    @property
    def is_standalone(self) -> bool:
        return self.pet_id is None

    @property
    def is_visible(self) -> bool:
        """Should the public see this at all?

        A sighting attached to a removed report goes with it — hiding the
        report but leaving its sightings addressable would defeat the removal.
        A standalone sighting has no report to inherit from and stands on its
        own ``is_removed``.
        """
        if self.is_removed:
            return False
        return self.pet is None or not self.pet.is_removed

    def can_remove(self, user) -> bool:
        """The person who logged it, the owner of the report, or a moderator.

        One home for this, because ``pet_id`` is nullable: every call site that
        reached for ``sighting.pet.user_id`` itself was one AttributeError
        waiting for the first standalone sighting, and three of them were.
        """
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if user.id == self.user_id or user.is_moderator:
            return True
        return self.pet is not None and user.id == self.pet.user_id

    def to_feature(self) -> dict:
        """GeoJSON Feature for the main map, shaped like Pet.to_feature.

        The two share a feed so one bounding-box request returns everything in
        view and the client keeps a single render path; ``kind`` is what tells
        them apart. Coordinates are exact and unblurred, which is deliberate and
        unchanged from sightings elsewhere: a street where a stray was spotted
        is nobody's home address.
        """
        age = max(0, (_utcnow() - (_as_aware(self.seen_at) or _utcnow())).days)
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lng, self.lat]},
            "properties": {
                "kind": "sighting",
                "id": self.id,
                "species": self.species,
                "species_label": SPECIES.get(self.species, "Animal"),
                "description": self.description,
                "note": self.note,
                "age_days": age,
                "approximate": False,
                "thumb_url": f"/sightings/{self.id}/photo" if self.photo else None,
                "url": f"/sightings/{self.id}",
            },
        }


class SearchTrack(db.Model):
    """Where somebody looked, and when.

    One model for both producers: a volunteer walking with their phone, and a
    drone flight reconstructed from the GPS in its photos. They differ only in
    ``source`` and in how the points arrive, so the storage, privacy rules and
    map rendering are shared.

    Two representations are kept, deliberately:

    * ``points`` — the trimmed GPS line, shown only to the searcher, the pet's
      owner, and moderators. Both ends are cut *before* this is written (see
      services.coverage.trim_ends), because a track otherwise begins at
      somebody's front door.
    * ``cells`` — the 50 m grid squares the line passes through. This is what
      the public map draws. Precomputing it means rendering coverage never has
      to decompress a single track.

    A track is only published once finished; while it is running it belongs to
    its author alone.
    """

    __tablename__ = "search_tracks"
    __table_args__ = (
        db.Index("ix_tracks_bbox", "min_lat", "min_lng", "max_lat", "max_lng"),
        db.Index("ix_tracks_visible", "is_removed", "finished_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    pet_id = db.Column(db.Integer, db.ForeignKey("pets.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    source = db.Column(db.String(16), nullable=False, default=SOURCE_FOOT,
                       server_default=SOURCE_FOOT)
    notes = db.Column(db.String(500), nullable=True)

    started_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    # Null while the search is in progress. Also the "is this published?" flag.
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    points = db.Column(db.LargeBinary, nullable=True)   # zlib JSON [[lat,lng,ts],…]
    cells = db.Column(db.LargeBinary, nullable=True)    # zlib JSON [[ix,iy],…]
    point_count = db.Column(db.Integer, nullable=False, default=0)
    cell_count = db.Column(db.Integer, nullable=False, default=0)
    # Measured on the full path before trimming. A scalar distance discloses
    # nothing about location, and "we walked 4 km" is the stat people want.
    distance_m = db.Column(db.Float, nullable=False, default=0.0)

    # Denormalised extent of the *trimmed* path, so the map can find tracks in
    # a viewport without decompressing every row.
    min_lat = db.Column(db.Float, nullable=True)
    min_lng = db.Column(db.Float, nullable=True)
    max_lat = db.Column(db.Float, nullable=True)
    max_lng = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, index=True)

    is_removed = db.Column(db.Boolean, nullable=False, default=False,
                           server_default=db.false(), index=True)
    removed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    removed_reason = db.Column(db.String(500), nullable=True)
    removed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    pet = db.relationship("Pet", backref=db.backref("tracks", lazy="dynamic",
                                                    cascade="all, delete-orphan"))
    searcher = db.relationship("User", foreign_keys=[user_id])
    removed_by = db.relationship("User", foreign_keys=[removed_by_id])

    @property
    def is_live(self) -> bool:
        return self.finished_at is None and not self.is_removed

    @property
    def is_published(self) -> bool:
        return self.finished_at is not None and not self.is_removed

    @property
    def duration_s(self) -> int:
        start, end = _as_aware(self.started_at), _as_aware(self.finished_at)
        if not start:
            return 0
        return max(0, int(((end or _utcnow()) - start).total_seconds()))

    @property
    def duration_label(self) -> str:
        minutes = self.duration_s // 60
        if minutes < 60:
            return f"{minutes} min"
        return f"{minutes // 60}h {minutes % 60:02d}m"

    @property
    def distance_label(self) -> str:
        if self.distance_m < 1000:
            return f"{int(self.distance_m)} m"
        return f"{self.distance_m / 1000:.1f} km"

    def may_see_line(self, user) -> bool:
        """The precise line is for the searcher, the pet's owner, and moderators."""
        if not user or not getattr(user, "is_authenticated", False):
            return False
        return (user.id == self.user_id
                or user.id == self.pet.user_id
                or user.is_moderator)


class PetLink(db.Model):
    """An owner's claim that some other observation is their missing pet.

    Three things can be claimed, which is why there are two nullable targets
    rather than one:

    * a standalone sighting (``Sighting.pet_id`` is null),
    * a sighting logged on somebody else's report,
    * a "found" report.

    Exactly one of ``sighting_id`` / ``linked_pet_id`` is set — enforced by a
    check constraint, because a row with both or neither has no meaning and
    would render as a blank entry on somebody's page.

    A link never moves or edits what it points at. The sighting stays on the
    report it was logged against and still belongs to whoever logged it; the
    found report is untouched. Claiming is additive and reversible, so an owner
    guessing wrong costs nothing but a click to undo.
    """

    __tablename__ = "pet_links"
    __table_args__ = (
        db.UniqueConstraint("pet_id", "sighting_id", name="uq_pet_links_sighting"),
        db.UniqueConstraint("pet_id", "linked_pet_id", name="uq_pet_links_pet"),
        db.CheckConstraint(
            "(sighting_id IS NOT NULL AND linked_pet_id IS NULL) OR "
            "(sighting_id IS NULL AND linked_pet_id IS NOT NULL)",
            name="ck_pet_links_one_target"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # The report doing the claiming — the missing pet.
    pet_id = db.Column(db.Integer, db.ForeignKey("pets.id"), nullable=False, index=True)

    sighting_id = db.Column(db.Integer, db.ForeignKey("sightings.id"), nullable=True, index=True)
    linked_pet_id = db.Column(db.Integer, db.ForeignKey("pets.id"), nullable=True, index=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    pet = db.relationship("Pet", foreign_keys=[pet_id],
                          backref=db.backref("links", lazy="dynamic",
                                             cascade="all, delete-orphan"))
    sighting = db.relationship("Sighting", foreign_keys=[sighting_id])
    linked_pet = db.relationship("Pet", foreign_keys=[linked_pet_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class ContactMessage(db.Model):
    """Audit log of messages relayed from a finder to a reporter.

    The body is kept so an abuse report can be investigated — the relay is the
    one channel where one user's words reach another's inbox, and a channel
    with no record is a channel that cannot be moderated.
    """

    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    pet_id = db.Column(db.Integer, db.ForeignKey("pets.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    body = db.Column(db.String(2000), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, index=True)

    pet = db.relationship("Pet")
    sender = db.relationship("User", foreign_keys=[sender_id])


class GeocodeCache(db.Model):
    """Address -> coordinates, cached so repeat lookups don't cost a Google call."""

    __tablename__ = "geocode_cache"

    id = db.Column(db.Integer, primary_key=True)
    normalized_address = db.Column(db.String(500), unique=True, nullable=False, index=True)
    lat = db.Column(db.Float, nullable=False)
    lng = db.Column(db.Float, nullable=False)
    formatted = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


def photos_uploaded_since(user_id: int, hours: int = 24) -> int:
    """How many photos this account has stored recently, of any kind.

    Report photos and sighting photos share one budget, because they share one
    database. PetPhoto carries no user_id — it belongs to a pet — so this joins
    through Pet rather than denormalising an owner onto every image row.
    """
    since = _utcnow() - timedelta(hours=hours)

    on_reports = (db.session.query(PetPhoto.id)
                  .join(Pet, PetPhoto.pet_id == Pet.id)
                  .filter(Pet.user_id == user_id)
                  .filter(PetPhoto.created_at >= since)
                  .count())
    on_sightings = (db.session.query(Sighting.id)
                    .filter(Sighting.user_id == user_id)
                    .filter(Sighting.photo.isnot(None))
                    .filter(Sighting.created_at >= since)
                    .count())
    return on_reports + on_sightings


def recent_count(model, author_column, user_id: int, hours: int = 1) -> int:
    """How many rows this user created in the last ``hours`` — crude rate limiting.

    Counting rows beats an in-process counter because the app may run more than
    one worker, and a per-process counter is no limit at all. ``author_column``
    is passed explicitly rather than guessed: Sighting calls it ``user_id`` and
    ContactMessage calls it ``sender_id``.
    """
    since = _utcnow() - timedelta(hours=hours)
    return (db.session.query(model)
            .filter(model.created_at >= since)
            .filter(author_column == user_id)
            .count())


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))
