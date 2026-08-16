/* The main map: clustered pet reports, filters, and a live "in view" list.
 *
 * The feed is fetched per viewport rather than all at once, so panning is
 * cheap and the payload stays small no matter how large the dataset grows.
 * Requests are debounced and the in-flight one is aborted when a new pan or
 * filter change supersedes it — otherwise a slow response can land after a
 * faster newer one and repaint the map with stale markers.
 */
(function () {
  "use strict";

  var CFG = window.PETMAP;
  var U = window.PetMapUtil;

  var map = L.map("map", { zoomControl: true }).fitBounds(CFG.bounds);
  map.setMaxBounds(L.latLngBounds(CFG.bounds).pad(0.6));
  map.setMinZoom(6);
  U.addBasemap(map);

  var cluster = L.markerClusterGroup({
    showCoverageOnHover: false,
    maxClusterRadius: 45,
    spiderfyOnMaxZoom: true
  });
  map.addLayer(cluster);

  var nearMeCircle = null;
  var pending = null;          // AbortController for the in-flight request
  var debounceTimer = null;

  var filters = {
    type: "",
    species: [],
    days: String(document.getElementById("f-days").value),
    status: "active",
    q: "",
    lat: null, lng: null, radius_km: null
  };

  // ---------- Fetching ----------

  function buildUrl() {
    var b = map.getBounds();
    var params = new URLSearchParams();
    params.set("bbox", [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].join(","));
    params.set("days", filters.days);
    params.set("status", filters.status);
    if (filters.type) params.set("type", filters.type);
    if (filters.q) params.set("q", filters.q);
    filters.species.forEach(function (s) { params.append("species", s); });
    if (filters.lat !== null && filters.radius_km) {
      params.set("lat", filters.lat);
      params.set("lng", filters.lng);
      params.set("radius_km", filters.radius_km);
    }
    return CFG.feedUrl + "?" + params.toString();
  }

  function refresh() {
    if (pending) pending.abort();
    pending = new AbortController();
    var url = buildUrl();

    fetch(url, { signal: pending.signal, headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        pending = null;
        render(data);
      })
      .catch(function (err) {
        if (err.name === "AbortError") return;   // superseded, not a failure
        pending = null;
        status("Couldn't load reports. Check your connection and pan the map to retry.", true);
      });
  }

  function scheduleRefresh() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refresh, 250);
  }

  // ---------- Rendering ----------

  function popupHtml(p) {
    var title = p.name ? U.escapeHtml(p.name)
                       : (p.report_type === "missing" ? "Missing " : "Found ") +
                         U.escapeHtml(p.species_label).toLowerCase();
    var html = '<div class="pet-popup">';
    if (p.thumb_url) {
      html += '<img class="pet-popup__img" src="' + p.thumb_url + '" alt="">';
    }
    html += '<div class="pet-popup__body">';
    html += '<div class="pet-popup__title">' + title + "</div>";
    html += '<div class="pet-popup__meta">' + U.escapeHtml(p.species_label);
    if (p.breed) html += " · " + U.escapeHtml(p.breed);
    if (p.colour) html += " · " + U.escapeHtml(p.colour);
    html += "</div>";
    html += '<div class="pet-popup__meta">' +
            (p.locality ? U.escapeHtml(p.locality) + " · " : "") +
            p.age_days + " day" + (p.age_days === 1 ? "" : "s") + " ago</div>";
    if (p.approximate) {
      html += '<div class="pet-popup__note">Approximate location</div>';
    }
    html += '<a class="btn btn--primary btn--sm" href="' + p.url + '">Open report</a>';
    html += "</div></div>";
    return html;
  }

  function render(data) {
    cluster.clearLayers();
    var features = data.features || [];

    features.forEach(function (f) {
      var p = f.properties;
      var coords = f.geometry.coordinates;      // GeoJSON is [lng, lat]
      var marker = L.marker([coords[1], coords[0]], {
        icon: U.pinIcon(U.markerColour(p), "pin-icon--" + p.report_type),
        title: p.name || p.species_label,
        alt: (p.report_type === "missing" ? "Missing " : "Found ") + p.species_label
      });
      marker.bindPopup(popupHtml(p), { minWidth: 210, maxWidth: 260 });
      marker.petId = p.id;
      cluster.addLayer(marker);
    });

    renderList(features);

    if (data.truncated) {
      status("Showing the " + features.length + " most recent reports in view — zoom in for the rest.");
    } else if (!features.length) {
      status("No reports match here. Try a wider date range, or zoom out.");
    } else {
      status(null);
    }
  }

  function renderList(features) {
    var list = document.getElementById("result-list");
    var count = document.getElementById("result-count");
    count.textContent = features.length;

    if (!features.length) {
      list.innerHTML = '<p class="muted">Nothing in view.</p>';
      return;
    }

    // Build with DOM nodes rather than innerHTML concatenation so user-supplied
    // text can't inject markup even if escaping is missed somewhere.
    list.innerHTML = "";
    features.slice(0, 100).forEach(function (f) {
      var p = f.properties;
      var item = document.createElement("a");
      item.className = "result-item";
      item.href = p.url;

      if (p.thumb_url) {
        var img = document.createElement("img");
        img.src = p.thumb_url;
        img.alt = "";
        img.loading = "lazy";
        item.appendChild(img);
      } else {
        var blank = document.createElement("span");
        blank.className = "result-item__blank";
        item.appendChild(blank);
      }

      var body = document.createElement("div");
      var title = document.createElement("strong");
      title.textContent = p.name || ((p.report_type === "missing" ? "Missing " : "Found ") +
                                     p.species_label.toLowerCase());
      var meta = document.createElement("small");
      meta.textContent = [p.locality, p.breed || p.colour,
                          p.age_days + "d ago"].filter(Boolean).join(" · ");
      body.appendChild(title);
      body.appendChild(document.createElement("br"));
      body.appendChild(meta);
      item.appendChild(body);

      var dot = document.createElement("i");
      dot.className = "dot";
      dot.style.background = U.markerColour(p);
      item.appendChild(dot);

      list.appendChild(item);
    });
  }

  function status(message, isError) {
    var el = document.getElementById("map-status");
    if (!message) { el.hidden = true; return; }
    el.textContent = message;
    el.classList.toggle("is-error", !!isError);
    el.hidden = false;
  }

  // ---------- Filter controls ----------

  document.querySelectorAll(".chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var kind = chip.dataset.filter;
      var value = chip.dataset.value;

      if (kind === "type") {
        // Single-select: exactly one of All / Missing / Found is on.
        document.querySelectorAll('[data-filter="type"]').forEach(function (c) {
          c.classList.toggle("is-on", c === chip);
        });
        filters.type = value;
      } else if (kind === "species") {
        chip.classList.toggle("is-on");
        var index = filters.species.indexOf(value);
        if (index === -1) filters.species.push(value);
        else filters.species.splice(index, 1);
      }
      scheduleRefresh();
    });
  });

  document.getElementById("f-days").addEventListener("change", function (e) {
    filters.days = e.target.value;
    scheduleRefresh();
  });

  document.getElementById("f-status").addEventListener("change", function (e) {
    filters.status = e.target.value;
    scheduleRefresh();
  });

  var searchBox = document.getElementById("f-q");
  searchBox.addEventListener("input", function (e) {
    filters.q = e.target.value.trim();
    scheduleRefresh();
  });

  // ---------- Near me ----------

  document.getElementById("near-me").addEventListener("click", function () {
    var hint = document.getElementById("near-me-hint");
    if (!navigator.geolocation) {
      hint.textContent = "Your browser won't share a location.";
      return;
    }
    hint.textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        var lat = pos.coords.latitude, lng = pos.coords.longitude;
        if (!L.latLngBounds(CFG.bounds).contains([lat, lng])) {
          hint.textContent = "You're outside Tasmania — showing the whole state instead.";
          return;
        }
        filters.lat = lat; filters.lng = lng; filters.radius_km = 10;
        if (nearMeCircle) map.removeLayer(nearMeCircle);
        nearMeCircle = L.circle([lat, lng], {
          radius: 10000, className: "near-me-circle",
          color: "#0a9ec2", weight: 1, fillOpacity: 0.05
        }).addTo(map);
        map.fitBounds(nearMeCircle.getBounds());
        hint.textContent = "Within 10 km of you. Pan the map to clear.";
        scheduleRefresh();
      },
      function () { hint.textContent = "Couldn't get your location."; },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }
    );
  });

  // Panning away clears the radius filter — the circle is a one-shot jump,
  // and leaving it applied while the user explores elsewhere is confusing.
  map.on("dragstart", function () {
    if (filters.radius_km) {
      filters.lat = filters.lng = filters.radius_km = null;
      if (nearMeCircle) { map.removeLayer(nearMeCircle); nearMeCircle = null; }
      document.getElementById("near-me-hint").textContent = "";
    }
  });

  // ---------- Coverage layer ----------
  // Cells only, for everyone — the main map never carries anybody's GPS line.
  // Off by default: it is a second, heavier request, and most visitors are
  // looking for pets rather than auditing where volunteers have been.

  var coverageLayer = L.layerGroup();
  var coveragePending = null;
  var coverageOn = false;

  function refreshCoverage() {
    if (!coverageOn) return;
    if (coveragePending) coveragePending.abort();
    coveragePending = new AbortController();

    var b = map.getBounds();
    var url = CFG.coverageUrl + "?bbox=" +
              [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].join(",");

    fetch(url, { signal: coveragePending.signal, headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        coveragePending = null;
        coverageLayer.clearLayers();
        (data.cells || []).forEach(function (c) {
          L.rectangle([[c[0], c[1]], [c[2], c[3]]], {
            color: "#1c9c56", weight: 0, fillOpacity: 0.22, interactive: false
          }).addTo(coverageLayer);
        });
        if (data.truncated) status("Too much search coverage to draw here — zoom in.");
      })
      .catch(function (err) {
        if (err.name !== "AbortError") coveragePending = null;
      });
  }

  document.getElementById("f-coverage").addEventListener("change", function (e) {
    coverageOn = e.target.checked;
    if (coverageOn) {
      map.addLayer(coverageLayer);
      refreshCoverage();
    } else {
      map.removeLayer(coverageLayer);
      coverageLayer.clearLayers();
    }
  });

  // ---------- Collapsible filter panel (mobile) ----------

  var filtersToggle = document.getElementById("filters-toggle");
  var filtersBody = document.getElementById("filters-body");

  function setFiltersOpen(open) {
    if (open) { filtersBody.removeAttribute("hidden"); filtersToggle.textContent = "Hide"; }
    else { filtersBody.setAttribute("hidden", ""); filtersToggle.textContent = "Filters"; }
    filtersToggle.setAttribute("aria-expanded", String(open));
  }

  filtersToggle.addEventListener("click", function () {
    setFiltersOpen(filtersBody.hasAttribute("hidden"));
  });

  // On a phone the panels stack, so an open filter list pushes the map most of
  // a screen down — the map is what people came for, so it starts collapsed.
  // Matches the CSS breakpoint at 60rem.
  if (window.matchMedia("(max-width: 60rem)").matches) setFiltersOpen(false);

  // ---------- Go ----------

  map.on("moveend", scheduleRefresh);
  map.on("moveend", refreshCoverage);
  refresh();
})();
