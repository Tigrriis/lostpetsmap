"""Pet reports: the map feed, CRUD, photos, sightings, and the contact relay.

Route map
---------
  GET  /api/pets                        GeoJSON feed for the map (public)
  GET  /api/geocode                     address -> pin position (signed in)
  GET  /pets/new                        report form
  POST /pets/new
  GET  /pets/<id>                       public detail page
  GET  /pets/<id>/edit                  owner or moderator
  POST /pets/<id>/edit
  POST /pets/<id>/status                mark reunited / reopen
  POST /pets/<id>/delete                soft delete, owner or moderator
  GET  /pets/<id>/photo/<photo_id>      image bytes (public, cacheable)
  POST /pets/<id>/photo/<photo_id>/delete
  POST /pets/<id>/sightings             log a sighting
  GET  /sightings/<id>/photo
  POST /pets/<id>/contact               relay a message to the reporter
  GET  /mine                            my reports

Two invariants worth stating once, because every route depends on them:

1. **Public surfaces filter ``is_removed``.** A moderator's removal is a soft
   delete; forgetting the filter un-removes it.
2. **Public surfaces serve ``public_lat``/``public_lng``.** The exact point goes
   only to the reporter and moderators. ``_may_see_exact`` is the one place that
   decides.
"""
from __future__ import annotations

from datetime import timedelta

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect,
    render_template, request, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import or_

from auth import active_user_required
from extensions import db
from mailer import send_owner_message, send_sighting_alert
from models import (
    MICROCHIP, REPORT_FOUND, REPORT_MISSING, REPORT_TYPES, SEXES, SIZES,
    SPECIES, STATUS_ACTIVE, STATUS_CLOSED, STATUS_REUNITED, STATUSES,
    TRACK_SOURCES, ContactMessage, Pet, PetPhoto, Sighting, recent_count,
)
from services import geocode as geocode_service
from services import images
from services.geo import bbox_around, parse_bbox, parse_latlng, within_bounds
from services.localtime import now_utc, parse_local_input, to_input_value
from services.localities import LOCALITIES

pets_bp = Blueprint("pets", __name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_pet_or_404(pet_id: int, include_removed: bool = False) -> Pet:
    pet = db.session.get(Pet, pet_id)
    if pet is None:
        abort(404)
    # A removed report is a 404 to the public and visible to moderators, so
    # moderation is reviewable without exposing removed content to everyone.
    if pet.is_removed and not include_removed:
        if not (current_user.is_authenticated and current_user.is_moderator):
            abort(404)
    return pet


def _may_see_exact(pet: Pet) -> bool:
    """Only the reporter and moderators get the un-blurred coordinates."""
    if not current_user.is_authenticated:
        return False
    return current_user.id == pet.user_id or current_user.is_moderator


def _require_owner(pet: Pet) -> None:
    if not pet.can_edit(current_user):
        abort(403)


def _form_choices() -> dict:
    """Everything the report form's selects need."""
    return {
        "report_types": REPORT_TYPES,
        "species": SPECIES,
        "sexes": SEXES,
        "sizes": SIZES,
        "microchip": MICROCHIP,
        "localities": LOCALITIES,
        "bounds": current_app.config["TAS_BOUNDS"],
        "centre": current_app.config["TAS_CENTRE"],
        "max_photos": current_app.config["MAX_PHOTOS_PER_PET"],
    }


def _clean(value: str | None, limit: int) -> str | None:
    text = (value or "").strip()
    return text[:limit] or None


def _form_values(pet: Pet, form) -> dict:
    """What the report form should render in each field.

    Submitted values win over stored ones, so a failed validation redisplays
    what the user actually typed instead of silently reverting their edits.
    ``form`` is the empty dict on a fresh GET.
    """
    def pick(key, stored, default=""):
        if form and key in form:
            return form.get(key)
        return stored if stored is not None else default

    checked = (lambda key, stored: bool(form.get(key)) if form else bool(stored))

    return {
        "report_type": pick("report_type", pet.report_type, REPORT_MISSING),
        "species": pick("species", pet.species, ""),
        "name": pick("name", pet.name),
        "breed": pick("breed", pet.breed),
        "colour": pick("colour", pet.colour),
        "sex": pick("sex", pet.sex, "unknown"),
        "size": pick("size", pet.size, ""),
        "microchipped": pick("microchipped", pet.microchipped, "unknown"),
        "collar": pick("collar", pet.collar),
        "description": pick("description", pet.description),
        "locality": pick("locality", pet.locality),
        "address_raw": pick("address_raw", pet.address_raw),
        "contact_phone": pick("contact_phone", pet.contact_phone),
        "show_phone": checked("show_phone", pet.show_phone),
        # blur defaults to on for a new report — see config.BLUR_RADIUS_M.
        "blur_location": checked("blur_location", True if pet.id is None else pet.blur_location),
        "lat": pick("lat", pet.lat),
        "lng": pick("lng", pet.lng),
        "last_seen_at": (form.get("last_seen_at") if form and "last_seen_at" in form
                         else to_input_value(pet.last_seen_at)),
    }


# ── Map feed ───────────────────────────────────────────────────────────────

@pets_bp.route("/api/pets")
def api_pets():
    """GeoJSON FeatureCollection for the map.

    Public and unauthenticated: browsing is the entire point of the service.
    Coordinates are always the public (possibly blurred) pair, including for the
    owner — the exact point belongs on the report's own page, not in a feed that
    anyone can scrape.
    """
    cfg = current_app.config
    q = Pet.query.filter(Pet.is_removed.is_(False))

    # Status. Active only by default; a reunited report stays visible for a
    # while because "she's home" is the outcome people come back to check.
    status = (request.args.get("status") or "active").lower()
    if status == "active":
        q = q.filter(Pet.status == STATUS_ACTIVE)
    elif status in STATUSES:
        q = q.filter(Pet.status == status)
    # "all" falls through with no status filter.

    report_type = (request.args.get("type") or "").lower()
    if report_type in REPORT_TYPES:
        q = q.filter(Pet.report_type == report_type)

    species = [s for s in request.args.getlist("species") if s in SPECIES]
    if species:
        q = q.filter(Pet.species.in_(species))

    # Age window, in days since last seen.
    try:
        days = int(request.args.get("days", cfg["DEFAULT_ACTIVE_DAYS"]))
    except (TypeError, ValueError):
        days = cfg["DEFAULT_ACTIVE_DAYS"]
    if days > 0:
        q = q.filter(Pet.last_seen_at >= now_utc() - timedelta(days=days))

    text = (request.args.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(Pet.name.ilike(like), Pet.breed.ilike(like),
                         Pet.colour.ilike(like), Pet.locality.ilike(like),
                         Pet.description.ilike(like)))

    bbox = parse_bbox(request.args.get("bbox"))
    if bbox:
        q = q.filter(Pet.public_lat.between(bbox.south, bbox.north),
                     Pet.public_lng.between(bbox.west, bbox.east))

    # Radius search ("pets near me"): narrow with the indexable box first, then
    # apply the true circle in Python over what survives.
    near = parse_latlng(request.args.get("lat"), request.args.get("lng"))
    radius_km = request.args.get("radius_km", type=float)
    if near and radius_km and radius_km > 0:
        box = bbox_around(near[0], near[1], radius_km * 1000.0)
        q = q.filter(Pet.public_lat.between(box.south, box.north),
                     Pet.public_lng.between(box.west, box.east))

    limit = cfg["MAP_RESULT_LIMIT"]
    rows = q.order_by(Pet.last_seen_at.desc()).limit(limit + 1).all()
    truncated = len(rows) > limit
    rows = rows[:limit]

    if near and radius_km and radius_km > 0:
        from services.geo import haversine_m
        radius_m = radius_km * 1000.0
        rows = [p for p in rows
                if haversine_m(near[0], near[1], p.public_lat, p.public_lng) <= radius_m]

    return jsonify({
        "type": "FeatureCollection",
        "features": [p.to_feature() for p in rows],
        "truncated": truncated,
    })


@pets_bp.route("/api/geocode")
@active_user_required
def api_geocode():
    """Resolve an address so the form can move the pin there.

    Signed-in only: it spends a metered Google call, and there is no reason an
    anonymous visitor needs it. Never returns an HTTP error for a failed lookup
    — the pin is still usable, so the client shows a hint instead.
    """
    result = geocode_service.geocode_detailed(request.args.get("address", ""))
    if result.coords:
        lat, lng = result.coords
        inside = within_bounds(lat, lng, current_app.config["TAS_BOUNDS"])
        return jsonify({
            "ok": True,
            "lat": lat, "lng": lng,
            "formatted": result.formatted,
            "locality": result.locality,
            "outside_bounds": not inside,
        })
    return jsonify({
        "ok": False,
        "status": result.status,
        "message": geocode_service.MESSAGES.get(result.status, "Address search failed."),
    })


# ── Create / edit ──────────────────────────────────────────────────────────

def _apply_form(pet: Pet, form, files) -> list[str]:
    """Populate ``pet`` from submitted form data. Returns a list of errors.

    Shared by create and edit so the two can never drift apart in what they
    validate. The caller commits only when the returned list is empty.
    """
    cfg = current_app.config
    errors: list[str] = []

    report_type = (form.get("report_type") or "").lower()
    if report_type not in REPORT_TYPES:
        errors.append("Choose whether this pet is missing or found.")
    else:
        pet.report_type = report_type

    species = (form.get("species") or "").lower()
    if species not in SPECIES:
        errors.append("Choose what kind of animal this is.")
    else:
        pet.species = species

    # Location. The pin is authoritative; the address is only ever a label.
    coords = parse_latlng(form.get("lat"), form.get("lng"))
    if coords is None:
        errors.append("Drop a pin on the map to set the location.")
    elif not within_bounds(coords[0], coords[1], cfg["TAS_BOUNDS"]):
        errors.append("That location is outside Tasmania — move the pin.")
    else:
        pet.set_location(coords[0], coords[1],
                         blur=bool(form.get("blur_location")),
                         radius_m=cfg["BLUR_RADIUS_M"])

    seen = parse_local_input(form.get("last_seen_at"))
    if seen is None:
        errors.append("Enter when the pet was last seen.")
    elif seen > now_utc() + timedelta(hours=1):
        # An hour of slack absorbs a phone clock that is slightly fast.
        errors.append("That date and time is in the future.")
    else:
        pet.last_seen_at = seen

    pet.name = _clean(form.get("name"), 80) if report_type == REPORT_MISSING else None
    pet.breed = _clean(form.get("breed"), 120)
    pet.colour = _clean(form.get("colour"), 120)
    pet.collar = _clean(form.get("collar"), 200)
    pet.description = _clean(form.get("description"), 4000)
    pet.locality = _clean(form.get("locality"), 120)
    pet.address_raw = _clean(form.get("address_raw"), 300)

    sex = (form.get("sex") or "unknown").lower()
    pet.sex = sex if sex in SEXES else "unknown"
    size = (form.get("size") or "").lower()
    pet.size = size if size in SIZES and size else None
    chip = (form.get("microchipped") or "unknown").lower()
    pet.microchipped = chip if chip in MICROCHIP else "unknown"

    pet.contact_phone = _clean(form.get("contact_phone"), 40)
    pet.show_phone = bool(form.get("show_phone")) and bool(pet.contact_phone)

    if report_type == REPORT_MISSING and not pet.name and not pet.description:
        errors.append("Give the pet's name or a description so people can recognise them.")
    if report_type == REPORT_FOUND and not pet.description and not pet.colour:
        errors.append("Describe the animal you found — colour at the very least.")

    # Photos. Errors here are appended but do not block the report: a lost pet
    # posted without a photo still beats no post at all.
    existing = len(pet.photos)
    room = cfg["MAX_PHOTOS_PER_PET"] - existing
    uploads = [f for f in files.getlist("photos") if f and f.filename]
    if uploads and room <= 0:
        errors.append(f"Already at the {cfg['MAX_PHOTOS_PER_PET']}-photo limit — "
                      "delete one before adding another.")
    for index, upload in enumerate(uploads[:max(room, 0)]):
        processed = images.process_photo(upload.read())
        if processed is None:
            errors.append(f"{upload.filename}: not a readable image.")
            continue
        pet.photos.append(PetPhoto(
            data=processed.data, thumb=processed.thumb,
            mimetype=processed.mimetype, sort_order=existing + index,
        ))

    return errors


@pets_bp.route("/pets/new", methods=["GET", "POST"])
@active_user_required
def new_pet():
    cfg = current_app.config
    if current_user.active_pet_count() >= cfg["MAX_ACTIVE_PETS_PER_USER"]:
        flash("You have a lot of open reports. Close one before posting another.", "error")
        return redirect(url_for("pets.my_pets"))

    pet = Pet(user_id=current_user.id)
    if request.method == "POST":
        errors = _apply_form(pet, request.form, request.files)
        # A photo problem alone should not throw away a filled-in form, but it
        # also should not be silently swallowed — flash it and carry on.
        blocking = [e for e in errors if "not a readable image" not in e]
        for message in errors:
            flash(message, "error")
        if not blocking:
            db.session.add(pet)
            db.session.commit()
            flash("Report posted. Share the link — that's what finds pets.", "success")
            return redirect(url_for("pets.pet_detail", pet_id=pet.id))
        return render_template("pet_form.html", pet=pet, is_new=True,
                               values=_form_values(pet, request.form), **_form_choices())

    return render_template("pet_form.html", pet=pet, is_new=True,
                           values=_form_values(pet, {}), **_form_choices())


@pets_bp.route("/pets/<int:pet_id>/edit", methods=["GET", "POST"])
@active_user_required
def edit_pet(pet_id: int):
    pet = _get_pet_or_404(pet_id, include_removed=True)
    _require_owner(pet)

    if request.method == "POST":
        errors = _apply_form(pet, request.form, request.files)
        blocking = [e for e in errors if "not a readable image" not in e]
        for message in errors:
            flash(message, "error")
        if not blocking:
            db.session.commit()
            flash("Report updated.", "success")
            return redirect(url_for("pets.pet_detail", pet_id=pet.id))
        # Discard the partial mutation, but redisplay what was typed. Reading
        # the submitted form (not the rolled-back row) is what keeps the user's
        # edits on screen.
        submitted = _form_values(pet, request.form)
        db.session.rollback()
        return render_template("pet_form.html", pet=pet, is_new=False,
                               values=submitted, **_form_choices())

    return render_template("pet_form.html", pet=pet, is_new=False,
                           values=_form_values(pet, {}), **_form_choices())


@pets_bp.route("/pets/<int:pet_id>/status", methods=["POST"])
@active_user_required
def set_status(pet_id: int):
    pet = _get_pet_or_404(pet_id, include_removed=True)
    _require_owner(pet)

    target = (request.form.get("status") or "").lower()
    if target not in STATUSES:
        abort(400)
    pet.status = target
    pet.resolved_at = now_utc() if target in (STATUS_REUNITED, STATUS_CLOSED) else None
    db.session.commit()

    if target == STATUS_REUNITED:
        flash("Marked as reunited — wonderful news.", "success")
    elif target == STATUS_CLOSED:
        flash("Report closed. It no longer appears on the map.", "info")
    else:
        flash("Report reopened.", "info")
    return redirect(url_for("pets.pet_detail", pet_id=pet.id))


@pets_bp.route("/pets/<int:pet_id>/delete", methods=["POST"])
@active_user_required
def delete_pet(pet_id: int):
    """Soft delete. The owner's own removals are theirs; moderators must give a reason."""
    pet = _get_pet_or_404(pet_id, include_removed=True)
    _require_owner(pet)

    pet.is_removed = True
    pet.removed_at = now_utc()
    pet.removed_by_id = current_user.id
    if current_user.id != pet.user_id:
        pet.removed_reason = _clean(request.form.get("reason"), 500) or "Removed by a moderator."
    db.session.commit()
    flash("Report removed.", "info")
    return redirect(url_for("pets.my_pets") if current_user.id == pet.user_id
                    else url_for("moderation.queue"))


# ── Detail ─────────────────────────────────────────────────────────────────

@pets_bp.route("/pets/<int:pet_id>")
def pet_detail(pet_id: int):
    pet = _get_pet_or_404(pet_id)
    exact = _may_see_exact(pet)
    return render_template(
        "pet_detail.html",
        pet=pet,
        exact=exact,
        lat=pet.lat if exact else pet.public_lat,
        lng=pet.lng if exact else pet.public_lng,
        sightings=pet.visible_sightings().all(),
        can_edit=pet.can_edit(current_user),
        species_label=SPECIES.get(pet.species, "Animal"),
        sex_label=SEXES.get(pet.sex, "Unknown"),
        size_label=SIZES.get(pet.size or "", ""),
        chip_label=MICROCHIP.get(pet.microchipped, "Unknown"),
        bounds=current_app.config["TAS_BOUNDS"],
        track_sources=TRACK_SOURCES,
        cell_m=current_app.config["COVERAGE_CELL_M"],
        trim_m=int(current_app.config["TRACK_TRIM_M"]),
    )


@pets_bp.route("/mine")
@login_required
def my_pets():
    rows = (Pet.query.filter_by(user_id=current_user.id, is_removed=False)
            .order_by(Pet.status.asc(), Pet.last_seen_at.desc()).all())
    return render_template("my_pets.html", pets=rows, statuses=STATUSES,
                           species=SPECIES, report_types=REPORT_TYPES)


# ── Photos ─────────────────────────────────────────────────────────────────

def _image_response(data: bytes, mimetype: str) -> Response:
    resp = Response(data, mimetype=mimetype)
    # Immutable in practice: a photo is never edited in place, only added or
    # deleted, so a long cache is safe and keeps the map cheap to browse.
    resp.headers["Cache-Control"] = "public, max-age=604800"
    resp.headers["Content-Length"] = str(len(data))
    return resp


@pets_bp.route("/pets/<int:pet_id>/photo/<int:photo_id>")
def pet_photo(pet_id: int, photo_id: int):
    pet = _get_pet_or_404(pet_id)
    photo = db.session.get(PetPhoto, photo_id)
    if photo is None or photo.pet_id != pet.id:
        abort(404)
    if request.args.get("size") == "thumb" and photo.thumb:
        return _image_response(photo.thumb, photo.mimetype)
    return _image_response(photo.data, photo.mimetype)


@pets_bp.route("/pets/<int:pet_id>/photo/<int:photo_id>/delete", methods=["POST"])
@active_user_required
def delete_photo(pet_id: int, photo_id: int):
    pet = _get_pet_or_404(pet_id, include_removed=True)
    _require_owner(pet)
    photo = db.session.get(PetPhoto, photo_id)
    if photo is None or photo.pet_id != pet.id:
        abort(404)
    db.session.delete(photo)
    db.session.commit()
    flash("Photo deleted.", "info")
    return redirect(url_for("pets.edit_pet", pet_id=pet.id))


@pets_bp.route("/sightings/<int:sighting_id>/photo")
def sighting_photo(sighting_id: int):
    sighting = db.session.get(Sighting, sighting_id)
    if sighting is None or sighting.is_removed or not sighting.photo:
        abort(404)
    if sighting.pet.is_removed:
        abort(404)
    return _image_response(sighting.photo, sighting.photo_mimetype or "image/jpeg")


# ── Sightings ──────────────────────────────────────────────────────────────

@pets_bp.route("/pets/<int:pet_id>/sightings", methods=["POST"])
@active_user_required
def add_sighting(pet_id: int):
    pet = _get_pet_or_404(pet_id)
    cfg = current_app.config

    if recent_count(Sighting, Sighting.user_id, current_user.id) >= cfg["MAX_SIGHTINGS_PER_HOUR"]:
        flash("That's a lot of sightings in an hour — try again later.", "error")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))

    coords = parse_latlng(request.form.get("lat"), request.form.get("lng"))
    seen = parse_local_input(request.form.get("seen_at"))
    note = _clean(request.form.get("note"), 1000)

    if coords is None or not within_bounds(coords[0], coords[1], cfg["TAS_BOUNDS"]):
        flash("Drop a pin where you saw the animal.", "error")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))
    if seen is None or seen > now_utc() + timedelta(hours=1):
        flash("Enter a valid date and time for the sighting.", "error")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))

    sighting = Sighting(pet_id=pet.id, user_id=current_user.id,
                        lat=coords[0], lng=coords[1], seen_at=seen, note=note)

    upload = request.files.get("photo")
    if upload and upload.filename:
        processed = images.process_single(upload.read())
        if processed is None:
            flash("That photo couldn't be read — the sighting was saved without it.", "error")
        else:
            sighting.photo, sighting.photo_mimetype = processed

    db.session.add(sighting)
    db.session.commit()

    # Tell the reporter. This is the whole reason someone bothers to log one.
    if pet.reporter and pet.reporter.id != current_user.id:
        send_sighting_alert(
            pet.reporter.email, pet.label,
            url_for("pets.pet_detail", pet_id=pet.id, _external=True),
            note or "", request.form.get("seen_at", ""),
        )

    flash("Sighting added — the person who posted this has been emailed.", "success")
    return redirect(url_for("pets.pet_detail", pet_id=pet.id))


@pets_bp.route("/sightings/<int:sighting_id>/delete", methods=["POST"])
@active_user_required
def delete_sighting(sighting_id: int):
    sighting = db.session.get(Sighting, sighting_id)
    if sighting is None:
        abort(404)
    # The person who logged it, the owner of the report, or a moderator.
    allowed = (current_user.id == sighting.user_id
               or current_user.id == sighting.pet.user_id
               or current_user.is_moderator)
    if not allowed:
        abort(403)
    sighting.is_removed = True
    sighting.removed_at = now_utc()
    sighting.removed_by_id = current_user.id
    db.session.commit()
    flash("Sighting removed.", "info")
    return redirect(url_for("pets.pet_detail", pet_id=sighting.pet_id))


# ── Contact relay ──────────────────────────────────────────────────────────

@pets_bp.route("/pets/<int:pet_id>/contact", methods=["POST"])
@active_user_required
def contact_reporter(pet_id: int):
    """Relay a message to whoever filed the report.

    Neither party's email address is ever rendered on the site. The sender's
    address goes into Reply-To, which is disclosed to the recipient by design —
    the form says so — and the body is logged so abuse can be traced.
    """
    pet = _get_pet_or_404(pet_id)
    cfg = current_app.config

    if pet.user_id == current_user.id:
        flash("That's your own report.", "info")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))

    if recent_count(ContactMessage, ContactMessage.sender_id,
                    current_user.id) >= cfg["MAX_CONTACTS_PER_HOUR"]:
        flash("You've sent a lot of messages in the last hour — try again later.", "error")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))

    body = _clean(request.form.get("message"), 2000)
    if not body or len(body) < 10:
        flash("Write a message first — say where and when you saw them.", "error")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))

    db.session.add(ContactMessage(pet_id=pet.id, sender_id=current_user.id, body=body))
    db.session.commit()

    send_owner_message(
        pet.reporter.email, pet.label,
        url_for("pets.pet_detail", pet_id=pet.id, _external=True),
        current_user.email, body,
    )
    flash("Message sent. They'll reply to your email address directly.", "success")
    return redirect(url_for("pets.pet_detail", pet_id=pet.id))
