/* Site-wide behaviour: theme toggle, confirmations, flash dismissal.
   Loaded on every page; map code lives in its own files. */
(function () {
  "use strict";

  // ---------- Theme ----------
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      localStorage.setItem("petmap-theme", next);
      // Map modules listen for this to swap basemap tiles.
      window.dispatchEvent(new CustomEvent("petmap-theme", { detail: next }));
    });
  }

  // ---------- Confirm before destructive submits ----------
  // Any form carrying data-confirm asks first. Keeps the confirmation text next
  // to the action in the template rather than scattered through JS.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // ---------- Photo deletion (pet form) ----------
  // Each delete is its own POST. A form can't nest inside the main report form,
  // so the buttons retarget a single hidden form instead.
  var deleteForm = document.getElementById("photo-delete-form");
  if (deleteForm) {
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-delete-photo]");
      if (!button) return;
      event.preventDefault();
      if (!window.confirm("Delete this photo?")) return;
      deleteForm.action = button.getAttribute("data-delete-photo");
      deleteForm.submit();
    });
  }

  // ---------- Photo gallery (pet detail) ----------
  var hero = document.getElementById("hero-photo");
  if (hero) {
    document.addEventListener("click", function (event) {
      var thumb = event.target.closest("[data-full]");
      if (!thumb) return;
      hero.src = thumb.getAttribute("data-full");
    });
  }

  // ---------- Flash messages ----------
  // Auto-dismiss the positive ones; leave errors up until the user moves on,
  // since they usually describe something that still needs fixing.
  var flashes = document.querySelectorAll(".flash--success, .flash--info");
  flashes.forEach(function (el) {
    setTimeout(function () {
      el.classList.add("is-fading");
      setTimeout(function () { el.remove(); }, 400);
    }, 6000);
  });
})();

/* Shared helpers for the map modules. */
window.PetMapUtil = (function () {
  "use strict";

  var BASEMAPS = {
    light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    dark: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
  };

  function themeName() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  /* Add a CARTO basemap that follows the site theme. */
  function addBasemap(map) {
    var layer = L.tileLayer(BASEMAPS[themeName()], {
      maxZoom: 19,
      subdomains: "abcd",
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO"
    }).addTo(map);
    window.addEventListener("petmap-theme", function () {
      layer.setUrl(BASEMAPS[themeName()]);
    });
    return layer;
  }

  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /* Colour a marker by what the report is and how it ended. */
  function markerColour(props) {
    if (props.status === "reunited") return "#1c9c56";
    return props.report_type === "missing" ? "#e0342a" : "#0a9ec2";
  }

  /* A small circular pin as a divIcon — no image requests, and it recolours
     with the theme for free because the ring uses a CSS variable. */
  function pinIcon(colour, extraClass) {
    return L.divIcon({
      className: "pin-icon " + (extraClass || ""),
      html: '<span class="pin-icon__dot" style="background:' + colour + '"></span>',
      iconSize: [22, 22],
      iconAnchor: [11, 11],
      popupAnchor: [0, -12]
    });
  }

  /* CSRF token for fetch() calls that change state. */
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  return {
    addBasemap: addBasemap,
    escapeHtml: escapeHtml,
    markerColour: markerColour,
    pinIcon: pinIcon,
    csrfToken: csrfToken,
    themeName: themeName
  };
})();
