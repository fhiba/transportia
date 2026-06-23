/* Transportia — trazar ruta A→B entre dos puntos del mapa (Leaflet) */
(function () {
  "use strict";

  var COLORS = {
    a: "#1F8A4C",        // verde — origen
    b: "#C0392B",        // rojo — destino
    route: "#2ecc71",    // verde brillante — polyline
    noData: "#7F8C8D"
  };

  var summaryEl = document.getElementById("summary");
  var statsEl = document.getElementById("route-stats");
  var errorEl = document.getElementById("route-error");
  var fromInput = document.getElementById("from-input");
  var toInput = document.getElementById("to-input");
  var traceBtn = document.getElementById("trace-btn");
  var clearBtn = document.getElementById("clear-btn");

  var map = L.map("map", { zoomControl: true }).setView([-34.61, -58.43], 12);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap, © CARTO",
    maxZoom: 19
  }).addTo(map);

  var markers = { a: null, b: null };
  var routeLayer = null;

  function fmtMin(s) { return (s / 60).toFixed(1) + " min"; }
  function fmtKm(m) { return (m / 1000).toFixed(2) + " km"; }

  function pinIcon(color, glyph) {
    return L.divIcon({
      className: "",
      html: '<div style="width:22px;height:22px;background:' + color +
            ';border:3px solid #16181A;border-radius:50%;display:flex;align-items:center;' +
            'justify-content:center;font:700 12px monospace;color:#E5DECB;">' + (glyph || "") + "</div>",
      iconSize: [22, 22],
      iconAnchor: [11, 11]
    });
  }

  function setPin(which, latlng) {
    var glyph = which === "a" ? "A" : "B";
    var color = which === "a" ? COLORS.a : COLORS.b;
    if (markers[which]) {
      markers[which].setLatLng(latlng);
    } else {
      markers[which] = L.marker(latlng, { icon: pinIcon(color, glyph) }).addTo(map);
    }
    var input = which === "a" ? fromInput : toInput;
    input.value = latlng.lat.toFixed(5) + ", " + latlng.lng.toFixed(5);
  }

  function clearRoute() {
    if (routeLayer) {
      map.removeLayer(routeLayer);
      routeLayer = null;
    }
    statsEl.hidden = true;
    errorEl.hidden = true;
  }

  function clearAll() {
    clearRoute();
    if (markers.a) { map.removeLayer(markers.a); markers.a = null; }
    if (markers.b) { map.removeLayer(markers.b); markers.b = null; }
    fromInput.value = "";
    toInput.value = "";
    summaryEl.className = "summary";
    summaryEl.textContent = "Tocá dos puntos del mapa para trazar una ruta de colectivo entre ellos.";
  }

  function parseLatLng(text) {
    var parts = text.split(",").map(function (s) { return parseFloat(s.trim()); });
    if (parts.length !== 2 || isNaN(parts[0]) || isNaN(parts[1])) return null;
    return L.latLng(parts[0], parts[1]);
  }

  function trace() {
    if (!markers.a || !markers.b) {
      summaryEl.className = "summary zero";
      summaryEl.textContent = "Faltan puntos. Tocá el mapa en dos lugares o escribí coordenadas.";
      return;
    }
    var a = markers.a.getLatLng();
    var b = markers.b.getLatLng();
    summaryEl.className = "summary";
    summaryEl.textContent = "Trazando ruta con OSRM + modelo…";
    clearRoute();

    var qs = "?from=" + a.lat + "," + a.lng + "&to=" + b.lat + "," + b.lng;
    fetch("/api/route/" + qs)
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || res.j.error) {
          summaryEl.className = "summary zero";
          summaryEl.textContent = res.j.error || "No se pudo trazar la ruta.";
          return;
        }
        renderRoute(res.j);
      })
      .catch(function () {
        summaryEl.className = "summary zero";
        summaryEl.textContent = "Error de red al trazar la ruta.";
      });
  }

  function renderRoute(data) {
    // Polyline principal
    routeLayer = L.polyline(data.geometry, {
      color: COLORS.route, weight: 5, opacity: 0.9
    }).addTo(map).bindTooltip("Ruta óptima · " + fmtKm(data.distance_m));

    // Ajustar viewport
    var bounds = L.latLngBounds(data.geometry);
    bounds.extend(markers.a.getLatLng());
    bounds.extend(markers.b.getLatLng());
    map.fitBounds(bounds, { padding: [30, 30] });

    // Resumen arriba
    var viaOsrm = data.routing === "osrm-local" ? "OSRM local" : "OSRM";
    summaryEl.className = "summary win";
    summaryEl.textContent =
      fmtKm(data.distance_m) + " · colectivo ~" + fmtMin(data.duration_bus_s) +
      " @ " + data.bus_speed_kmh + " km/h (modelo) · auto ~" + fmtMin(data.duration_car_s) + " · " + viaOsrm;

    // Panel de stats
    statsEl.hidden = false;
    statsEl.innerHTML =
      '<div class="stat-row"><span>DISTANCIA</span><b>' + fmtKm(data.distance_m) + '</b></div>' +
      '<div class="stat-row"><span>TIEMPO COLECTIVO (modelo)</span><b>' + fmtMin(data.duration_bus_s) + '</b></div>' +
      '<div class="stat-row"><span>VELOCIDAD MEDIA</span><b>' + data.bus_speed_kmh + ' km/h</b></div>' +
      '<div class="stat-row"><span>TIEMPO AUTO (OSRM)</span><b>' + fmtMin(data.duration_car_s) + '</b></div>' +
      '<div class="stat-row"><span>PUNTOS DE RUTA</span><b>' + data.n_points + '</b></div>' +
      '<p class="stat-note">La velocidad media (' + data.bus_speed_kmh + ' km/h) sale de la mediana observada ' +
      'en todos los segmentos GPS del dataset. El tiempo de colectivo es la distancia OSRM dividida por esa velocidad.</p>';
  }

  // Click en mapa → asignar A, luego B, luego resetear
  var nextClick = "a";
  map.on("click", function (e) {
    if (nextClick === "a") {
      clearRoute();
      setPin("a", e.latlng);
      nextClick = "b";
      summaryEl.className = "summary";
      summaryEl.textContent = "Origen fijado. Tocá el destino (B) en el mapa.";
    } else if (nextClick === "b") {
      setPin("b", e.latlng);
      nextClick = "done";
      trace();
    } else {
      clearAll();
      setPin("a", e.latlng);
      nextClick = "b";
      summaryEl.className = "summary";
      summaryEl.textContent = "Origen fijado. Tocá el destino (B) en el mapa.";
    }
  });

  // Inputs manuales
  function syncFromInput(which) {
    var ll = parseLatLng(which === "a" ? fromInput.value : toInput.value);
    if (ll) {
      setPin(which, ll);
      clearRoute();
    }
  }
  fromInput.addEventListener("change", function () { syncFromInput("a"); });
  toInput.addEventListener("change", function () { syncFromInput("b"); });

  traceBtn.addEventListener("click", function () {
    if (fromInput.value) syncFromInput("a");
    if (toInput.value) syncFromInput("b");
    if (markers.a && markers.b) {
      nextClick = "done";
      trace();
    } else {
      summaryEl.className = "summary zero";
      summaryEl.textContent = "Faltan puntos. Tocá el mapa o escribí coordenadas en ambos campos.";
    }
  });

  clearBtn.addEventListener("click", function () {
    clearAll();
    nextClick = "a";
  });
})();
