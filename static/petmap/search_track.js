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

  // Accuracy gates, in metres, against the radius the phone reports per fix.
  // A phone's first fixes come from Wi-Fi and cell towers and can be hundreds
  // of metres out, so the line waits for a real lock before it starts...
  var LOCK_M = 30;
  var LOCK_WAIT_MS = 60000;     // ...but after a minute, starts with anything usable
  // 50 m was too strict: an 11-minute walk under cover came back with two
  // usable fixes, which was enough to measure a distance and not enough to
  // draw. A consumer phone routinely reports 30-80 m in a suburb. The
  // smoothing below gives those rough fixes little weight rather than none.
  var MAX_ACCURACY_M = 120;     // discard only wildly imprecise fixes
  // With "Precise Location" off the browser only gets a position fuzzed to a
  // few kilometres and updated rarely. Nothing drawable comes of that, and the
  // fix is a phone setting, so say which one.
  var APPROX_M = 1000;
  var STALL_S = 20;             // silence this long means GPS has stopped; say so

  // Log a point once the smoothed position has moved past its own
  // uncertainty: at least MIN_MOVE_M (standing-still jitter), at most
  // MAX_STEP_M (so a rough patch still draws).
  var MIN_MOVE_M = 5;
  var MAX_STEP_M = 20;

  // How fast the searcher plausibly moves, in m/s. `noise` is how quickly the
  // smoothing lets the estimate drift between fixes; `max` rejects a fix that
  // would need more than that to reach (typically a Wi-Fi fix hundreds of
  // metres out that still claims ±20 m). Three rejections in a row means the
  // estimate was the thing that was wrong, so it restarts from the fixes.
  var MOTION = { on_foot: { noise: 3, max: 8 }, vehicle: { noise: 15, max: 45 } };
  var JUMPS_BEFORE_RESET = 3;
  var STORE_KEY = "petmap-track-" + CFG.petId;
  var TRIM_M = CFG.trimM || 50; // quoted in the safety prompt; server decides it

  var state = null;             // {trackId, startedAt, source, buffer[], sent, distance, last}
  var watchId = null;
  var wakeLock = null;
  var flushTimer = null;
  var tickTimer = null;
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
    accuracy: document.getElementById("live-accuracy"),
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

  // ---------- Smoothing ----------
  // A minimal Kalman filter over lat/lng, weighted by each fix's reported
  // accuracy: a ±5 m fix moves the estimate nearly all the way, a ±100 m
  // fix barely nudges it. Its uncertainty grows between fixes at the
  // searcher's plausible speed, so after a gap the next fix counts for more.
  // Not persisted: a resumed search waits for a fresh lock rather than trust
  // an estimate from before the page was suspended.

  var est = null;               // {lat, lng, variance (m^2), t (ms)}

  function smooth(point, acc, t, noise) {
    if (!est) {
      est = { lat: point[0], lng: point[1], variance: acc * acc, t: t };
      return est;
    }
    est.variance += Math.max(0, t - est.t) / 1000 * noise * noise;
    est.t = t;
    var k = est.variance / (est.variance + acc * acc);
    est.lat += k * (point[0] - est.lat);
    est.lng += k * (point[1] - est.lng);
    est.variance *= 1 - k;
    return est;
  }

  function implausibleJump(point, acc, t, maxSpeed) {
    if (!est) return false;
    var secs = Math.max(1, (t - est.t) / 1000);
    return metresBetween([est.lat, est.lng], point) >
           maxSpeed * secs + acc + Math.sqrt(est.variance);
  }

  // ---------- Server ----------

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": U.csrfToken() },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) {
          var err = new Error(data.error || ("HTTP " + r.status));
          err.status = r.status;          // callers tell "gone" from "offline"
          throw err;
        }
        return data;
      });
    });
  }

  // The server no longer has a live track under this id — it was finished
  // (409) or discarded (404), usually from another tab or a lost response.
  // Recording into it would go nowhere, so stop and forget the local copy.
  function trackGone(err) {
    return err && (err.status === 409 || err.status === 404);
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
      .catch(function (err) {
        if (trackGone(err)) {
          // This used to read as "Offline — still recording" forever: the
          // stored track had already been finished, every flush hit 409, and
          // the only way out was Discard — which deleted the published search.
          resetLocal();
          window.alert("That search was already finished, so this page has stopped " +
                       "recording. Reload to see it on the map.");
          return;
        }
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
    // A browser gives no event when GPS stops (screen locked, tab in the
    // background, phone in a pocket), so silence is the only signal.
    // lastFixAt is 0 after a permission error, whose own message must stay.
    var silent = lastFixAt ? Math.floor((Date.now() - lastFixAt) / 1000) : 0;
    if (silent >= STALL_S) {
      status("No GPS update for " + silent + " s. Keep this screen on and this " +
             "page in front, or nothing is recorded.", true);
    }
  }

  function showLive(on) {
    el.idle.hidden = on;
    el.live.hidden = !on;
  }

  // Forget the search on this device: stop the GPS, drop the stored copy,
  // clear the live line. The server-side track is untouched.
  function resetLocal() {
    stopWatching();
    clearStored();
    state = null;
    forgetGps();
    if (liveLine && window.PM_detailMap) {
      window.PM_detailMap.removeLayer(liveLine); liveLine = null;
    }
    showLive(false);
    // Re-arm the controls. Finish is disabled while its request is in flight,
    // and the paths that land here without a reload would otherwise leave it
    // dead for the next search started on this page.
    el.finish.disabled = false;
    el.start.disabled = false;
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

  var discarded = 0;
  var jumps = 0;
  var lastFixAt = 0;            // any fix, used or not; drives the stall warning
  var waitingSince = null;      // when the wait for a first lock began
  var bestWaiting = Infinity;   // best accuracy seen during that wait

  function forgetGps() {
    est = null;
    if (el.accuracy) el.accuracy.textContent = "–";
    discarded = 0; jumps = 0;
    waitingSince = null; bestWaiting = Infinity;
  }

  function motion() {
    return MOTION[state.source] || MOTION.on_foot;
  }

  // Still waiting for a lock: report what the phone is managing, and whether
  // it is a signal problem (move into the open) or a settings one.
  function waitingStatus(acc, waited) {
    if (bestWaiting >= APPROX_M && waited >= 15000) {
      status("Your phone is only sharing an approximate location (±" +
             Math.round(bestWaiting) + " m), which can't draw a search. Turn on " +
             "Precise Location for this browser. iPhone: Settings → Privacy & " +
             "Security → Location Services → Safari Websites (or Chrome). " +
             "Android: Chrome → Settings → Site settings → Location.", true);
    } else if (waited >= LOCK_WAIT_MS) {
      status("Still no usable GPS fix (best ±" + Math.round(bestWaiting) +
             " m). Move away from buildings and heavy tree cover.", true);
    } else {
      status("Waiting for a GPS lock: ±" + Math.round(acc) + " m now, want ±" +
             LOCK_M + " m. Open sky helps.");
    }
  }

  function onFix(pos) {
    if (!state) return;
    var c = pos.coords;
    var now = Date.now();
    var acc = Math.max(c.accuracy || MAX_ACCURACY_M, 1);
    var point = [c.latitude, c.longitude];
    lastFixAt = now;
    if (el.accuracy) el.accuracy.textContent = Math.round(acc);

    if (!est) {
      if (waitingSince === null) waitingSince = now;
      bestWaiting = Math.min(bestWaiting, acc);
      var waited = now - waitingSince;
      if (acc > LOCK_M && !(waited >= LOCK_WAIT_MS && acc <= MAX_ACCURACY_M)) {
        waitingStatus(acc, waited);
        return;
      }
      waitingSince = null; bestWaiting = Infinity;
    } else if (acc > MAX_ACCURACY_M) {
      // Hold the line, but say how many are being dropped, because a silent
      // counter that never moves looks identical to a working recording.
      discarded++;
      status("Weak GPS (±" + Math.round(acc) + " m): " + discarded +
             " fix" + (discarded === 1 ? "" : "es") + " skipped so far. Still trying.");
      return;
    } else if (implausibleJump(point, acc, now, motion().max)) {
      if (++jumps < JUMPS_BEFORE_RESET) {
        status("Ignored a GPS jump that would mean moving impossibly fast.");
        return;
      }
      est = null;               // the fixes agree with each other, not with us
    }
    jumps = 0;
    status("Recording. Keep this screen open.");

    var e = smooth(point, acc, now, motion().noise);
    var here = [e.lat, e.lng];
    if (state.last) {
      var moved = metresBetween(state.last, here);
      var step = Math.min(Math.max(MIN_MOVE_M, Math.sqrt(e.variance)), MAX_STEP_M);
      if (moved < step) return;             // not clearly moved yet
      state.distance += moved;
    }
    state.last = here;

    state.buffer.push([
      Number(here[0].toFixed(6)),
      Number(here[1].toFixed(6)),
      Math.floor(now / 1000)
    ]);
    save();
    tick();

    if (liveLine) liveLine.addLatLng(here);
    else if (window.PM_detailMap) {
      liveLine = L.polyline([here], { color: U.colours.live(), weight: 4, opacity: 0.8,
                                      dashArray: "6 4" }).addTo(window.PM_detailMap);
    }
  }

  function onFixError(err) {
    if (err.code === err.PERMISSION_DENIED) {
      lastFixAt = 0;            // keep this message; the stall warning would bury it
      status("Location permission denied — nothing is being recorded.", true);
    } else {
      status("Can't get a GPS fix right now. Still trying.", true);
    }
  }

  function beginWatching() {
    // maximumAge 0: a fresh reading every time, never a cached position.
    watchId = navigator.geolocation.watchPosition(onFix, onFixError, {
      enableHighAccuracy: true, maximumAge: 0, timeout: 20000
    });
    lastFixAt = Date.now();     // a phone that never answers also reads as stalled
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
    var source = el.source ? el.source.value : "on_foot";
    post(CFG.urls.start, { source: source })
      .then(function (data) {
        state = { trackId: data.track_id, startedAt: Date.now(), source: source,
                  buffer: [], sent: 0, distance: 0, last: null };
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
        resetLocal();
        // Tell them *before* reloading. The other order looks harmless and is
        // not: the alert is discarded as the page unloads, so a search that
        // published nothing did so silently.
        if (!data.published) window.alert(data.message || "That search wasn't published.");
        window.location.reload();          // simplest way to redraw coverage
      })
      .catch(function (err) {
        if (trackGone(err)) {
          resetLocal();
          window.alert("That search no longer exists on the server, so there is " +
                       "nothing to publish.");
          return;
        }
        el.finish.disabled = false;
        beginWatching();                    // keep going rather than lose it
        status("Couldn't save: " + err.message + ". Still recording.", true);
      });
  });

  el.abandon.addEventListener("click", function () {
    if (!state) return;
    if (!window.confirm("Discard this search? Nothing will be saved.")) return;
    var id = state.trackId;
    resetLocal();
    post("/tracks/" + id + "/delete", {}).catch(function () {});
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
