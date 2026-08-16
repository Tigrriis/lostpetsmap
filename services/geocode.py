"""Address geocoding via the Google Maps Geocoding API, with a DB-backed cache.

Unlike the pressure map this came from, geocoding here is a *convenience*, not
the way locations are captured: the map pin is authoritative. A user with no
address, or an app with no API key, loses nothing but the search box. So every
failure path returns a status the caller can render as a hint rather than an
error, and the pin stays wherever the user put it.

Every successful lookup is cached in ``GeocodeCache`` so an address is only ever
sent to Google once.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

import requests
from flask import current_app

from extensions import db
from models import GeocodeCache

# status values
OK = "ok"
NOT_FOUND = "not_found"
NO_KEY = "no_key"          # GOOGLE_MAPS_API_KEY not configured
DENIED = "denied"          # REQUEST_DENIED — bad key / API not enabled / no billing
OVER_LIMIT = "over_limit"  # OVER_QUERY_LIMIT — quota / rate limit
ERROR = "error"            # network / unexpected

MESSAGES = {
    NOT_FOUND: "No match for that address — drop the pin on the map instead.",
    NO_KEY: "Address search isn't configured on this server. Drop the pin on the map.",
    DENIED: "Address search is unavailable right now. Drop the pin on the map.",
    OVER_LIMIT: "Address search is busy. Try again shortly, or drop the pin on the map.",
    ERROR: "Address search failed. Drop the pin on the map.",
}


class GeocodeResult(NamedTuple):
    coords: Optional[tuple[float, float]]
    status: str
    formatted: Optional[str] = None
    locality: Optional[str] = None


def _normalize(address: str) -> str:
    return " ".join(address.strip().lower().split())


def _locality_from(result: dict) -> Optional[str]:
    """Pull the suburb/town out of a Google result's address components."""
    for comp in result.get("address_components", []):
        types = comp.get("types", [])
        if "locality" in types or "sublocality" in types:
            return (comp.get("long_name") or "").upper() or None
    return None


def lookup_cached(address: str) -> Optional[GeocodeResult]:
    """Answer from the cache alone, or None if this would need a Google call.

    Lets a caller charge its rate limit only for lookups that actually cost
    money. Re-checking an address someone already resolved should never eat
    into anyone's budget.
    """
    key = _normalize(address or "")
    if not key:
        return None
    row = db.session.query(GeocodeCache).filter_by(normalized_address=key).first()
    if row is None:
        return None
    return GeocodeResult((row.lat, row.lng), OK, row.formatted)


def geocode_detailed(address: str) -> GeocodeResult:
    """Geocode ``address``, returning coordinates and a status code.

    Biased to Tasmania via the configured suffix and ``components=country:AU``,
    so "Elizabeth St" resolves in Hobart rather than in Sydney or London.
    """
    address = (address or "").strip()
    if not address:
        return GeocodeResult(None, NOT_FOUND)

    key = _normalize(address)
    cached = db.session.query(GeocodeCache).filter_by(normalized_address=key).first()
    if cached:
        return GeocodeResult((cached.lat, cached.lng), OK, cached.formatted)

    api_key = current_app.config.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return GeocodeResult(None, NO_KEY)

    try:
        resp = requests.get(
            current_app.config["GOOGLE_GEOCODE_URL"],
            params={
                "address": address + current_app.config["GEOCODE_SUFFIX"],
                "key": api_key,
                "region": "au",
                "components": "country:AU",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        current_app.logger.warning("Geocode request failed: %s", exc)
        return GeocodeResult(None, ERROR)

    # Check the error statuses before the empty-results check: a denied or
    # rate-limited response also has no results, but must report its own cause.
    status = data.get("status")
    if status == "REQUEST_DENIED":
        current_app.logger.error("Google geocode REQUEST_DENIED: %s", data.get("error_message"))
        return GeocodeResult(None, DENIED)
    if status == "OVER_QUERY_LIMIT":
        current_app.logger.error("Google geocode OVER_QUERY_LIMIT")
        return GeocodeResult(None, OVER_LIMIT)
    if status == "ZERO_RESULTS" or not data.get("results"):
        return GeocodeResult(None, NOT_FOUND)
    if status != "OK":
        current_app.logger.warning("Google geocode status=%s", status)
        return GeocodeResult(None, ERROR)

    try:
        result = data["results"][0]
        loc = result["geometry"]["location"]
        lat, lng = float(loc["lat"]), float(loc["lng"])
    except (KeyError, ValueError, IndexError):
        return GeocodeResult(None, NOT_FOUND)

    formatted = result.get("formatted_address")
    locality = _locality_from(result)

    # A low-precision (APPROXIMATE) match is still useful here, unlike on the
    # pressure map: "somewhere in Sorell" is a perfectly good starting point for
    # a pin the user is about to drag anyway. It is accepted, and the UI tells
    # them to check it.
    db.session.add(GeocodeCache(normalized_address=key, lat=lat, lng=lng, formatted=formatted))
    db.session.commit()
    return GeocodeResult((lat, lng), OK, formatted, locality)


def geocode(address: str) -> Optional[tuple[float, float]]:
    """Return (lat, lng) for a free-text address, or ``None`` if not resolved."""
    return geocode_detailed(address).coords
