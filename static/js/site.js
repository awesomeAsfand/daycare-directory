// GulfNurseries: small enhancements; every page works without this file.
(function () {
  "use strict";

  // Mobile menu and search panels: <button data-toggle="id">
  document.querySelectorAll("[data-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var panel = document.getElementById(btn.dataset.toggle);
      if (!panel) return;
      panel.hidden = !panel.hidden;
      btn.setAttribute("aria-expanded", String(!panel.hidden));
      var input = !panel.hidden && panel.querySelector("input");
      if (input) input.focus();
    });
  });

  // Close an open dropdown (<details class="chip-select">) when clicking elsewhere
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.chip-select[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });

  // Live suggestions under a search box: <input data-suggest="/search/suggest/">
  document.querySelectorAll("input[data-suggest]").forEach(function (input) {
    var box = document.getElementById(input.getAttribute("aria-controls"));
    var form = input.form;
    var timer, last = "";
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = input.value.trim();
        if (q === last) return;
        last = q;
        if (q.length < 2) { box.innerHTML = ""; return; }
        var params = new URLSearchParams(new FormData(form));
        fetch(input.dataset.suggest + "?" + params.toString())
          .then(function (r) { return r.text(); })
          .then(function (html) { if (input.value.trim() === q) box.innerHTML = html; });
      }, 250);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" && box.firstElementChild) { e.preventDefault(); box.querySelector("a").focus(); }
      if (e.key === "Escape") box.innerHTML = "";
    });
    box.addEventListener("keydown", function (e) {
      var a = document.activeElement;
      if (e.key === "ArrowDown" && a.nextElementSibling) { e.preventDefault(); a.nextElementSibling.focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); (a.previousElementSibling || input).focus(); }
    });
    document.addEventListener("click", function (e) {
      if (!form.contains(e.target)) box.innerHTML = "";
    });
  });

  // Hero "City or neighbourhood" select: grey until something is picked
  document.querySelectorAll(".hero-search select").forEach(function (s) {
    var set = function () { s.classList.toggle("has-value", !!s.value); };
    s.addEventListener("change", set); set();
  });

  // City page: on phones the neighbourhood groups are an accordion with the first one open
  if (window.matchMedia("(max-width: 899px)").matches) {
    document.querySelectorAll(".regions:not(.regions--az) .region").forEach(function (d, i) {
      if (i > 0) d.removeAttribute("open");
    });
  }

  // Photos dialog on the nursery page
  document.querySelectorAll("[data-dialog]").forEach(function (btn) {
    var dialog = document.getElementById(btn.dataset.dialog);
    if (!dialog || !dialog.showModal) return;
    btn.addEventListener("click", function (e) { e.preventDefault(); dialog.showModal(); });
    dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
  });

  // Neighbourhood map (Leaflet, loaded on that page only)
  var data = document.getElementById("map-data");
  if (data && window.L) {
    var pins = JSON.parse(data.textContent);
    var maps = [];
    var build = function (el) {
      var map = L.map(el, { scrollWheelZoom: false });
      // OSM's tile policy requires a Referer; the site's own policy (same-origin)
      // would send none, so the tiles send just the origin
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
        referrerPolicy: "strict-origin-when-cross-origin"
      }).addTo(map);
      var bounds = [];
      pins.forEach(function (p, i) {
        var icon = L.divIcon({
          className: "", iconSize: null,
          html: '<span class="pin' + (i === 0 ? " pin--top" : "") + '">' + (p.rating ? "★ " + p.rating : "•") + "</span>"
        });
        var a = document.createElement("a");
        a.href = p.url; a.textContent = p.name;
        L.marker([p.lat, p.lng], { icon: icon, title: p.name }).addTo(map).bindPopup(a);
        bounds.push([p.lat, p.lng]);
      });
      if (bounds.length) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
      maps.push(map);
      return map;
    };
    var desktop = document.getElementById("map");
    if (desktop && desktop.offsetParent !== null) build(desktop);
    var sheet = document.getElementById("map-sheet");
    var mobileMap = null;
    document.querySelectorAll("[data-map-open]").forEach(function (b) {
      b.addEventListener("click", function () {
        sheet.hidden = false;
        if (!mobileMap) mobileMap = build(document.getElementById("map-mobile"));
        else mobileMap.invalidateSize();
      });
    });
    document.querySelectorAll("[data-map-close]").forEach(function (b) {
      b.addEventListener("click", function () { sheet.hidden = true; });
    });
  }
})();
