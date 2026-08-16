/* The report form's location picker.
 *
 * The pin is the authoritative location — the address box is a shortcut that
 * moves it. That ordering is deliberate: "last seen behind the oval" has no
 * street address, and forcing one would either block the report or fabricate a
 * position. So the geocoder never blocks submission and every failure is a
 * hint, not an error.
 */
(function () {
  "use strict";

  var CFG = window.PETMAP_FORM;
  var U = window.PetMapUtil;

  var latField = document.getElementById("lat");
  var lngField = document.getElementById("lng");
  var readout = document.getElementById("pin-readout");

  var map = L.map("pin-map", { zoomControl: true });
  var bounds = L.latLngBounds(CFG.bounds);
  map.setMaxBounds(bounds.pad(0.6));
  map.setMinZoom(6);
  U.addBasemap(map);

  var marker = null;

  function setPin(lat, lng, zoom) {
    if (!bounds.contains([lat, lng])) {
      readout.textContent = "That point is outside Tasmania — move the pin.";
      readout.classList.add("is-error");
      return;
    }
    readout.classList.remove("is-error");

    if (marker) {
      marker.setLatLng([lat, lng]);
    } else {
      marker = L.marker([lat, lng], {
        draggable: true,
        icon: U.pinIcon("#e0342a", "pin-icon--drag"),
        keyboard: true,
        alt: "Pet location"
      }).addTo(map);
      marker.on("dragend", function () {
        var p = marker.getLatLng();
        setPin(p.lat, p.lng);
      });
    }

    latField.value = lat.toFixed(6);
    lngField.value = lng.toFixed(6);
    readout.textContent = "Pin set at " + lat.toFixed(5) + ", " + lng.toFixed(5) +
                          " — drag it to fine-tune.";
    if (zoom) map.setView([lat, lng], zoom);
  }

  // Existing pin (edit), or the whole state (new report).
  if (CFG.lat !== null && CFG.lng !== null && !isNaN(CFG.lat) && !isNaN(CFG.lng)) {
    map.setView([CFG.lat, CFG.lng], 15);
    setPin(CFG.lat, CFG.lng);
  } else {
    map.fitBounds(bounds);
    readout.textContent = "Click the map to drop a pin where the pet was last seen.";
  }

  map.on("click", function (e) { setPin(e.latlng.lat, e.latlng.lng); });

  // ---------- Address lookup ----------

  var addressField = document.getElementById("address_raw");
  var geocodeBtn = document.getElementById("geocode-btn");
  var geocodeHint = document.getElementById("geocode-hint");
  var localityField = document.getElementById("locality");

  function lookup() {
    var address = addressField.value.trim();
    if (!address) return;
    geocodeBtn.disabled = true;
    geocodeHint.textContent = "Looking up…";
    geocodeHint.classList.remove("is-error");

    fetch(CFG.geocodeUrl + "?address=" + encodeURIComponent(address),
          { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        geocodeBtn.disabled = false;
        if (!data.ok) {
          geocodeHint.textContent = data.message || "Couldn't find that address.";
          geocodeHint.classList.add("is-error");
          return;
        }
        if (data.outside_bounds) {
          geocodeHint.textContent = "That address resolved outside Tasmania. Place the pin by hand.";
          geocodeHint.classList.add("is-error");
          return;
        }
        setPin(data.lat, data.lng, 16);
        // Only fill the suburb if the user hasn't typed one — never overwrite
        // a deliberate entry with a guess.
        if (data.locality && !localityField.value.trim()) {
          localityField.value = data.locality.charAt(0) + data.locality.slice(1).toLowerCase();
        }
        geocodeHint.textContent = data.formatted
          ? "Found: " + data.formatted + ". Check the pin and drag it if needed."
          : "Pin moved. Check it and drag if needed.";
      })
      .catch(function () {
        geocodeBtn.disabled = false;
        geocodeHint.textContent = "Address search failed. Drop the pin on the map.";
        geocodeHint.classList.add("is-error");
      });
  }

  geocodeBtn.addEventListener("click", lookup);
  addressField.addEventListener("keydown", function (e) {
    // Enter in the address box means "look this up", not "submit the report" —
    // submitting a half-filled form from a lookup attempt is a real annoyance.
    if (e.key === "Enter") { e.preventDefault(); lookup(); }
  });

  // ---------- Missing vs found ----------
  // A found animal has no name you could know, so the field is hidden rather
  // than left there to be filled in with a guess.

  var nameField = document.getElementById("name-field");
  function syncReportType() {
    var found = document.querySelector('input[name="report_type"]:checked').value === "found";
    nameField.hidden = found;
    if (found) document.getElementById("name").value = "";
  }
  document.querySelectorAll('input[name="report_type"]').forEach(function (radio) {
    radio.addEventListener("change", syncReportType);
  });
  syncReportType();

  // ---------- Submit guard ----------
  // The server validates this too; catching it here saves a round trip that
  // would otherwise lose the user's photo selection.

  document.getElementById("pet-form").addEventListener("submit", function (e) {
    if (!latField.value || !lngField.value) {
      e.preventDefault();
      readout.textContent = "Drop a pin on the map before saving.";
      readout.classList.add("is-error");
      document.getElementById("pin-map").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
})();
