"""Recompute public coordinates after BLUR_RADIUS_M changes.

``Pet.set_location`` only re-rolls the offset when the true point moves, which
is what stops repeated sampling of the public feed from averaging back to the
truth. That also means lowering the configured radius leaves existing reports
blurred at the old, wider one — safe, but inconsistent with what the map now
promises.

Run this once after changing the radius:

    python tools/reblur.py --dry-run
    python tools/reblur.py

One caveat, stated plainly: re-blurring draws a second offset around the same
true point. Anyone who recorded the old public coordinate now has two samples
instead of one. Tightening 250 m to 100 m makes the *new* point more revealing
than the pair, so it costs nothing here — but do not run this repeatedly, and
do not use it to "refresh" offsets. It is a migration, not a routine.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app                                    # noqa: E402
from extensions import db                              # noqa: E402
from models import Pet                                 # noqa: E402
from services.geo import haversine_m                   # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    with app.app_context():
        radius = app.config["BLUR_RADIUS_M"]
        rows = Pet.query.filter_by(blur_location=True, is_removed=False).all()
        print(f"Blur radius is now {radius:g} m. {len(rows)} blurred report(s).")

        changed = 0
        for pet in rows:
            before = haversine_m(pet.lat, pet.lng, pet.public_lat, pet.public_lng)
            if before <= radius:
                print(f"  #{pet.id}: offset {before:.0f} m already within {radius:g} m — left alone.")
                continue

            # Force a fresh draw: set_location is a no-op when nothing moved.
            pet.public_lat = None
            pet.set_location(pet.lat, pet.lng, blur=True, radius_m=radius)
            after = haversine_m(pet.lat, pet.lng, pet.public_lat, pet.public_lng)
            print(f"  #{pet.id}: {before:.0f} m -> {after:.0f} m")
            changed += 1

        if args.dry_run:
            db.session.rollback()
            print(f"Dry run — {changed} report(s) would change. Nothing written.")
        else:
            db.session.commit()
            print(f"Done — {changed} report(s) re-blurred.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
