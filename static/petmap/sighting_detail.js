/* A standalone sighting's own small map: one marker, exact location. */
(function () {
  "use strict";

  var CFG = window.PETMAP_DETAIL;
  var U = window.PetMapUtil;

  var map = L.map("detail-map", { zoomControl: true, scrollWheelZoom: false });
  map.setView([CFG.lat, CFG.lng], 16);
  U.addBasemap(map);
  map.on("click", function () { map.scrollWheelZoom.enable(); });

  var marker = null;
  function draw() {
    if (marker) map.removeLayer(marker);
    marker = L.marker([CFG.lat, CFG.lng], {
      icon: U.pinIcon(U.colours.sighting(), "pin-icon--sighting")
    }).addTo(map).bindPopup("Seen here");
  }
  draw();

  // Marker colour comes from the theme's tokens, so it has to be repainted
  // when the theme changes rather than keeping its old-mode colour.
  window.addEventListener("petmap-theme", draw);
})();
