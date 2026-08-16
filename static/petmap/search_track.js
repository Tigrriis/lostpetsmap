/* Live search logging: record where you walk, publish it as coverage.
 *
 * The hard constraint this is built around: **a browser cannot read GPS in the
 * background.** iOS Safari suspends watchPosition the moment the tab is
 * backgrounded or the screen locks, and Android is only a little better. So:
 *
 *   - A Wake Lock keeps the screen on for as long as the search runs.
 *   - Every fix is written to localStorage the instant it arrives, and flushed
 *     to the server in batches. A suspension, a crash, or a closed tab can
 *     therefore only ever lose the last few seconds — never the whole walk.
 *   - Coming back to the page resumes the same track rather than starting a
 *     second one.
 *
 * Anything promising true background tracking would need a native wrapper; the
 * UI says "keep this screen open" because that is the truth.
 */
(function () {
  "use strict";

  var CFG = window.PETMAP_DETAIL;
  if (!CFG || !CFG.canLogSearch) return;

  var U = window.PetMapUtil;

  var FLUSH_MS = 30000;         // batch uploads; see the note above
  var MIN_MOVE_M = 5;           // ignore GPS jitter while standing still
  var MAX_ACCURACY_M = 50;      // discard wildly imprecise fixes
  var STORE_KEY = "petmap-track-" + CFG.petId;
  var TRIM_M = CFG.trimM || 50; // quoted in the safety prompt; server decides it

  var state = null;             // {trackId, startedAt, buffer[], sent, distance}
  var watchId = null;
  var wakeLock = null;
  var flushTimer = null;
  var tickTimer = null;
  var lastFix = null;
  var liveLine = null;

  var el = {
    idle: document.getElementById("search-idle"),
    live: document.getElementById("search-live"),
    start: document.getElementById("start-search"),
    finish: document.getElementById("finish-search"),
    abandon: document.getElementById("abandon-search"),
    source: document.getElementById("track-source"),
    notes: document.getElementById("track-notes"),
    time: document.getElementById("live-time"),
    distance: document.getElementById("live-distance"),
    points: document.getElementById("live-points"),
    status: document.getElementById("live-status")
  };
  if (!el.start) return;

  // ---------- Persistence ----------

  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* full or private mode */ }
  }
  function load() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (e) { return null; }
  }
  function clearStored() {
    try { localStorage.removeItem(STORE_KEY); } catch (e) { /* nothing to do */ }
  }

  // ---------- Geometry ----------

  function metresBetween(a, b) {
    var R = 6371000, p1 = a[0] * Math.PI / 180, p2 = b[0] * Math.PI / 180;
    var dp = p2 - p1, dl = (b[1] - a[1]) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  // ---------- Server ----------

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": U.csrfToken() },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        return data;
      });
    });
  }

  function flush() {
    if (!state || !state.buffer.length) return Promise.resolve();
    var batch = state.buffer.slice(0, 500);
    return post("/tracks/" + state.trackId + "/points", { points: batch })
      .then(function (data) {
        // Only drop what the server confirmed, so a failed request retries
        // the same points instead of losing them.
        state.buffer = state.buffer.slice(batch.length);
        state.sent = data.total;
        save();
        if (data.full) status("Reached the maximum length for one search — finish up.");
      })
      .catch(function () {
        status("Offline — still recording, will upload when you're back.", true);
      });
  }

  // ---------- UI ----------

  function status(text, isError) {
    el.status.textContent = text;
    el.status.classList.toggle("is-error", !!isError);
  }

  function tick() {
    if (!state) return;
    var secs = Math.floor((Date.now() - state.startedAt) / 1000);
    var mins = Math.floor(secs / 60);
    el.time.textContent = mins + ":" + String(secs % 60).padStart(2, "0");
    el.distance.textContent = Math.round(state.distance);
    el.points.textContent = state.sent + state.buffer.length;
  }

  function showLive(on) {
    el.idle.hidden = on;
    el.live.hidden = !on;
  }

  // ---------- Wake lock ----------

  function acquireWakeLock() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen").then(function (lock) {
      wakeLock = lock;
      lock.addEventListener("release", function () { wakeLock = null; });
    }).catch(function () { /* denied, or battery saver — not fatal */ });
  }

  function releaseWakeLock() {
    if (wakeLock) { wakeLock.release().catch(function () {}); wakeLock = null; }
  }

  // Re-acquire on return: the lock is dropped automatically when the page is
  // hidden, and is not restored by itself.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && state) acquireWakeLock();
  });

  // ---------- Recording ----------

  function onFix(pos) {
    if (!state) return;
    var c = pos.coords;
    if (c.accuracy && c.accuracy > MAX_ACCURACY_M) {
      status("Weak GPS signal (±" + Math.round(c.accuracy) + " m) — waiting for better.");
      return;
    }
    var point = [c.latitude, c.longitude];
    if (lastFix) {
      var moved = metresBetween(lastFix, point);
      if (moved < MIN_MOVE_M) return;          // standing still; don't log jitter
      state.distance += moved;
    }
    lastFix = point;

    state.buffer.push([
      Number(c.latitude.toFixed(6)),
      Number(c.longitude.toFixed(6)),
      Math.floor(Date.now() / 1000)
    ]);
    save();
    tick();
    status("Recording. Keep this screen open.");

    if (liveLine) liveLine.addLatLng(point);
    else if (window.PM_detailMap) {
      liveLine = L.polyline([point], { color: "#0a9ec2", weight: 4, opacity: 0.8,
                                       dashArray: "6 4" }).addTo(window.PM_detailMap);
    }
  }

  function onFixError(err) {
    if (err.code === err.PERMISSION_DENIED) {
      status("Location permission denied — nothing is being recorded.", true);
    } else {
      status("Can't get a GPS fix right now. Still trying.", true);
    }
  }

  function beginWatching() {
    watchId = navigator.geolocation.watchPosition(onFix, onFixError, {
      enableHighAccuracy: true, maximumAge: 5000, timeout: 20000
    });
    flushTimer = setInterval(flush, FLUSH_MS);
    tickTimer = setInterval(tick, 1000);
    acquireWakeLock();
  }

  function stopWatching() {
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    clearInterval(flushTimer); clearInterval(tickTimer);
    releaseWakeLock();
  }

  // ---------- Actions ----------

  // Acknowledged once per device, not per search. The banner above the button
  // is visible every time regardless; this exists so the first recording on a
  // phone cannot be started by someone who scrolled straight past it. Nagging
  // on every search would train people to dismiss it without reading.
  var ACK_KEY = "petmap-track-safety-ack";

  function safetyAcknowledged() {
    try {
      if (localStorage.getItem(ACK_KEY) === "1") return true;
    } catch (e) { /* private mode: ask every time, which is the safe default */ }

    // Same wording as the banner above the button, so the two cannot drift.
    var ok = window.confirm(
      "Don't start recording at your home.\n\n" +
      "Your track becomes part of a public coverage map. Walk or drive to " +
      "the search area first, then press start. Press stop before you head " +
      "back. Only about " + TRIM_M + " m is trimmed from each end.\n\n" +
      "Start recording now?");
    if (ok) {
      try { localStorage.setItem(ACK_KEY, "1"); } catch (e) { /* fine */ }
    }
    return ok;
  }

  el.start.addEventListener("click", function () {
    if (!navigator.geolocation) {
      window.alert("This browser can't share a location, so a search can't be recorded.");
      return;
    }
    if (!safetyAcknowledged()) return;
    el.start.disabled = true;
    post(CFG.urls.start, { source: el.source ? el.source.value : "on_foot" })
      .then(function (data) {
        state = { trackId: data.track_id, startedAt: Date.now(),
                  buffer: [], sent: 0, distance: 0 };
        save();
        showLive(true);
        status("Waiting for GPS…");
        beginWatching();
      })
      .catch(function (err) { window.alert(err.message); })
      .then(function () { el.start.disabled = false; });
  });

  el.finish.addEventListener("click", function () {
    if (!state) return;
    el.finish.disabled = true;
    stopWatching();
    var body = { notes: el.notes ? el.notes.value : "", points: state.buffer };
    post("/tracks/" + state.trackId + "/finish", body)
      .then(function (data) {
        clearStored();
        state = null; lastFix = null;
        if (liveLine && window.PM_detailMap) {
          window.PM_detailMap.removeLayer(liveLine); liveLine = null;
        }
        showLive(false);
        window.location.reload();          // simplest way to redraw coverage
        if (!data.published) window.alert(data.message);
      })
      .catch(function (err) {
        el.finish.disabled = false;
        beginWatching();                    // keep going rather than lose it
        status("Couldn't save: " + err.message + ". Still recording.", true);
      });
  });

  el.abandon.addEventListener("click", function () {
    if (!state) return;
    if (!window.confirm("Discard this search? Nothing will be saved.")) return;
    stopWatching();
    post("/tracks/" + state.trackId + "/delete", {}).catch(function () {});
    clearStored();
    state = null; lastFix = null;
    if (liveLine && window.PM_detailMap) {
      window.PM_detailMap.removeLayer(liveLine); liveLine = null;
    }
    showLive(false);
  });

  // ---------- Resume ----------
  // A reload mid-search (or the tab being evicted) must not orphan the track.

  var stored = load();
  if (stored && stored.trackId) {
    state = stored;
    showLive(true);
    status("Resumed the search you had running.");
    tick();
    beginWatching();
    flush();
  }

  // Best-effort flush on the way out — sendBeacon outlives the page, which a
  // normal fetch would not. It must be FormData, not JSON: a beacon cannot set
  // the X-CSRFToken header, and a plain form field is the one place Flask-WTF
  // will look without one. Failure here is survivable either way, because the
  // localStorage copy is resumed next visit.
  window.addEventListener("pagehide", function () {
    if (!state || !state.buffer.length || !navigator.sendBeacon) return;
    try {
      var payload = new FormData();
      payload.append("csrf_token", U.csrfToken());
      payload.append("points", JSON.stringify(state.buffer));
      navigator.sendBeacon("/tracks/" + state.trackId + "/points", payload);
    } catch (e) { /* the localStorage copy survives regardless */ }
  });
})();
