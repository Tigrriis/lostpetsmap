"""Seed a handful of demo reports for local development.

Invented animals at real Tasmanian street locations, so the map has something
on it while you work on the UI. Never run this against production — every row
it makes is flagged in the description so you can tell them apart.

    python tools/seed_demo.py
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app                                    # noqa: E402
from extensions import db                              # noqa: E402
from models import Pet, User                           # noqa: E402
from services.localtime import now_utc                 # noqa: E402

DEMO_PASSWORD = "demo-password"

USERS = [
    ("demo@example.com", "Demo User", "user"),
    ("mod@example.com", "Moderator", "admin"),
]

REPORTS = [
    dict(report_type="missing", species="cat", name="Raven", breed="Maine Coon",
         colour="Black and white, white chest patch", sex="female", size="large",
         microchipped="yes", collar="No collar",
         description="Very shy, will not come to strangers. Desexed. Last seen "
                     "in the back garden around 10am.",
         locality="New Town", lat=-42.8610617, lng=147.304103, days=2),
    dict(report_type="missing", species="dog", name="Barney", breed="Kelpie cross",
         colour="Tan with black muzzle", sex="male", size="medium",
         microchipped="yes", collar="Red collar, council tag",
         description="Friendly but flighty around traffic. Slipped his lead near "
                     "the oval and headed towards the river.",
         locality="Kingston", lat=-42.9758, lng=147.3083, days=1),
    dict(report_type="found", species="cat", name=None, breed=None,
         colour="Ginger tabby, white socks", sex="unknown", size="small",
         microchipped="unknown", collar="No collar or tag",
         description="Turned up on the back deck two nights running, very thin "
                     "and hungry. Being fed but not taken inside.",
         locality="Glenorchy", lat=-42.8408826, lng=147.2627259, days=3),
    dict(report_type="missing", species="cat", name="Pepper", breed="Domestic short hair",
         colour="Grey tabby", sex="female", size="medium",
         microchipped="no", collar="Purple collar with bell",
         description="Indoor cat, got out through a window. Unlikely to have gone far.",
         locality="Launceston", lat=-41.4391, lng=147.1358, days=5),
    dict(report_type="found", species="dog", name=None, breed="Border collie",
         colour="Black and white", sex="male", size="medium",
         microchipped="unknown", collar="Blue collar, no tag",
         description="Wandering on the highway verge, no traffic sense. Handed to "
                     "the local vet for scanning.",
         locality="Devonport", lat=-41.1789, lng=146.3506, days=1),
]


def main() -> int:
    with app.app_context():
        users = {}
        for email, name, role in USERS:
            user = User.query.filter_by(email=email).first()
            if user is None:
                user = User(email=email, display_name=name, role=role)
                user.set_password(DEMO_PASSWORD)
                db.session.add(user)
            users[email] = user
        db.session.commit()

        author = users["demo@example.com"]
        created = 0
        for spec in REPORTS:
            days = spec.pop("days")
            lat, lng = spec.pop("lat"), spec.pop("lng")
            spec["description"] += "  [demo data]"

            existing = Pet.query.filter_by(user_id=author.id, locality=spec["locality"],
                                           species=spec["species"]).first()
            if existing:
                continue
            pet = Pet(user_id=author.id, last_seen_at=now_utc() - timedelta(days=days), **spec)
            # Found animals are pinned exactly: a street corner where a stray
            # turned up is nobody's home address.
            pet.set_location(lat, lng, blur=(spec["report_type"] == "missing"),
                             radius_m=app.config["BLUR_RADIUS_M"])
            db.session.add(pet)
            created += 1
        db.session.commit()

        print(f"Seeded {created} report(s).")
        print(f"Sign in as demo@example.com or mod@example.com (admin) / {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
