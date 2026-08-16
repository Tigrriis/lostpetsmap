/* Standalone sighting form: pin picker, plus location from the photo's EXIF. */
(function () {
  "use strict";

  var CFG = window.PETMAP_SIGHTING;
  var U = window.PetMapUtil;

  var latField = document.getElementById("lat");
  var lngField = document.getElementById("lng");
  var readout = document.getElementById("pin-readout");

  var bounds = L.latLngBounds(CFG.bounds);
  var map = L.map("pin-map", { zoomControl: true }).fitBounds(bounds);
  map.setMaxBounds(bounds.pad(0.6));
  map.setMinZoom(6);
  U.addBasemap(map);

  var marker = null;

  function setPin(lat, lng, zoom) {
    if (!bounds.contains([lat, lng])) {
      readout.textContent = "That point is outside Tasmania.";
      readout.classList.add("is-error");
      return;
    }
    readout.classList.remove("is-error");
    if (marker) {
      marker.setLatLng([lat, lng]);
    } else {
      marker = L.marker([lat, lng], {
        draggable: true, icon: U.pinIcon(U.colours.sighting(), "pin-icon--drag")
      }).addTo(map);
      marker.on("dragend", function () {
        var p = marker.getLatLng();
        setPin(p.lat, p.lng);
      });
    }
    latField.value = lat.toFixed(6);
    lngField.value = lng.toFixed(6);
    readout.textContent = "Pin set at " + lat.toFixed(5) + ", " + lng.toFixed(5) +
                          " — drag to fine-tune.";
    if (zoom) map.setView([lat, lng], zoom);
  }

  map.on("click", function (e) { setPin(e.latlng.lat, e.latlng.lng); });

  // Default the time to now, in the browser's zone — Tasmanian wall time for a
  // Tasmanian user, which is what the server expects.
  var when = document.getElementById("seen_at");
  if (when && !when.value) {
    when.value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
                   .toISOString().slice(0, 16);
  }
  if (when) {
    when.addEventListener("input", function () { when.dataset.userEdited = "1"; });
  }

  // A photo of the animal usually carries where and when it was taken, which
  // beats asking someone to remember the street corner.
  var photo = document.getElementById("photo");
  if (photo && window.PetMapExif) {
    photo.addEventListener("change", function () {
      var file = photo.files && photo.files[0];
      if (!file) return;
      window.PetMapExif.readFile(file).then(function (fix) {
        if (!fix) return;
        if (!bounds.contains([fix.lat, fix.lng])) {
          readout.textContent = "That photo was taken outside Tasmania — " +
                                "place the pin by hand.";
          readout.classList.add("is-error");
          return;
        }
        setPin(fix.lat, fix.lng, 16);
        readout.textContent = "Location taken from the photo. Drag the pin if " +
                              "it isn't quite right.";
        if (when && fix.taken && !when.dataset.userEdited) {
          var m = /^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})/.exec(fix.taken);
          if (m) when.value = m[1] + "-" + m[2] + "-" + m[3] + "T" + m[4] + ":" + m[5];
        }
      });
    });
  }

  document.getElementById("sighting-standalone").addEventListener("submit", function (e) {
    if (!latField.value || !lngField.value) {
      e.preventDefault();
      readout.textContent = "Drop a pin before posting.";
      readout.classList.add("is-error");
      document.getElementById("pin-map").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
})();
