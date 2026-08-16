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
