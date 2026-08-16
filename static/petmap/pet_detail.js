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

  var colour = CFG.reportType === "missing" ? "#e0342a" : "#0a9ec2";

  if (CFG.approximate) {
    // Draw the uncertainty rather than a false point. A bare marker on a
    // blurred coordinate reads as precise, which is exactly the wrong message.
    L.circle([CFG.lat, CFG.lng], {
      radius: 400, color: colour, weight: 1.5, fillOpacity: 0.12, dashArray: "4 4"
    }).addTo(map).bindPopup("Somewhere in this area.");
  } else {
    L.marker([CFG.lat, CFG.lng], { icon: U.pinIcon(colour) })
      .addTo(map)
      .bindPopup(CFG.reportType === "missing" ? "Last seen here" : "Found here");
  }

  var points = [[CFG.lat, CFG.lng]];
  (CFG.sightings || []).forEach(function (s) {
    L.marker([s.lat, s.lng], { icon: U.pinIcon("#e2820b", "pin-icon--sighting") })
      .addTo(map)
      .bindPopup("Sighting · " + U.escapeHtml(s.when));
    points.push([s.lat, s.lng]);
  });

  if (points.length > 1) {
    map.fitBounds(L.latLngBounds(points).pad(0.35));
  }

  // Exposed so search_track.js can draw the live trace on this same map.
  window.PM_detailMap = map;

  // ---------- Search coverage ----------

  var coverageLayer = L.layerGroup().addTo(map);
  var lineLayer = L.layerGroup().addTo(map);

  function renderTracks(data) {
    coverageLayer.clearLayers();
    lineLayer.clearLayers();

    (data.cells || []).forEach(function (b) {
      // b is [south, west, north, east].
      L.rectangle([[b[0], b[1]], [b[2], b[3]]], {
        className: "coverage-cell",
        color: "#1c9c56", weight: 0, fillOpacity: 0.22, interactive: false
      }).addTo(coverageLayer);
    });

    ((data.lines && data.lines.features) || []).forEach(function (f) {
      var latlngs = f.geometry.coordinates.map(function (c) { return [c[1], c[0]]; });
      var p = f.properties;
      L.polyline(latlngs, {
        color: p.source === "drone" ? "#7b5cd6" : "#1c9c56",
        weight: 3, opacity: 0.85
      }).addTo(lineLayer).bindPopup(
        "<strong>" + U.escapeHtml(p.source_label) + "</strong><br>" +
        U.escapeHtml(p.searcher) + " · " + U.escapeHtml(p.distance) +
        " · " + U.escapeHtml(p.duration));
    });

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
      dot.style.background = t.source === "drone" ? "#7b5cd6" : "#1c9c56";
      row.appendChild(dot);

      var body = document.createElement("div");
      var head = document.createElement("strong");
      head.textContent = t.source_label + " · " + t.distance;
      var meta = document.createElement("small");
      // started_label is formatted server-side in Australia/Hobart, matching
      // every other time on the page.
      meta.textContent = [
        t.started_label, t.duration, t.searcher, t.cell_count + " cells"
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

  fetch(CFG.urls.tracks, { headers: { "Accept": "application/json" } })
    .then(function (r) { return r.json(); })
    .then(renderTracks)
    .catch(function () { /* coverage is additive; the map still works without it */ });

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
        draggable: true, icon: U.pinIcon("#e2820b", "pin-icon--drag")
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

  var sightingForm = document.getElementById("sighting-form");
  if (sightingForm) {
    // Default the time to now, in the browser's local zone — for a Tasmanian
    // user that is the Tasmanian wall time the server expects.
    var whenField = document.getElementById("seen_at");
    if (whenField && !whenField.value) {
      var now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
      whenField.value = now.toISOString().slice(0, 16);
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
