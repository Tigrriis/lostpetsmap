"""The server side of resuming a search that the phone lost track of.

search_track.js keeps a copy of the running search in localStorage and picks
it up again after a reload. If the finish request succeeded but its reply was
lost, the page comes back believing the search is still running. It then
retries finish, or keeps posting points. What the server says in those two
cases is what decides whether the client can recover on its own.
"""
from conftest import login, make_pet


def start(client, pet):
    resp = client.post(f"/pets/{pet.id}/tracks", json={"source": "on_foot"})
    assert resp.status_code == 201
    return resp.get_json()["track_id"]


# Four fixes ~100 m apart, north to south: 300 m, comfortably above the
# minimum and long enough to survive 50 m trimmed off each end.
WALK = [[-42.8600, 147.3045, 1_700_000_000 + i * 60] for i in range(4)]
for i, p in enumerate(WALK):
    p[0] = -42.8600 - i * 0.0009


def test_finishing_twice_says_the_same_thing_twice(app, client, user):
    pet = make_pet(user)
    login(client)
    tid = start(client, pet)
    client.post(f"/tracks/{tid}/points", json={"points": WALK})

    first = client.post(f"/tracks/{tid}/finish", json={}).get_json()
    assert first["published"] is True

    # The retry after a lost reply. It used to omit `published` entirely, which
    # the client read as "not published" and reported as alert("undefined").
    again = client.post(f"/tracks/{tid}/finish", json={})
    assert again.status_code == 200
    body = again.get_json()
    assert body["ok"] is True
    assert body["published"] is True
    assert body["message"]
    assert body["track"]["id"] == tid


def test_finishing_a_short_search_twice_stays_unpublished(app, client, user):
    pet = make_pet(user)
    login(client)
    tid = start(client, pet)
    # ~30 m: under TRACK_MIN_PUBLISH_M, so nothing is published. (Two fixes
    # 100 m apart would NOT do — the trim caps at a quarter of the path per
    # end, so half of any walk survives, and 50 m of walk is coverage.)
    short = [WALK[0], [WALK[0][0] - 0.00027, WALK[0][1], WALK[0][2] + 60]]
    client.post(f"/tracks/{tid}/points", json={"points": short})

    client.post(f"/tracks/{tid}/finish", json={})
    again = client.post(f"/tracks/{tid}/finish", json={}).get_json()
    assert again["published"] is False
    assert "too short" in again["message"]


def test_points_after_finish_are_refused_with_409(app, client, user):
    """The status the client keys its 'this track is gone' recovery on."""
    pet = make_pet(user)
    login(client)
    tid = start(client, pet)
    client.post(f"/tracks/{tid}/points", json={"points": WALK})
    client.post(f"/tracks/{tid}/finish", json={})

    resp = client.post(f"/tracks/{tid}/points", json={"points": WALK[:1]})
    assert resp.status_code == 409


def test_finish_after_discard_is_404(app, client, user):
    pet = make_pet(user)
    login(client)
    tid = start(client, pet)
    client.post(f"/tracks/{tid}/delete", json={})

    assert client.post(f"/tracks/{tid}/finish", json={}).status_code == 404
    assert client.post(f"/tracks/{tid}/points", json={"points": WALK[:1]}).status_code == 404
