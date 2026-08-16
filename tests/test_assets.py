"""Static asset cache-busting.

Assets are served with a one-year cache lifetime, which is only safe because
every URL carries a content hash. If the hashing ever stops working, that
lifetime turns a CSS bug into one frozen in caches for twelve months — so
these tests guard the pair together, not the hashing alone.
"""
import re

import pytest

from app import _ASSET_VERSIONS, _asset_version


@pytest.fixture(autouse=True)
def clear_version_cache():
    _ASSET_VERSIONS.clear()
    yield
    _ASSET_VERSIONS.clear()


def test_every_asset_url_carries_a_version(app, client):
    body = client.get("/").get_data(as_text=True)
    assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', body)

    assert assets, "the page should reference some static assets"
    for url in assets:
        assert "?v=" in url, f"{url} would be cached without a way to bust it"


def test_the_version_is_a_content_hash(app, tmp_path):
    import os
    path = os.path.join(app.static_folder, "petmap/style.css")
    first = _asset_version(app, "petmap/style.css")
    assert first and len(first) == 8

    # Same content, fresh cache -> same version.
    _ASSET_VERSIONS.clear()
    assert _asset_version(app, "petmap/style.css") == first

    # Changed content -> different version.
    original = open(path, "rb").read()
    try:
        with open(path, "ab") as fh:
            fh.write(b"\n/* touched */\n")
        _ASSET_VERSIONS.clear()
        assert _asset_version(app, "petmap/style.css") != first
    finally:
        with open(path, "wb") as fh:
            fh.write(original)


def test_two_assets_get_different_versions(app):
    a = _asset_version(app, "petmap/style.css")
    b = _asset_version(app, "petmap/app.js")
    assert a and b and a != b


def test_a_missing_asset_does_not_break_the_page(app, client):
    """A broken reference should render a dead link, never a 500."""
    assert _asset_version(app, "petmap/does-not-exist.css") is None
    with app.test_request_context():
        from flask import url_for
        url = url_for("static", filename="petmap/does-not-exist.css")
    assert url.endswith("does-not-exist.css"), "no version, but still a URL"


def test_assets_are_served_with_a_long_lifetime(app, client):
    resp = client.get("/static/petmap/style.css")
    assert resp.status_code == 200
    max_age = int(re.search(r"max-age=(\d+)", resp.headers["Cache-Control"]).group(1))
    assert max_age >= 2_592_000, "versioned assets should cache for a long time"
