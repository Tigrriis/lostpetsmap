/* The report page's two maps: where the pet was last seen (plus any sightings),
   and the picker inside the "I've seen this animal" form. */
(function () {
  "use strict";

  var CFG = window.PETMAP_DETAIL;
  var U = window.PetMapUtil;

  // ---------- Location map ----------

  var map = L.map("detail-map", { zoomControl: true, scrollWheelZoom: false });
  map.setView([CFG.lat, CFG.lng], 15);
  U.addBasemap(map);

  // Scroll-wheel zoom is off until the map is clicked, so scrolling past it on
  // a phone doesn't trap the page.
  map.on("click", function () { map.scrollWheelZoom.enable(); });

  // Everything colour-bearing lives in a layer that can be rebuilt, because
  // the style pack's tokens differ between light and dark — a marker drawn
  // once would keep its old-mode colour after the theme toggle.
  var baseLayer = L.layerGroup().addTo(map);

  function drawBase() {
    baseLayer.clearLayers();
    // Shaped like the properties the map endpoint returns, so colour and shape
    // are decided by the same two helpers the map page uses. Reading status as
    // well as report type is what makes a reunited pet look reunited here too,
    // rather than still red on its own detail page.
    var props  = { status: CFG.status, report_type: CFG.reportType };
    var colour = U.markerColour(props);
    var shape  = U.markerShape(props);

    if (CFG.approximate) {
      // Draw the uncertainty rather than a false point. A bare marker on a
      // blurred coordinate reads as precise, which is exactly the wrong message.
      L.circle([CFG.lat, CFG.lng], {
        radius: 400, color: colour, weight: 1.5, fillOpacity: 0.12, dashArray: "4 4"
      }).addTo(baseLayer).bindPopup("Somewhere in this area.");
    } else {
      L.marker([CFG.lat, CFG.lng], { icon: U.pinIcon(colour, "pin-icon--" + shape) })
        .addTo(baseLayer)
        .bindPopup(CFG.reportType === "missing" ? "Last seen here" : "Found here");
    }

    (CFG.sightings || []).forEach(function (s) {
      L.marker([s.lat, s.lng], { icon: U.pinIcon(U.colours.sighting(), "pin-icon--sighting") })
        .addTo(baseLayer)
        .bindPopup("Sighting · " + U.escapeHtml(s.when));
    });
  }
  drawBase();

  var points = [[CFG.lat, CFG.lng]];
  (CFG.sightings || []).forEach(function (s) { points.push([s.lat, s.lng]); });

  if (points.length > 1) {
    map.fitBounds(L.latLngBounds(points).pad(0.35));
  }

  // Exposed so search_track.js can draw the live trace on this same map.
  window.PM_detailMap = map;

  // Coverage arrives after the map is drawn and refits the view to include it.
  // Once the reader has panned or zoomed themselves, stop doing that — having
  // the map jump under you because a fetch landed is worse than a tight view.
  var userMovedMap = false;
  map.on("dragstart zoomstart", function () { userMovedMap = true; });

  // ---------- Search coverage ----------

  var coverageLayer = L.layerGroup().addTo(map);
  var lineLayer = L.layerGroup().addTo(map);

  function renderTracks(data) {
    coverageLayer.clearLayers();
    lineLayer.clearLayers();

    var covered = [];
    (data.cells || []).forEach(function (b) {
      // b is [south, west, north, east].
      L.rectangle([[b[0], b[1]], [b[2], b[3]]], {
        className: "coverage-cell",
        color: U.colours.coverage(), weight: 0, fillOpacity: 0.3, interactive: false
      }).addTo(coverageLayer);
      covered.push([b[0], b[1]], [b[2], b[3]]);
    });

    ((data.lines && data.lines.features) || []).forEach(function (f) {
      var latlngs = f.geometry.coordinates.map(function (c) { return [c[1], c[0]]; });
      var p = f.properties;
      L.polyline(latlngs, {
        color: p.source === "drone" ? U.colours.drone() : U.colours.onFoot(),
        weight: 3, opacity: 0.85
      }).addTo(lineLayer).bindPopup(
        "<strong>" + U.escapeHtml(p.source_label) + "</strong><br>" +
        U.escapeHtml(p.searcher) + " · " + U.escapeHtml(p.distance) +
        " · " + U.escapeHtml(p.duration));
    });

    // Widen the view to take in the coverage. Without this the map stays at
    // the pin's zoom 15, and a search that ranged a couple of kilometres is
    // drawn correctly and entirely off-screen — indistinguishable, to whoever
    // is looking, from not being drawn at all. Only ever zooms out: the pet's
    // own location must stay in frame.
    if (covered.length && !userMovedMap) {
      map.fitBounds(L.latLngBounds(covered.concat(points)).pad(0.15),
                    { maxZoom: map.getZoom() });
    }

    renderTrackList(data.tracks || []);
  }

  function renderTrackList(tracks) {
    var list = document.getElementById("track-list");
    var count = document.getElementById("track-count");
    if (!list) return;
    count.textContent = tracks.length;

    if (!tracks.length) {
      list.innerHTML = '<p class="muted">No searches logged yet.</p>';
      return;
    }

    list.innerHTML = "";
    tracks.forEach(function (t) {
      var row = document.createElement("div");
      row.className = "track-row";

      var dot = document.createElement("i");
      dot.className = "dot";
      dot.style.background = t.source === "drone" ? U.colours.drone() : U.colours.onFoot();
      row.appendChild(dot);

      var body = document.createElement("div");
      var head = document.createElement("strong");
      head.textContent = t.source_label + " · " + t.distance;
      var meta = document.createElement("small");
      // when_label is built server-side in Australia/Hobart and already spans
      // start to finish where those differ — see tracks._when_label.
      meta.textContent = [
        t.when_label || t.started_label, t.duration, t.searcher,
        t.cell_count + " cells"
      ].filter(Boolean).join(" · ");
      body.appendChild(head);
      body.appendChild(document.createElement("br"));
      body.appendChild(meta);
      if (t.notes) {
        var note = document.createElement("em");
        note.className = "track-row__note";
        note.textContent = t.notes;
        body.appendChild(document.createElement("br"));
        body.appendChild(note);
      }
      row.appendChild(body);
      list.appendChild(row);
    });
  }

  var lastTrackData = null;

  fetch(CFG.urls.tracks, { headers: { "Accept": "application/json" } })
    .then(function (r) { return r.json(); })
    .then(function (data) { lastTrackData = data; renderTracks(data); })
    .catch(function () { /* coverage is additive; the map still works without it */ });

  // Repaint on a theme switch, from the cached response rather than refetching.
  window.addEventListener("petmap-theme", function () {
    drawBase();
    if (lastTrackData) renderTracks(lastTrackData);
  });

  // ---------- Sighting picker ----------

  var pickerEl = document.getElementById("sighting-map");
  if (!pickerEl) return;

  var picker = null;
  var pickerMarker = null;
  var latField = document.getElementById("sighting-lat");
  var lngField = document.getElementById("sighting-lng");
  var readout = document.getElementById("sighting-readout");
  var bounds = L.latLngBounds(CFG.bounds);

  function initPicker() {
    if (picker) return;
    picker = L.map("sighting-map", { zoomControl: true });
    picker.setView([CFG.lat, CFG.lng], 14);
    U.addBasemap(picker);
    picker.setMaxBounds(bounds.pad(0.6));
    picker.on("click", function (e) { setSightingPin(e.latlng.lat, e.latlng.lng); });
  }

  function setSightingPin(lat, lng) {
    if (!bounds.contains([lat, lng])) {
      readout.textContent = "That point is outside Tasmania.";
      readout.classList.add("is-error");
      return;
    }
    readout.classList.remove("is-error");
    if (pickerMarker) {
      pickerMarker.setLatLng([lat, lng]);
    } else {
      pickerMarker = L.marker([lat, lng], {
        draggable: true, icon: U.pinIcon(U.colours.sighting(), "pin-icon--drag")
      }).addTo(picker);
      pickerMarker.on("dragend", function () {
        var p = pickerMarker.getLatLng();
        setSightingPin(p.lat, p.lng);
      });
    }
    latField.value = lat.toFixed(6);
    lngField.value = lng.toFixed(6);
    readout.textContent = "Sighting at " + lat.toFixed(5) + ", " + lng.toFixed(5) + ".";
  }

  // The picker lives inside a <details>. Leaflet measures the container when
  // it initialises, so building it while the element is display:none produces
  // a zero-size map with grey tiles — hence deferring until it opens.
  var disclosure = pickerEl.closest("details");
  if (disclosure) {
    if (disclosure.open) setTimeout(initPickerAndSize, 0);
    disclosure.addEventListener("toggle", function () {
      if (disclosure.open) initPickerAndSize();
    });
  } else {
    initPicker();
  }

  function initPickerAndSize() {
    initPicker();
    setTimeout(function () { picker.invalidateSize(); }, 50);
  }

  // ---------- Location from the sighting photo ----------
  // A phone photo of the animal already carries where and when it was taken.
  // Reading it here saves the reporter placing a pin by hand, and is more
  // accurate than their memory of the spot. Parsed on the device by exif.js;
  // the photo is uploaded too (sightings keep theirs), but the coordinates do
  // not wait on that.

  var photoField = document.getElementById("sighting-photo");
  if (photoField && window.PetMapExif) {
    photoField.addEventListener("change", function () {
      var file = photoField.files && photoField.files[0];
      if (!file) return;

      window.PetMapExif.readFile(file).then(function (fix) {
        if (!fix) return;                       // no GPS; the pin stays manual
        if (!bounds.contains([fix.lat, fix.lng])) {
          readout.textContent = "That photo was taken outside Tasmania — " +
                                "place the pin by hand.";
          readout.classList.add("is-error");
          return;
        }

        initPickerAndSize();
        setSightingPin(fix.lat, fix.lng);
        readout.textContent = "Location taken from the photo. Drag the pin if " +
                              "it isn't quite right.";

        // EXIF time is camera-local, which for a Tasmanian phone is exactly
        // what the datetime-local field wants. Only fill an untouched field —
        // never overwrite a time the reporter typed.
        var when = document.getElementById("seen_at");
        if (when && fix.taken && !when.dataset.userEdited) {
          var m = /^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})/.exec(fix.taken);
          if (m) when.value = m[1] + "-" + m[2] + "-" + m[3] + "T" + m[4] + ":" + m[5];
        }
      });
    });
  }

  var sightingForm = document.getElementById("sighting-form");
  if (sightingForm) {
    // Default the time to now, in the browser's local zone — for a Tasmanian
    // user that is the Tasmanian wall time the server expects.
    var whenField = document.getElementById("seen_at");
    if (whenField && !whenField.value) {
      var now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
      whenField.value = now.toISOString().slice(0, 16);
    }
    // Track deliberate edits so a photo's timestamp never clobbers one.
    if (whenField) {
      whenField.addEventListener("input", function () {
        whenField.dataset.userEdited = "1";
      });
    }

    sightingForm.addEventListener("submit", function (e) {
      if (!latField.value || !lngField.value) {
        e.preventDefault();
        readout.textContent = "Click the map to mark where you saw the animal.";
        readout.classList.add("is-error");
      }
    });
  }
})();
