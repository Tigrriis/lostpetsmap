"""Search tracks: live GPS logging, drone photo import, and the coverage layer.

Route map
---------
  POST /pets/<id>/tracks              start a live track          (JSON)
  POST /tracks/<id>/points            append a batch of fixes     (JSON)
  POST /tracks/<id>/finish            trim, cell, publish         (JSON)
  POST /tracks/<id>/delete            soft delete
  POST /pets/<id>/tracks/drone        import a drone sortie from photo EXIF
  GET  /pets/<id>/tracks.geojson      tracks for one report's map
  GET  /api/coverage                  coverage cells for the main map

The privacy model, in one place:

* A track is invisible to everyone but its author until it is finished.
* Once finished, the **cells** are public; the **line** is not. Only the
  searcher, the pet's owner, and moderators ever receive the line.
* Both ends of the line are trimmed before it is stored at all, so the untrimmed
  path never exists in the database to be leaked later.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for,
)
from flask_login import current_user

from auth import active_user_required
from extensions import db
from models import (
    SOURCE_DRONE, SOURCE_FOOT, TRACK_SOURCES, Pet, SearchTrack,
)
from services import coverage, exif_track
from services.geo import parse_bbox, within_bounds
from services.localtime import format_local, now_utc

tracks_bp = Blueprint("tracks", __name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _pet_or_404(pet_id: int) -> Pet:
    pet = db.session.get(Pet, pet_id)
    if pet is None or (pet.is_removed and not
                       (current_user.is_authenticated and current_user.is_moderator)):
        abort(404)
    return pet


def _own_track_or_404(track_id: int) -> SearchTrack:
    """A track the caller is allowed to write to — that means its author only.

    Moderators can remove a track but never append to one; ownership of the
    recording is what makes "this is where I searched" mean anything.
    """
    track = db.session.get(SearchTrack, track_id)
    if track is None or track.is_removed:
        abort(404)
    if track.user_id != current_user.id:
        abort(403)
    return track


def _cfg(key):
    return current_app.config[key]


def _valid_fix(raw) -> list[float] | None:
    """Coerce one client-supplied fix to ``[lat, lng, epoch_seconds]``."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        lat, lng = float(raw[0]), float(raw[1])
        ts = int(raw[2]) if len(raw) > 2 else 0
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if lat != lat or lng != lng:          # NaN
        return None
    if not within_bounds(lat, lng, _cfg("TAS_BOUNDS")):
        return None
    return [lat, lng, ts]


def _incoming_points():
    """The points from a batch request, as a list, or None if malformed.

    Two encodings are accepted, and the second is not redundant. When the tab
    is closing the client uses ``navigator.sendBeacon``, which cannot set the
    X-CSRFToken header — so that request arrives as multipart form data with
    the token as an ordinary field, which is the one form Flask-WTF will
    validate without a header.
    """
    body = request.get_json(silent=True)
    if isinstance(body, dict) and isinstance(body.get("points"), list):
        return body["points"]

    raw = request.form.get("points")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if isinstance(parsed, list):
            return parsed
    return None


def _finalise(track: SearchTrack, trim: bool = True) -> None:
    """Trim, measure, and compute coverage cells. Idempotent.

    ``trim`` is off for drone imports, and that is a deliberate asymmetry. A
    live phone track begins the instant the button is pressed — at the
    searcher's home or car, which is exactly what the trim exists to hide. A
    flight is assembled afterwards from photos the operator chose to upload,
    and its launch point is somewhere they picked *for this search*; cutting
    the first and last 200 m would delete real coverage from a short sortie
    while protecting nothing. The upload form says the launch point will show,
    so the operator can leave those photos out if it happens to be their yard.
    """
    points = coverage.decode_points(track.points)

    # Distance comes from the *full* path — a scalar leaks no location, and
    # under-reporting how far someone walked would be a shame.
    track.distance_m = coverage.path_length_m(points)

    if track.distance_m < _cfg("TRACK_MIN_PUBLISH_M"):
        trimmed = []          # too short to be coverage; see the config note
    elif trim:
        trimmed = coverage.trim_ends(points, _cfg("TRACK_TRIM_M"),
                                     _cfg("TRACK_TRIM_MAX_FRACTION"))
    else:
        trimmed = [list(p) for p in points]
    track.points = coverage.encode_points(trimmed) if trimmed else None
    track.point_count = len(trimmed)

    cells = coverage.cells_for_path(trimmed, _cfg("COVERAGE_CELL_M"),
                                    _cfg("COVERAGE_GRID_REF_LAT"))
    track.cells = coverage.encode_cells(cells) if cells else None
    track.cell_count = len(cells)

    box = coverage.bbox_of(trimmed)
    if box:
        track.min_lat, track.min_lng, track.max_lat, track.max_lng = box
    else:
        track.min_lat = track.min_lng = track.max_lat = track.max_lng = None


def _when_label(track: SearchTrack) -> str:
    """"16 Aug, 1:07 PM – 2:22 PM", collapsing to one time if they match.

    Built here rather than joined in the browser: the start carries a date and
    the end does not, so comparing the two formatted strings client-side never
    finds them equal and a zero-length search renders as "2:38 PM – 2:38 PM".
    For a drone flight both ends come from the photos' own EXIF, so this is the
    window the area was actually overflown.
    """
    started = format_local(track.started_at, "%-d %b, %-I:%M %p")
    if not track.finished_at:
        return started
    finished = format_local(track.finished_at, "%-I:%M %p")
    same_minute = format_local(track.started_at, "%-I:%M %p") == finished
    return started if same_minute else f"{started} – {finished}"


def _track_summary(track: SearchTrack) -> dict:
    return {
        "id": track.id,
        "source": track.source,
        "source_label": TRACK_SOURCES.get(track.source, "Search"),
        "searcher": track.searcher.public_name if track.searcher else "",
        # Formatted server-side in Australia/Hobart, like every other time on
        # the site. Letting the browser format it would show a visitor from
        # interstate a different clock than the report page beside it.
        #
        # For a drone flight these two come from the first and last photo's
        # EXIF, so the pair is the actual window the area was overflown — the
        # question someone reading the coverage map is really asking.
        "started_label": format_local(track.started_at, "%-d %b, %-I:%M %p"),
        "finished_label": format_local(track.finished_at, "%-I:%M %p"),
        "when_label": _when_label(track),
        "started_at": track.started_at.isoformat() if track.started_at else None,
        "finished_at": track.finished_at.isoformat() if track.finished_at else None,
        "duration": track.duration_label,
        "distance": track.distance_label,
        "cell_count": track.cell_count,
        "notes": track.notes,
    }


# ── Live tracking ──────────────────────────────────────────────────────────

@tracks_bp.route("/pets/<int:pet_id>/tracks", methods=["POST"])
@active_user_required
def start_track(pet_id: int):
    pet = _pet_or_404(pet_id)

    live = SearchTrack.query.filter_by(user_id=current_user.id, is_removed=False,
                                       finished_at=None).count()
    if live >= _cfg("TRACK_MAX_ACTIVE_PER_USER"):
        return jsonify({"error": "You already have searches running. Finish one first."}), 409

    body = request.get_json(silent=True) or {}
    source = body.get("source")
    track = SearchTrack(
        pet_id=pet.id,
        user_id=current_user.id,
        source=source if source in TRACK_SOURCES else SOURCE_FOOT,
        started_at=now_utc(),
        points=coverage.encode_points([]),
    )
    db.session.add(track)
    db.session.commit()
    return jsonify({"ok": True, "track_id": track.id}), 201


@tracks_bp.route("/tracks/<int:track_id>/points", methods=["POST"])
@active_user_required
def append_points(track_id: int):
    """Append a batch of fixes to a running track.

    Batched rather than one-at-a-time because a phone browser is suspended the
    moment its screen locks; the client buffers locally and flushes every
    ~30 s, so a suspension costs nothing already captured.
    """
    track = _own_track_or_404(track_id)
    if track.finished_at is not None:
        return jsonify({"error": "That search is already finished."}), 409

    incoming = _incoming_points()
    if incoming is None:
        return jsonify({"error": "Expected a list of points."}), 400
    if len(incoming) > _cfg("TRACK_MAX_BATCH_POINTS"):
        return jsonify({"error": "Too many points in one batch."}), 413

    existing = coverage.decode_points(track.points)
    accepted = 0
    for raw in incoming:
        fix = _valid_fix(raw)
        if fix is None:
            continue                       # a bad fix is dropped, not fatal
        if len(existing) >= _cfg("TRACK_MAX_POINTS"):
            break
        existing.append(fix)
        accepted += 1

    track.points = coverage.encode_points(existing)
    track.point_count = len(existing)
    db.session.commit()

    return jsonify({"ok": True, "accepted": accepted, "total": len(existing),
                    "full": len(existing) >= _cfg("TRACK_MAX_POINTS")})


@tracks_bp.route("/tracks/<int:track_id>/finish", methods=["POST"])
@active_user_required
def finish_track(track_id: int):
    track = _own_track_or_404(track_id)
    if track.finished_at is not None:
        return jsonify({"ok": True, "track": _track_summary(track)})

    body = request.get_json(silent=True) or {}
    notes = (body.get("notes") or "").strip()[:500] or None

    # Accept any last points the client is still holding.
    trailing = body.get("points")
    if isinstance(trailing, list):
        existing = coverage.decode_points(track.points)
        for raw in trailing[:_cfg("TRACK_MAX_BATCH_POINTS")]:
            fix = _valid_fix(raw)
            if fix and len(existing) < _cfg("TRACK_MAX_POINTS"):
                existing.append(fix)
        track.points = coverage.encode_points(existing)

    track.notes = notes
    track.finished_at = now_utc()
    _finalise(track)
    db.session.commit()

    if track.cell_count == 0:
        return jsonify({
            "ok": True, "published": False, "track": _track_summary(track),
            "message": "That search was too short to map — nothing was published.",
        })
    return jsonify({"ok": True, "published": True, "track": _track_summary(track)})


@tracks_bp.route("/tracks/<int:track_id>/delete", methods=["POST"])
@active_user_required
def delete_track(track_id: int):
    track = db.session.get(SearchTrack, track_id)
    if track is None:
        abort(404)
    if not (current_user.id == track.user_id or current_user.is_moderator):
        abort(403)

    track.is_removed = True
    track.removed_at = now_utc()
    track.removed_by_id = current_user.id
    if current_user.id != track.user_id:
        track.removed_reason = (request.form.get("reason") or "").strip()[:500] or \
            "Removed by a moderator."
    db.session.commit()

    if request.is_json:
        return jsonify({"ok": True})
    flash("Search track removed.", "info")
    return redirect(url_for("pets.pet_detail", pet_id=track.pet_id))


# ── Drone import ───────────────────────────────────────────────────────────

@tracks_bp.route("/pets/<int:pet_id>/tracks/drone", methods=["POST"])
@active_user_required
def import_drone_photos(pet_id: int):
    """Build a flight path from the GPS in a sortie's photos.

    Two ways in, and the difference is where the JPEG is parsed:

    * **JSON** ``{"fixes": [{lat, lng, taken, alt}, …]}`` — the normal path.
      static/petmap/exif.js reads each photo's header on the device and posts
      only the coordinates. A 300-frame sortie is a few kilobytes rather than
      several gigabytes, which is what the 16 MB request cap was rejecting, and
      the imagery never leaves the operator's machine.
    * **multipart** ``photos`` — the fallback, for no-JavaScript and small
      batches. Files are read for EXIF and discarded without being stored, but
      they do have to cross the wire first, so this is what hits the cap.
    """
    pet = _pet_or_404(pet_id)
    wants_json = request.is_json

    def fail(message: str, code: int = 400):
        if wants_json:
            return jsonify({"error": message}), code
        flash(message, "error")
        return redirect(url_for("pets.pet_detail", pet_id=pet.id))

    if wants_json:
        body = request.get_json(silent=True) or {}
        result = exif_track.fixes_from_client(body.get("fixes"))
        notes = (body.get("notes") or "").strip()[:500] or None
    else:
        uploads = [f for f in request.files.getlist("photos") if f and f.filename]
        if not uploads:
            return fail("Choose the photos from the flight — their GPS is what "
                        "builds the path.")
        result = exif_track.fixes_from_uploads(uploads)
        notes = (request.form.get("notes") or "").strip()[:500] or None

    points = exif_track.to_points(result.fixes)
    inside = [p for p in points if within_bounds(p[0], p[1], _cfg("TAS_BOUNDS"))]
    outside = len(points) - len(inside)

    if len(inside) < 2:
        return fail("Couldn't build a path — fewer than two of those photos had "
                    "usable GPS in Tasmania. Check that geotagging was on.")

    stamped = [p[2] for p in inside if p[2]]
    start_dt = datetime.fromtimestamp(min(stamped), timezone.utc) if stamped else now_utc()
    end_dt = datetime.fromtimestamp(max(stamped), timezone.utc) if stamped else now_utc()

    track = SearchTrack(
        pet_id=pet.id, user_id=current_user.id, source=SOURCE_DRONE,
        started_at=start_dt, finished_at=end_dt, notes=notes,
        points=coverage.encode_points(inside),
    )
    _finalise(track, trim=False)      # see _finalise: launch point is search info
    db.session.add(track)
    db.session.commit()

    parts = [f"Flight added from {len(inside)} photo positions "
             f"({track.distance_label}, {track.cell_count} cells covered)."]
    if outside:
        parts.append(f"{outside} were outside Tasmania and ignored.")
    if result.skipped:
        parts.append(f"{len(result.skipped)} had no usable GPS.")
    parts.append("The photos themselves were never uploaded."
                 if wants_json else "The photos themselves were not stored.")
    message = " ".join(parts)

    if wants_json:
        return jsonify({"ok": True, "message": message,
                        "track": _track_summary(track)})
    flash(message, "success")
    return redirect(url_for("pets.pet_detail", pet_id=pet.id))


# ── Reading ────────────────────────────────────────────────────────────────

@tracks_bp.route("/pets/<int:pet_id>/tracks.geojson")
def pet_tracks(pet_id: int):
    """Coverage, and lines for whoever is entitled to them, for one report."""
    pet = _pet_or_404(pet_id)
    cell_m = _cfg("COVERAGE_CELL_M")
    ref_lat = _cfg("COVERAGE_GRID_REF_LAT")

    rows = (SearchTrack.query
            .filter_by(pet_id=pet.id, is_removed=False)
            .filter(SearchTrack.finished_at.isnot(None))
            .order_by(SearchTrack.started_at.asc()).all())

    features, summaries = [], []
    merged: set[tuple[int, int]] = set()

    for track in rows:
        summaries.append(_track_summary(track))
        merged.update(coverage.decode_cells(track.cells))

        if track.may_see_line(current_user):
            line = coverage.decode_points(track.points)
            if len(line) >= 2:
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString",
                                 "coordinates": [[p[1], p[0]] for p in line]},
                    "properties": {**_track_summary(track), "kind": "line"},
                })

    return jsonify({
        "cells": [coverage.cell_bounds(c, cell_m, ref_lat) for c in sorted(merged)],
        "cell_size_m": cell_m,
        "lines": {"type": "FeatureCollection", "features": features},
        "tracks": summaries,
    })


@tracks_bp.route("/api/coverage")
def api_coverage():
    """Searched cells in a viewport, for the main map's coverage layer.

    Cells only — the main map never carries anybody's GPS line, whoever is
    signed in.
    """
    cell_m = _cfg("COVERAGE_CELL_M")
    ref_lat = _cfg("COVERAGE_GRID_REF_LAT")

    q = (SearchTrack.query
         .filter_by(is_removed=False)
         .filter(SearchTrack.finished_at.isnot(None)))

    pet_id = request.args.get("pet_id", type=int)
    if pet_id:
        q = q.filter(SearchTrack.pet_id == pet_id)

    source = request.args.get("source")
    if source in TRACK_SOURCES:
        q = q.filter(SearchTrack.source == source)

    bbox = parse_bbox(request.args.get("bbox"))
    if bbox:
        # Overlap test on the stored extents: keep any track whose box
        # intersects the viewport.
        q = q.filter(SearchTrack.min_lat <= bbox.north,
                     SearchTrack.max_lat >= bbox.south,
                     SearchTrack.min_lng <= bbox.east,
                     SearchTrack.max_lng >= bbox.west)

    merged: set[tuple[int, int]] = set()
    truncated = False
    for track in q.limit(500).all():
        cells = coverage.decode_cells(track.cells)
        if bbox:
            cells = coverage.cells_in_bbox(cells, bbox, cell_m, ref_lat)
        merged.update(cells)
        if len(merged) >= _cfg("COVERAGE_MAX_CELLS"):
            truncated = True
            break

    return jsonify({
        "cells": [coverage.cell_bounds(c, cell_m, ref_lat) for c in sorted(merged)],
        "cell_size_m": cell_m,
        "truncated": truncated,
    })
