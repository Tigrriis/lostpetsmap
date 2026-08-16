# PetMap Tasmania

A public map of lost and found pets across Tasmania. Anyone can browse; an
account is needed to post a report, log a sighting, or message a reporter.

This is a ground-up rewrite of the QGIS-plugin era `PetGIS V1`, which scraped
Facebook group posts into CSVs and hand-maintained shapefiles per status. Here
the data is user-submitted, lives in Postgres, and the map is the front door.

The Flask/Postgres/Render/auth scaffolding and the Arete HUD stylesheet are
carried over from the `as3500design` suite — in particular its crowd-sourced
pressure map, which is structurally the same problem (address → point → photo →
moderation → Leaflet).

---

## Running it locally

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt
```

```bash
.venv/Scripts/python bootstrap_db.py && .venv/Scripts/python app.py
```

That gives you SQLite at `instance/petmap.db`, no geocoding, and emails printed
to the log. Nothing else is required — see `.env.example` for the optional bits.

Some demo reports to look at:

```bash
.venv/Scripts/python tools/seed_demo.py
```

Tests:

```bash
.venv/Scripts/python -m pytest -q
```

---

## How it fits together

| File | Does |
| --- | --- |
| `app.py` | Wiring, template filters, error handlers, the map page. `gunicorn app:app`. |
| `models.py` | `User`, `Pet`, `PetPhoto`, `Sighting`, `ContactMessage`, `GeocodeCache`. |
| `pets.py` | The map feed, report CRUD, photos, sightings, contact relay. |
| `auth.py` | Register/login/logout, signed password-reset tokens, role guards. |
| `moderation.py` | Review queue, soft-delete and restore, account suspension. |
| `services/geo.py` | Bounding boxes, haversine, Tasmania's extent. |
| `services/geocode.py` | Google Geocoding + DB cache. Entirely optional. |
| `services/images.py` | Resize to ≤1280 px JPEG, build a 240 px square thumbnail. |
| `services/localtime.py` | The UTC ⇄ Australia/Hobart boundary. |
| `services/localities.py` | 239 Tasmanian place names, lifted from the V1 dataset. |

### Three decisions worth knowing before you change anything

**Locations are stored twice.** `Pet.lat/lng` is where the reporter actually
pinned. `Pet.public_lat/public_lng` is what the world sees — for a missing pet
that is a random point within 250 m, because "last seen" is nearly always the
owner's own address. Anything public must serve the public pair;
`pets._may_see_exact` is the single place that decides otherwise. The random
offset is generated once and stored, never re-rolled on save: repeated sampling
of a re-rolled offset averages back to the true point.

**Removal is a flag, never a DELETE.** Moderation has to be reversible, so every
query behind a public surface filters `is_removed == False`. Forget the filter
and you un-remove the row.

**The map pin is authoritative, the address is a label.** Geocoding is a
convenience button that moves the pin. A pet last seen "on the track behind the
oval" has no street address, and the app must not require one — so no failure of
the geocoder ever blocks a report, and the whole thing runs fine with no Google
key at all.

### Contact details

No email address is ever rendered. Messages are relayed through Resend with the
sender's address in `Reply-To`, and the body is logged to `ContactMessage` so an
abuse report can be investigated. A phone number appears only if the reporter
ticks the box.

---

## Deploying to Render

`render.yaml` is a blueprint: a free Postgres instance plus a web service.
Point Render at the repo, sync the blueprint, then set the secrets in the
dashboard (they are all marked `sync: false`, so nothing secret is in the repo):

| Variable | Needed? | Without it |
| --- | --- | --- |
| `SECRET_KEY` | auto-generated | — |
| `DATABASE_URL` | from the blueprint | — |
| `GOOGLE_MAPS_API_KEY` | optional | The address box says so; the pin still works. |
| `RESEND_API_KEY` | **yes, in practice** | Password resets and sighting alerts go to the log, not the user. |
| `MAIL_FROM` | with Resend | Must be on a domain verified in Resend. |

`MAIL_FROM` on the default `resend.dev` sender only delivers to the Resend
account owner, which is fine for a smoke test and useless in production.

Migrations run from the start command, and again lazily on the first request —
belt and braces, because a `render.yaml` start-command change only lands on a
manual blueprint sync while code deploys land on every push.

### Making yourself an admin

Roles are not self-service. After registering, promote yourself once from a
Render shell:

```bash
python -c "from app import app; from extensions import db; from models import User; app.app_context().push(); u=User.query.filter_by(email='you@example.com').one(); u.role='admin'; db.session.commit(); print(u.email, u.role)"
```

From then on `/moderate` lets an admin set anyone else's role.

---

## Known gaps

Things deliberately left out of this first cut, in rough priority order:

- **No sightings layer on the main map.** Sightings show on a report's own page
  only. Putting them on the main map is genuinely useful and is the first thing
  I would add.
- **No matching between found and missing reports.** "This found tabby looks
  like that missing tabby 2 km away" is the obvious next feature, and the data
  model already supports it (species + location + date window).
- **No email verification on signup.** An account can be created with an address
  the person does not own. Rate limits and moderation cover the spam case, but
  this should be added before any real promotion of the site.
- **Rate limiting is per-user row counts**, not per-IP, so it does nothing about
  someone registering accounts in bulk.
- **No pagination on the moderation queue** — it shows the most recent 50 of
  each kind.
- Photos live in the database. That is what keeps deployment to one Postgres
  and no object storage, but it will want revisiting well before the free tier's
  1 GB.
