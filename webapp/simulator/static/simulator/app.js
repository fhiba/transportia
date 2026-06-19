/* Transportia — render del mapa y panel de propuestas (Leaflet) */
(function () {
  "use strict";

  var COLORS = {
    current: "#C0392B",   // rojo — ruta actual
    zone:    "#B7892A",   // ocre — zona congestionada
    removed: "#C0392B",   // rojo — paradas eliminadas (familia ACTUAL)
    added:   "#1F8A4C",   // verde — paradas nuevas (familia PROPUESTO)
    proposed:"#1F8A4C"    // verde — recorrido propuesto
  };

  var summaryEl = document.getElementById("summary");
  var proposalsEl = document.getElementById("proposals");
  var legendEl = document.getElementById("legend");

  function fmtMin(s) { return (s / 60).toFixed(1) + " min"; }

  function squareIcon(color, glyph) {
    return L.divIcon({
      className: "",
      html: '<div style="width:16px;height:16px;background:' + color +
            ';border:2px solid #16181A;display:flex;align-items:center;justify-content:center;' +
            'font:700 11px monospace;color:#E5DECB;">' + (glyph || "") + "</div>",
      iconSize: [16, 16],
      iconAnchor: [8, 8]
    });
  }

  function legend() {
    legendEl.innerHTML =
      '<div class="row"><span class="swatch" style="background:' + COLORS.current + '"></span> Ruta actual</div>' +
      '<div class="row"><span class="swatch" style="background:' + COLORS.proposed + '"></span> Recorrido propuesto</div>' +
      '<div class="row"><span class="swatch" style="height:9px;background:' + COLORS.zone + ';opacity:.45"></span> Zona congestionada</div>' +
      '<div class="row"><span class="dot" style="background:' + COLORS.removed + '"></span> Parada eliminada</div>' +
      '<div class="row"><span class="dot" style="background:' + COLORS.added + '"></span> Parada nueva</div>';
  }

  function renderProposals(data) {
    if (!data.proposals.length) {
      proposalsEl.innerHTML = '<p class="empty">No se detectaron tramos con mejora clara para esta línea.</p>';
      return;
    }
    var html = "";
    data.proposals.forEach(function (p, i) {
      html += '<div class="prop">';
      html += '<h3>ZONA ' + (i + 1) + ' <span class="save">−' + fmtMin(p.savings) + "</span></h3>";
      html += '<div class="tt">Tiempo: <b>' + Math.round(p.original_tt) + "s</b> → <b>" +
              Math.round(p.alternative_tt) + "s</b> · gap máx " + Math.round(p.max_gap) + "m</div>";

      if (p.removed_stops.length) {
        html += '<div class="lbl">ELIMINAR ' + p.removed_stops.length + " PARADA(S)</div>";
        p.removed_stops.forEach(function (s) { html += '<span class="tag del">✕ ' + s.stop_id + "</span>"; });
      }
      var adds = p.new_stops.filter(function (s) { return s.is_new; }).concat(p.extra_stops_needed);
      if (adds.length) {
        html += '<div class="lbl">AGREGAR ' + adds.length + " PARADA(S)</div>";
        adds.forEach(function (s) {
          var lines = (s.lines && s.lines.length) ? " [" + s.lines.slice(0, 3).join(", ") + "]" : "";
          html += '<span class="tag add">+ ' + s.stop_id + lines + "</span>";
        });
      }
      html += "</div>";
    });
    proposalsEl.innerHTML = html;
  }

  function draw(data) {
    var map = L.map("map", { zoomControl: true });
    L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
      attribution: "© OpenStreetMap, © CARTO",
      maxZoom: 19
    }).addTo(map);

    var bounds = [];

    if (data.current_geometry && data.current_geometry.length > 1) {
      var cur = L.polyline(data.current_geometry, { color: COLORS.current, weight: 5, opacity: 0.85 }).addTo(map);
      bounds = bounds.concat(data.current_geometry);
      cur.bindTooltip("Ruta actual " + data.route);
    }

    data.proposals.forEach(function (p) {
      // zona congestionada (marca recta gruesa A→B)
      L.polyline([[p.lat_A, p.lon_A], [p.lat_B, p.lon_B]],
        { color: COLORS.zone, weight: 11, opacity: 0.4 }).addTo(map).bindTooltip("Zona congestionada");

      if (p.proposed_geometry && p.proposed_geometry.length > 1) {
        L.polyline(p.proposed_geometry, { color: COLORS.proposed, weight: 4, opacity: 0.95, dashArray: "1" })
          .addTo(map).bindTooltip("Recorrido propuesto (−" + fmtMin(p.savings) + ")");
        bounds = bounds.concat(p.proposed_geometry);
      }
      p.removed_stops.forEach(function (s) {
        L.marker([s.lat, s.lon], { icon: squareIcon(COLORS.removed, "✕") }).addTo(map)
          .bindTooltip("Eliminar " + s.stop_id);
      });
      p.new_stops.filter(function (s) { return s.is_new; }).concat(p.extra_stops_needed).forEach(function (s) {
        L.marker([s.lat, s.lon], { icon: squareIcon(COLORS.added, "+") }).addTo(map)
          .bindTooltip("Nueva " + s.stop_id + (s.lines && s.lines.length ? " [" + s.lines.join(", ") + "]" : ""));
      });
    });

    if (bounds.length) map.fitBounds(bounds, { padding: [25, 25] });
    else map.setView([-34.61, -58.43], 12);
  }

  function setSummary(data) {
    var cls = data.total_savings_s > 0 ? "win" : "zero";
    summaryEl.className = "summary " + cls;
    var route = data.routing === "sin-osrm"
      ? " · ⚠ sin OSRM (rutas en línea recta)"
      : " · calles vía " + data.routing;
    summaryEl.textContent =
      data.n_stops + " paradas · " + fmtMin(data.total_obs_s) + " observados · " +
      "ahorro total −" + fmtMin(data.total_savings_s) + " (" + data.total_savings_pct + "%)" + route;
  }

  legend();
  fetch("/api/simulate/" + encodeURIComponent(window.ROUTE) + "/")
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok || res.j.error) {
        summaryEl.className = "summary zero";
        summaryEl.textContent = res.j.error || "No se pudo simular esta línea.";
        proposalsEl.innerHTML = "";
        return;
      }
      setSummary(res.j);
      renderProposals(res.j);
      draw(res.j);
    })
    .catch(function () {
      summaryEl.className = "summary zero";
      summaryEl.textContent = "Error de red al simular.";
    });
})();
