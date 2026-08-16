"""Moderation: review reports and sightings, restore removals, manage accounts.

A public, user-submitted map needs a way to deal with spam, scams ("pay me and
I'll return your dog") and misplaced pins. Everything here is reversible —
removal is a flag, not a DELETE — so a moderator's mistake costs a click rather
than someone's only photo of their cat.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from auth import admin_required, moderator_required
from extensions import db
from models import (
    ROLE_ADMIN, ROLE_MODERATOR, ROLE_USER, ContactMessage, Pet, Sighting, User,
)
from services.localtime import now_utc

moderation_bp = Blueprint("moderation", __name__, url_prefix="/moderate")

PAGE_SIZE = 50


@moderation_bp.route("")
@moderator_required
def queue():
    view = request.args.get("view", "reports")

    pets = sightings = messages = users = None
    if view == "removed":
        pets = (Pet.query.filter_by(is_removed=True)
                .order_by(Pet.removed_at.desc()).limit(PAGE_SIZE).all())
    elif view == "sightings":
        sightings = (Sighting.query.order_by(Sighting.created_at.desc())
                     .limit(PAGE_SIZE).all())
    elif view == "messages":
        messages = (ContactMessage.query.order_by(ContactMessage.created_at.desc())
                    .limit(PAGE_SIZE).all())
    elif view == "users":
        users = User.query.order_by(User.created_at.desc()).limit(PAGE_SIZE).all()
    else:
        view = "reports"
        pets = (Pet.query.filter_by(is_removed=False)
                .order_by(Pet.created_at.desc()).limit(PAGE_SIZE).all())

    return render_template("moderate.html", view=view, pets=pets, sightings=sightings,
                           messages=messages, users=users)


# ── Reports ────────────────────────────────────────────────────────────────

@moderation_bp.route("/pet/<int:pet_id>/remove", methods=["POST"])
@moderator_required
def remove_pet(pet_id: int):
    pet = db.session.get(Pet, pet_id) or abort(404)
    pet.is_removed = True
    pet.removed_at = now_utc()
    pet.removed_by_id = current_user.id
    pet.removed_reason = (request.form.get("reason") or "").strip()[:500] or "Removed by a moderator."
    db.session.commit()
    flash(f"Report #{pet.id} removed.", "info")
    return redirect(request.referrer or url_for("moderation.queue"))


@moderation_bp.route("/pet/<int:pet_id>/restore", methods=["POST"])
@moderator_required
def restore_pet(pet_id: int):
    pet = db.session.get(Pet, pet_id) or abort(404)
    pet.is_removed = False
    pet.removed_at = None
    pet.removed_by_id = None
    pet.removed_reason = None
    db.session.commit()
    flash(f"Report #{pet.id} restored.", "success")
    return redirect(request.referrer or url_for("moderation.queue", view="removed"))


@moderation_bp.route("/sighting/<int:sighting_id>/remove", methods=["POST"])
@moderator_required
def remove_sighting(sighting_id: int):
    sighting = db.session.get(Sighting, sighting_id) or abort(404)
    sighting.is_removed = True
    sighting.removed_at = now_utc()
    sighting.removed_by_id = current_user.id
    sighting.removed_reason = (request.form.get("reason") or "").strip()[:500] or None
    db.session.commit()
    flash("Sighting removed.", "info")
    return redirect(request.referrer or url_for("moderation.queue", view="sightings"))


@moderation_bp.route("/sighting/<int:sighting_id>/restore", methods=["POST"])
@moderator_required
def restore_sighting(sighting_id: int):
    sighting = db.session.get(Sighting, sighting_id) or abort(404)
    sighting.is_removed = False
    sighting.removed_at = None
    sighting.removed_by_id = None
    db.session.commit()
    flash("Sighting restored.", "success")
    return redirect(request.referrer or url_for("moderation.queue", view="sightings"))


# ── Accounts ───────────────────────────────────────────────────────────────

def _target(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    # A moderator cannot act on an admin, and nobody can act on themselves —
    # the second rule is what stops an admin from locking themselves out.
    if user.is_admin and not current_user.is_admin:
        abort(403)
    if user.id == current_user.id:
        abort(400)
    return user


@moderation_bp.route("/user/<int:user_id>/ban", methods=["POST"])
@moderator_required
def ban_user(user_id: int):
    user = _target(user_id)
    user.is_banned = True
    db.session.commit()
    flash(f"{user.email} suspended.", "info")
    return redirect(request.referrer or url_for("moderation.queue", view="users"))


@moderation_bp.route("/user/<int:user_id>/unban", methods=["POST"])
@moderator_required
def unban_user(user_id: int):
    user = _target(user_id)
    user.is_banned = False
    db.session.commit()
    flash(f"{user.email} reinstated.", "success")
    return redirect(request.referrer or url_for("moderation.queue", view="users"))


@moderation_bp.route("/user/<int:user_id>/remove_reports", methods=["POST"])
@moderator_required
def remove_user_reports(user_id: int):
    """Bulk-remove everything one account posted — the spam-cleanup button."""
    user = _target(user_id)
    reason = (request.form.get("reason") or "").strip()[:500] or "Bulk removal by a moderator."
    count = 0
    for pet in Pet.query.filter_by(user_id=user.id, is_removed=False).all():
        pet.is_removed = True
        pet.removed_at = now_utc()
        pet.removed_by_id = current_user.id
        pet.removed_reason = reason
        count += 1
    db.session.commit()
    flash(f"Removed {count} report(s) from {user.email}.", "info")
    return redirect(request.referrer or url_for("moderation.queue", view="users"))


@moderation_bp.route("/user/<int:user_id>/role", methods=["POST"])
@admin_required
def set_role(user_id: int):
    user = _target(user_id)
    role = (request.form.get("role") or "").lower()
    if role not in (ROLE_USER, ROLE_MODERATOR, ROLE_ADMIN):
        abort(400)
    user.role = role
    db.session.commit()
    flash(f"{user.email} is now {role}.", "success")
    return redirect(request.referrer or url_for("moderation.queue", view="users"))
