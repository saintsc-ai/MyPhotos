/*
 * Map view module — the Leaflet-based map tab. Extracted from
 * index.html (Phase 4d). Owns:
 *
 *   STATE
 *     - the Leaflet map + layerGroup (markers) instance, plus the
 *       light/dark tile layers
 *     - the debounced moveend reload timer + a "we're programmatic"
 *       guard so fitBounds doesn't re-fire its own moveend handler
 *     - the cluster context-menu DOM node + its outside-click closer
 *     - the side panel's current cell (so a second click can toggle)
 *     - the active spider's layerGroup + its source cluster lat/lng
 *
 *   DOM
 *     #map (Leaflet root), #map-side-panel + body/count/close, plus
 *     the floating .cluster-context-menu node it injects on demand.
 *
 *   BEHAVIOUR
 *     - Server-side clustering at /api/photos/locations/clusters; the
 *       module just fetches per-bbox + per-zoom and paints whatever
 *       comes back as either a single-photo marker or a count bubble
 *     - Left-click on any marker → side panel with the cell's photos
 *       grouped by date (taken_at desc, capped at 500)
 *     - Right-click / long-press on a count bubble → cluster chooser
 *       (펼치기 / 사진 보기) floating at the cursor
 *     - Right-click / long-press on a single-photo marker → straight
 *       to the lightbox via window.lightbox.openForPhotoId
 *     - 펼치기 = spider: ring (≤11) or Archimedean spiral (12-200)
 *       around the cluster center; the rest of the map gets dimmed
 *       via body.spider-open
 *     - Map pan/zoom/click closes the cluster menu + any active spider
 *
 * Dependencies (loaded as globals before this file):
 *   - L (Leaflet)                — from the <script src> in <head>
 *   - $, escapeAttr, _t, _tn      (/js/common.js)
 *   - api / friendlyError         (/js/api.js)
 *   - window.lightbox.*           (/js/panels/lightbox.js)
 *
 * Public surface (window.mapView):
 *   init({ filterQueryString, getTheme })
 *                                 — wire deps. Doesn't construct the
 *                                   Leaflet map yet — that waits for
 *                                   activate() so #map is visible and
 *                                   sized first.
 *   activate()                    — call from setView("map"). Idempotent:
 *                                   first call lazily inits the map +
 *                                   fits the densest-region bbox, every
 *                                   call kicks invalidateSize so leaflet
 *                                   re-measures after the display flip.
 *   applyTheme(theme)             — "light" or "dark"; swaps basemap.
 *                                   Safe to call before activate() —
 *                                   the choice persists and initMap
 *                                   picks it up on first run.
 *   reload()                      — call after the filter set changes;
 *                                   re-centers on the (filtered) hotspot
 *                                   and reloads visible markers.
 *   closeSidePanel()              — call from setView when leaving the
 *                                   map tab so the panel doesn't linger.
 */
(function () {
  "use strict";

  // --- Constants --------------------------------------------------
  const SPIDER_CAP = 200;

  // --- State ------------------------------------------------------
  let map = null;
  let markers = null;
  let _markerReloadTimer = null;
  let _suppressMoveReload = false;
  let _lightTiles = null, _darkTiles = null;
  let _pendingTheme = null;   // requested before initMap ran
  let _clusterMenuEl = null;
  let _clusterMenuCloser = null;
  let _mapSidePanelCell = null;
  let _activeSpider = null;

  // --- Deps -------------------------------------------------------
  let _deps = {};
  function _fq()    { return _deps.filterQueryString ? _deps.filterQueryString() : ""; }
  function _theme() { return _deps.getTheme ? _deps.getTheme() : "dark"; }

  // --- Tile layers (built once, reused on every theme swap) -------
  function _buildTileLayers() {
    if (_lightTiles && _darkTiles) return;
    // Light → stock OSM (local-language labels).
    _lightTiles = L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
      }
    );
    // Dark → CartoDB Dark Matter; paired with a stronger CSS brightness
    // filter in the stylesheet so the basemap reads as a muted night
    // view rather than near-black, while still keeping English labels
    // visible.
    _darkTiles = L.tileLayer(
      "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      {
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
          ' contributors © <a href="https://carto.com/attributions">CARTO</a>',
        maxZoom: 19,
        subdomains: "abcd",
      }
    );
  }

  function applyTheme(theme) {
    if (!map) {
      // Map not built yet — remember the choice; initMap will use it.
      _pendingTheme = theme;
      return;
    }
    _buildTileLayers();
    const isDark = theme === "dark";
    const wanted = isDark ? _darkTiles : _lightTiles;
    const other = isDark ? _lightTiles : _darkTiles;
    if (map.hasLayer(other)) map.removeLayer(other);
    if (!map.hasLayer(wanted)) wanted.addTo(map);
  }

  // --- Init Leaflet map (called lazily on first activate) ---------
  async function initMap() {
    if (map) return;
    map = L.map("map").setView([37.5, 127.0], 7);
    _buildTileLayers();
    const initialTheme = _pendingTheme || _theme();
    _pendingTheme = null;
    (initialTheme === "dark" ? _darkTiles : _lightTiles).addTo(map);
    // Dedicated pane for spider markers. leaflet's default marker
    // pane is z-index 600 and creates its own stacking context, so
    // any per-marker z-index lift gets clamped by the pane when
    // compared against external overlays. A separate pane at 700
    // lets the spider tiles sit above the dim overlay (650) while
    // standalone markers + clusters stay in the 600 pane and end
    // up correctly muted underneath.
    map.createPane("spiderPane");
    map.getPane("spiderPane").style.zIndex = "700";
    // Plain layer group — clustering is server-side now so the build
    // cost stays sub-second even on dense home/work areas. Leaflet
    // just renders the pre-aggregated ~100 cells.
    markers = L.layerGroup();
    map.addLayer(markers);

    // The popup's box is sized before the thumbnail loads → the image
    // overflows the white frame. Re-measure once the image has natural
    // dimensions.
    map.on("popupopen", (e) => {
      const popup = e.popup;
      const el = popup.getElement && popup.getElement();
      const img = el && el.querySelector(".map-photo-popup img");
      if (!img) return;
      if (img.complete && img.naturalWidth > 0) {
        popup.update();
      } else {
        const refit = () => popup.update();
        img.addEventListener("load", refit, { once: true });
        img.addEventListener("error", refit, { once: true });
      }
    });

    // Pan / zoom → reload markers for the new viewport (debounced).
    map.on("moveend", () => {
      if (_suppressMoveReload) return;
      if (_markerReloadTimer) clearTimeout(_markerReloadTimer);
      _markerReloadTimer = setTimeout(loadMarkersInBounds, 300);
    });

    // Pan/zoom dismisses the floating cluster menu (its position is
    // in viewport coords and won't follow the map).
    map.on("movestart zoomstart", closeClusterMenu);
    // zoomstart catches mouse-wheel / pinch / +/- buttons; map click
    // fires only on the tile background (marker clicks don't bubble),
    // so clicking inside the spider keeps it open.
    map.on("zoomstart", () => { if (_activeSpider) closeSpider(); });
    map.on("click", () => { if (_activeSpider) closeSpider(); });

    _addLocateControl();

    await centerOnHotspotAndLoad();
  }

  // --- "Locate me" control ----------------------------------------
  // Browser Geolocation API only works in a secure context (HTTPS or
  // localhost). Over plain HTTP on a LAN, most desktop browsers and
  // Android Chrome refuse to even ask the user; iOS Safari is more
  // lenient. We add the control unconditionally and let the user see
  // a clear error if their browser blocks it — the alternative
  // (hiding the button) is more confusing than a clean "denied" toast.
  let _locateMarker = null;
  let _locateMarkerTimer = null;

  function _dropLocateMarker(latlng, accuracyMeters) {
    if (_locateMarker) {
      try { map.removeLayer(_locateMarker); } catch (_) {}
    }
    // Translucent accuracy ring + a solid pin in the middle. Both
    // self-clear after 30 s so the marker doesn't outstay its welcome
    // while the user pans the map looking for photos.
    _locateMarker = L.layerGroup([
      L.circle(latlng, {
        radius: Math.max(accuracyMeters || 30, 30),
        color: "#3b82f6", fillColor: "#3b82f6",
        fillOpacity: 0.12, weight: 1,
      }),
      L.circleMarker(latlng, {
        radius: 7, color: "#fff", weight: 2,
        fillColor: "#3b82f6", fillOpacity: 1,
      }),
    ]);
    _locateMarker.addTo(map);
    if (_locateMarkerTimer) clearTimeout(_locateMarkerTimer);
    _locateMarkerTimer = setTimeout(() => {
      if (_locateMarker) {
        try { map.removeLayer(_locateMarker); } catch (_) {}
        _locateMarker = null;
      }
    }, 30000);
  }

  function _locateMe() {
    if (!map) return;
    if (!navigator.geolocation) {
      alert(_t("map.locate_unavailable",
        "이 브라우저는 위치 정보를 지원하지 않습니다."));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const lat = pos.coords.latitude;
        const lng = pos.coords.longitude;
        const acc = pos.coords.accuracy;
        // Zoom 15 ≈ neighbourhood scale. Lower (zoom out) and clusters
        // near the user collapse into one bubble; higher and you lose
        // the surrounding context. flyTo's debounced moveend then
        // re-fetches the bbox so the user sees photos in that area.
        map.flyTo([lat, lng], Math.max(15, map.getZoom()), { duration: 0.6 });
        _dropLocateMarker([lat, lng], acc);
      },
      (err) => {
        if (err && err.code === err.PERMISSION_DENIED) {
          alert(_t("map.locate_denied",
            "위치 권한이 거부되었습니다. 브라우저 설정에서 허용해 주세요."));
        } else {
          // POSITION_UNAVAILABLE / TIMEOUT / insecure-context refusal
          // all land here. Include the browser's message so the user
          // can see "Only secure origins are allowed" if that's the
          // actual cause (HTTP on LAN).
          alert(_t("map.locate_failed", "현재 위치를 가져올 수 없습니다.")
            + (err && err.message ? "\n\n" + err.message : ""));
        }
      },
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
    );
  }

  function _addLocateControl() {
    const LocateControl = L.Control.extend({
      onAdd: function () {
        const div = L.DomUtil.create("div",
          "leaflet-bar leaflet-control map-locate-control");
        const label = _t("map.locate_me", "현재 위치로 이동");
        div.innerHTML = `<a href="#" role="button" ` +
          `title="${escapeAttr(label)}" aria-label="${escapeAttr(label)}">📍</a>`;
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.on(div, "click", (e) => {
          L.DomEvent.preventDefault(e);
          _locateMe();
        });
        return div;
      },
    });
    new LocateControl({ position: "topleft" }).addTo(map);
  }

  // --- Fit to densest region + first marker load ------------------
  async function centerOnHotspotAndLoad() {
    if (!map || !markers) return;
    try {
      // Pass the same filter query the cluster fetch uses, so the
      // chosen hotspot bbox lands ON the filtered photos. Without
      // this, a narrow filter (e.g. 텍스트 있음 with OCR run only on
      // a few photos) would seed the viewport to the all-photo
      // hotspot — typically a totally different region — and the
      // subsequent filtered cluster fetch would return 0, reading as
      // "no clusters on the map" even though matching photos exist
      // elsewhere.
      const fq = _fq();
      const url = "/api/photos/locations/initial-bbox" + (fq ? "?" + fq : "");
      const r = await fetch(url);
      if (r.ok) {
        const hot = await r.json();
        if (hot && hot.min_lat != null) {
          _suppressMoveReload = true;
          map.fitBounds(
            [[hot.min_lat, hot.min_lng], [hot.max_lat, hot.max_lng]],
            { padding: [40, 40], animate: false }
          );
          // Release suppression after fitBounds settles. moveend fires
          // synchronously after fitBounds with animate:false.
          setTimeout(() => { _suppressMoveReload = false; }, 0);
        }
      }
    } catch (_) { /* ignore — keep default view */ }
    await loadMarkersInBounds();
  }

  // --- Marker load (filter-aware, current viewport) ---------------
  async function loadMarkersInBounds() {
    if (!map || !markers) return;
    const b = map.getBounds();
    const bbox = `${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`;
    const zoom = map.getZoom();
    const fq = _fq();
    const url = `/api/photos/locations/clusters?bbox=${encodeURIComponent(bbox)}&zoom=${zoom}`
      + (fq ? "&" + fq : "");
    let data;
    try {
      const res = await fetch(url);
      if (!res.ok) return;
      data = await res.json();
    } catch (_) { return; }
    markers.clearLayers();
    closeSpider();
    for (const c of data) {
      markers.addLayer(makeMapMarker(c));
    }
  }

  // --- Long-press → contextmenu (mobile fallback) -----------------
  // Leaflet's native `contextmenu` fires on mouse right-click + some
  // browsers' long-press, but coverage on iOS Safari / Chrome-Android
  // is inconsistent (OS menu wins, or nothing fires). Wrap the
  // binding so EVERY marker gets both the native event AND a touch
  // long-press fallback that synthesizes a contextmenu-shaped event
  // for the same handler. 500 ms hold + ≤10 px movement budget.
  function bindMarkerLongPress(m, handler) {
    m.on("contextmenu", handler);
    m.on("add", () => {
      const el = m.getElement ? m.getElement() : null;
      if (!el) return;
      let timer = null;
      let startX = 0, startY = 0;
      let lastX = 0, lastY = 0;
      const cancel = () => {
        if (timer) { clearTimeout(timer); timer = null; }
      };
      el.addEventListener("touchstart", (e) => {
        const t = e.touches[0]; if (!t) return;
        startX = lastX = t.clientX;
        startY = lastY = t.clientY;
        cancel();
        timer = setTimeout(() => {
          timer = null;
          handler({
            originalEvent: {
              clientX: lastX, clientY: lastY,
              preventDefault: () => {},
              stopPropagation: () => {},
            },
          });
        }, 500);
      }, { passive: true });
      el.addEventListener("touchmove", (e) => {
        const t = e.touches[0]; if (!t) return;
        lastX = t.clientX; lastY = t.clientY;
        if (Math.hypot(lastX - startX, lastY - startY) > 10) cancel();
      }, { passive: true });
      el.addEventListener("touchend", cancel);
      el.addEventListener("touchcancel", cancel);
    });
  }

  // --- Marker factory ---------------------------------------------
  function makeMapMarker(c) {
    // estimated_count > 0 means at least one photo in this cell has
    // an inferred (not EXIF) location. Add a class so CSS softens the
    // border / makes it dashed — visually obvious without a second
    // marker layer.
    const estCls = (c.estimated_count | 0) > 0 ? " est-marker" : "";
    if (c.count === 1) {
      // Single photo — round thumbnail marker, same interaction model
      // as the cluster bubbles: left-click opens the side panel
      // (containing this one photo), right-click jumps straight to
      // the lightbox. Skipping the chooser popup since 줌인 / 펼치기
      // are meaningless for a single-photo cell.
      const icon = L.divIcon({
        html:
          `<div class="map-photo-marker${estCls}" title="${escapeAttr(_t("map.marker_title", "클릭: 사진 목록 옆에 펼치기 — 우클릭: 바로 사진 보기"))}">` +
          `<img src="/api/photos/${c.sample_id}/thumb?size=256" alt="" ` +
          `style="width:100%;height:100%;object-fit:cover;object-position:center;display:block">` +
          `</div>`,
        className: "map-photo-marker-wrap",
        iconSize: [40, 40],
        iconAnchor: [20, 20],
      });
      const m = L.marker([c.lat, c.lng], { icon });
      m.on("click", (e) => {
        if (e.originalEvent) e.originalEvent.stopPropagation();
        openMapSidePanel(c);
      });
      bindMarkerLongPress(m, (e) => {
        if (e.originalEvent) {
          e.originalEvent.preventDefault();
          e.originalEvent.stopPropagation();
        }
        try { map.closePopup(); } catch (_) { /* ignore */ }
        window.lightbox.openForPhotoId(c.sample_id);
      });
      return m;
    }
    const size = c.count >= 100 ? "big" : (c.count >= 10 ? "med" : "");
    const label = c.count >= 1000 ? `${Math.round(c.count / 100) / 10}k` : c.count;
    // < 1000 photos: cluster can be spidered into individual markers.
    // ≥ 1000 photos: spider would be unreadable so the chooser falls
    //                back to a capped lightbox (counter still shows
    //                the true count).
    const titleAttr = _t("map.cluster_title",
      "클릭: 사진 목록 — 우클릭: 메뉴 (펼치기 / 사진 보기)");
    const icon = L.divIcon({
      html: `<div class="map-cluster ${size}${estCls}" title="${titleAttr}">${label}</div>`,
      className: "map-cluster-wrap",
      iconSize: size === "big" ? [44, 44] : [36, 36],
      iconAnchor: size === "big" ? [22, 22] : [18, 18],
    });
    const m = L.marker([c.lat, c.lng], { icon });
    // Left-click → side panel with the cell's photo grid.
    m.on("click", (e) => {
      if (e.originalEvent) e.originalEvent.stopPropagation();
      openMapSidePanel(c);
    });
    // Right-click → free-floating context menu at the cursor position
    // (Windows-style). bindMarkerLongPress also covers mobile via
    // the synthesized event.
    bindMarkerLongPress(m, (e) => {
      if (e.originalEvent) {
        e.originalEvent.preventDefault();
        e.originalEvent.stopPropagation();
      }
      openClusterMenu(e, c);
    });
    return m;
  }

  // --- Floating cluster context menu ------------------------------
  function closeClusterMenu() {
    if (_clusterMenuEl) {
      _clusterMenuEl.remove();
      _clusterMenuEl = null;
    }
    if (_clusterMenuCloser) {
      document.removeEventListener("mousedown", _clusterMenuCloser, true);
      document.removeEventListener("contextmenu", _clusterMenuCloser, true);
      _clusterMenuCloser = null;
    }
  }
  function openClusterMenu(e, c) {
    closeClusterMenu();
    const oe = e.originalEvent;
    const x = (oe ? oe.clientX : 0);
    const y = (oe ? oe.clientY : 0);

    const el = document.createElement("div");
    el.className = "cluster-context-menu";
    el.innerHTML = buildClusterChoiceHtml(c);
    document.body.appendChild(el);

    // Position with 2px gap from the click; nudge inward if the menu
    // would spill off the right/bottom edge of the viewport.
    const rect = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = x + 2;
    let top = y + 2;
    if (left + rect.width > vw - 4) left = Math.max(4, x - rect.width - 2);
    if (top + rect.height > vh - 4) top = Math.max(4, y - rect.height - 2);
    el.style.left = left + "px";
    el.style.top = top + "px";

    _clusterMenuEl = el;
    attachClusterChoiceHandlers(el, c);

    _clusterMenuCloser = (ev) => {
      if (ev.type === "mousedown" && el.contains(ev.target)) return;
      closeClusterMenu();
    };
    // Defer attachment a tick so the originating right-click doesn't
    // immediately close the menu we just opened.
    setTimeout(() => {
      document.addEventListener("mousedown", _clusterMenuCloser, true);
      document.addEventListener("contextmenu", _clusterMenuCloser, true);
    }, 0);
  }

  function buildClusterChoiceHtml(c) {
    const spiderLabel = c.count > SPIDER_CAP
      ? _tn("map.spider_top_n", "펼치기 (상위 {n}장)", { n: SPIDER_CAP })
      : _t("map.spider", "펼치기");
    const lightboxLabel = _t("map.view_photos", "사진 보기");
    return `
      <div class="cluster-choice">
        <button data-action="spider">
          <span class="ic">⊕</span>${escapeAttr(spiderLabel)}
        </button>
        <button data-action="lightbox">
          <span class="ic">🖼</span>${escapeAttr(lightboxLabel)}
        </button>
      </div>
    `;
  }

  function attachClusterChoiceHandlers(root, c) {
    if (!root) return;
    root.querySelectorAll("[data-action]").forEach(btn => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (btn.disabled) return;
        const action = btn.dataset.action;
        closeClusterMenu();
        if (action === "spider") {
          spiderfyCluster(c);
        } else if (action === "lightbox") {
          openLightboxFromCell(c.lat, c.lng, map.getZoom(), c.sample_id);
        }
      });
    });
  }

  // --- Side panel (left-click on any marker) ----------------------
  function openMapSidePanel(c) {
    const panel = $("#map-side-panel");
    // Toggle: clicking the same cluster while open closes it.
    if (
      panel.classList.contains("open") &&
      _mapSidePanelCell &&
      _mapSidePanelCell.lat === c.lat &&
      _mapSidePanelCell.lng === c.lng &&
      _mapSidePanelCell.sample_id === c.sample_id
    ) {
      closeMapSidePanel();
      return;
    }
    const body = $("#map-side-panel-body");
    const countEl = $("#map-side-panel-count");
    _mapSidePanelCell = c;
    const loadingTxt = _t("common.loading", "불러오는 중…");
    countEl.textContent = loadingTxt;
    body.innerHTML = `<div class="panel-empty">${escapeAttr(loadingTxt)}</div>`;
    panel.classList.add("open");
    // Body marker so the lightbox CSS can shift right and avoid
    // sitting under the panel when both are open simultaneously.
    document.body.classList.add("map-side-panel-open");

    const fq = _fq();
    // Cap matches the contextmenu lightbox fallback so very large
    // clusters don't try to render thousands of <img> in one shot.
    const limit = c.count >= 1000 ? 500 : Math.min(500, c.count);
    const url =
      `/api/photos/in-cell?lat=${c.lat}&lng=${c.lng}&zoom=${map.getZoom()}` +
      `&limit=${limit}` + (fq ? "&" + fq : "");
    fetch(url)
      .then(async r => {
        if (!r.ok) {
          // Surface HTTP failures distinctly from network errors and
          // from render-throws below. Caller's .catch sees an Error
          // with a useful message.
          const txt = (await r.text().catch(() => "")).slice(0, 200);
          throw new Error(`HTTP ${r.status}${txt ? ": " + txt : ""}`);
        }
        return r.json();
      })
      .then(arr => {
        if (_mapSidePanelCell !== c) return;     // user clicked another cluster
        if (!Array.isArray(arr) || !arr.length) {
          countEl.textContent = _tn("map.panel_count", "{shown}개 항목", { shown: 0 });
          body.innerHTML = `<div class="panel-empty">${escapeAttr(_t("map.panel_empty", "사진을 찾을 수 없습니다."))}</div>`;
          return;
        }
        // Wrap the render in its own try so a render bug doesn't
        // fall through to the fetch-level catch — that path was
        // showing "불러오기 실패" while the count already said
        // "1개 항목", which made debugging impossible (looked like
        // a network issue when it was actually a JS exception).
        try {
          renderMapSidePanel(arr, c.count);
        } catch (e) {
          console.error("renderMapSidePanel failed", e, "arr=", arr);
          body.innerHTML =
            `<div class="panel-empty" style="color:#f99">`
            + escapeAttr(_t("map.panel_render_failed", "표시 실패: "))
            + escapeAttr(String(e && e.message || e || "unknown"))
            + `</div>`;
        }
      })
      .catch(e => {
        if (_mapSidePanelCell !== c) return;
        // Log so the user can hand us the error text from devtools
        // — generic "불러오기 실패" alone has no actionable hint.
        console.error("map side-panel fetch failed", e, "url=", url);
        body.innerHTML = `<div class="panel-empty">${escapeAttr(_t("map.panel_load_failed", "불러오기 실패"))}</div>`;
      });
  }

  function closeMapSidePanel() {
    $("#map-side-panel").classList.remove("open");
    document.body.classList.remove("map-side-panel-open");
    _mapSidePanelCell = null;
  }

  // Format an ISO date (YYYY-MM-DD) in the user's active language via
  // Intl. Each locale gets its native long-form date with weekday —
  // "2024년 10월 12일 토" (ko), "Sat, October 12, 2024" (en), and so on.
  // null/undefined → catalog's "(no date)" label so photos without
  // taken_at still group cleanly.
  function _localeDateLabel(iso) {
    if (!iso) return _t("gal.no_date_label", "날짜 없음");
    const d = new Date(iso + "T00:00:00");
    if (isNaN(d.getTime())) return iso;
    const lang = (window.i18n && window.i18n.getCurrentLang)
      ? window.i18n.getCurrentLang() : "ko";
    try {
      return new Intl.DateTimeFormat(lang, {
        year: "numeric", month: "long", day: "numeric", weekday: "short",
      }).format(d);
    } catch (_) {
      return iso;       // Intl unavailable / bad lang tag — never mind
    }
  }

  function renderMapSidePanel(photos, totalCount) {
    const body = $("#map-side-panel-body");
    const countEl = $("#map-side-panel-count");
    const shown = photos.length;
    countEl.textContent = totalCount > shown
      ? _tn("map.panel_count_truncated",
          "{shown}개 / 총 {total}개 (상위 {shown}장만 표시)",
          { shown, total: totalCount.toLocaleString() })
      : _tn("map.panel_count", "{shown}개 항목", { shown });

    // Group by date — photos are already sorted taken_at desc by the
    // server; we preserve that order, just inserting date headers.
    // curKey starts undefined (NOT null) so a first photo with
    // taken_at=null still pushes the initial group — otherwise
    // key (null) === curKey (null) skipped the push and the
    // groups[-1].photos.push below threw
    // "Cannot read properties of undefined (reading 'photos')".
    const groups = [];
    let curKey;     // undefined sentinel: distinct from any real key
    for (const p of photos) {
      const key = p.taken_at ? p.taken_at.slice(0, 10) : null;
      if (key !== curKey) {
        groups.push({ key, photos: [] });
        curKey = key;
      }
      groups[groups.length - 1].photos.push(p);
    }

    const html = groups.map(g => {
      const imgs = g.photos.map(p =>
        `<img src="/api/photos/${p.id}/thumb?size=256" ` +
        `data-id="${p.id}" loading="lazy" ` +
        `alt="${escapeAttr(p.filename || "")}" ` +
        `title="${escapeAttr(p.filename || "")}">`
      ).join("");
      return `<div class="date-group">` +
        `<h3>${escapeAttr(_localeDateLabel(g.key))}</h3>` +
        `<div class="thumb-grid">${imgs}</div></div>`;
    }).join("");
    body.innerHTML = html;
    body.scrollTop = 0;

    body.querySelectorAll("img[data-id]").forEach(img => {
      img.addEventListener("click", () => {
        const id = parseInt(img.dataset.id, 10);
        if (!id) return;
        // On wide screens we keep the panel open — the lightbox slides
        // rightward (body.map-side-panel-open rule) so both are visible.
        // On narrow screens that shift pushes the lightbox off-screen
        // AND the panel sits on top via z-index, so we close the panel
        // before opening the lightbox. User can re-open by clicking
        // the cluster again.
        if (window.matchMedia("(max-width: 768px)").matches) {
          closeMapSidePanel();
        }
        window.lightbox.openForPhotoId(id);
      });
    });
  }

  // --- Spiderfy ---------------------------------------------------
  function closeSpider() {
    if (!_activeSpider) return;
    try { map.removeLayer(_activeSpider.layer); } catch (_) { /* ignore */ }
    document.body.classList.remove("spider-open");
    _activeSpider = null;
  }

  async function spiderfyCluster(c) {
    // Toggle: clicking the cluster that's already spidered closes it.
    if (
      _activeSpider &&
      _activeSpider.sourceLat === c.lat &&
      _activeSpider.sourceLng === c.lng
    ) {
      closeSpider();
      return;
    }
    closeSpider();
    const fq = _fq();
    // Cap at SPIDER_CAP — painting more tiles than this makes the
    // spider an unreadable carpet AND eats RAM. Server's /in-cell
    // returns photos sorted by taken_at desc, so the cap yields the
    // most recent N.
    const url = `/api/photos/in-cell?lat=${c.lat}&lng=${c.lng}&zoom=${map.getZoom()}`
      + `&limit=${SPIDER_CAP}` + (fq ? "&" + fq : "");
    let arr = null;
    try {
      const r = await fetch(url);
      if (r.ok) arr = await r.json();
    } catch (_) { /* network */ }
    if (!arr || !arr.length) {
      openLightboxFromCell(c.lat, c.lng, map.getZoom(), c.sample_id);
      return;
    }
    if (arr.length === 1) {
      window.lightbox.openForPhotoId(arr[0].id);
      return;
    }

    const n = arr.length;
    // Tile is 40 px round; aim for ~52 px between centres so each
    // tile has ~12 px breathing room. Tighter makes photos hard to
    // read at a glance even at 1× scale.
    const centerPt = map.latLngToLayerPoint([c.lat, c.lng]);
    const layer = L.layerGroup();
    let positions;
    if (n <= 11) {
      // Ring with radius driven by circumference / pitch, floored at
      // 52 px so a 3-photo cluster doesn't collapse to the centre.
      const r = Math.max(52, (52 * n) / (2 * Math.PI));
      positions = [];
      for (let i = 0; i < n; i++) {
        const angle = (Math.PI * 2 * i) / n - Math.PI / 2;  // start at top
        positions.push(L.point(
          centerPt.x + Math.cos(angle) * r,
          centerPt.y + Math.sin(angle) * r,
        ));
      }
    } else {
      // True Archimedean spiral: r = startR + b·θ.
      // - cycleGap (= 2π·b) is radial distance between adjacent loops
      //   — ~one marker diameter so successive rings are visually one
      //   tile apart, no more, no less.
      // - tileSep is arc-length step between tiles. Because we advance
      //   angle by tileSep/r, chord distance between consecutive tiles
      //   stays roughly constant regardless of loop (no more "tight
      //   near the centre, sparse outside" problem).
      const startR = 52;
      const cycleGap = 48;
      const tileSep = 48;
      const b = cycleGap / (2 * Math.PI);
      positions = [];
      let angle = 0;
      for (let i = 0; i < n; i++) {
        const r = startR + b * angle;
        positions.push(L.point(
          centerPt.x + Math.cos(angle) * r,
          centerPt.y + Math.sin(angle) * r,
        ));
        angle += tileSep / r;
      }
    }

    // Hitbox/visual decoupling (see CSS): the inner visual layer is
    // pointer-events:none and scales on hover, while the outer 40×40
    // hitbox stays fixed. Cursor sliding past one marker into a
    // neighbour's hitbox switches hover targets naturally — no need
    // for the old "disable pointer-events on every sibling" hack.
    for (let i = 0; i < n; i++) {
      const pos = positions[i];
      const latlng = map.layerPointToLatLng(pos);
      const photo = arr[i];
      const icon = L.divIcon({
        html:
          `<div class="map-photo-marker map-photo-marker-spider" ` +
          `title="${escapeAttr(photo.filename || _t("map.view_photos", "사진 보기"))}">` +
          `<div class="map-photo-marker-visual">` +
          `<img src="/api/photos/${photo.id}/thumb?size=256" alt="">` +
          `</div></div>`,
        className: "map-photo-marker-wrap map-photo-marker-spider-wrap",
        iconSize: [40, 40],
        iconAnchor: [20, 20],
      });
      // pane="spiderPane" puts this marker in the z-index:700 pane
      // so it sits above the dim overlay (650) while regular markers
      // stay in the default 600 pane and get correctly muted.
      const mk = L.marker(latlng, { icon, pane: "spiderPane" });
      mk.on("click", (e) => {
        if (e.originalEvent) e.originalEvent.stopPropagation();
        window.lightbox.openForPhotoId(photo.id);
      });
      layer.addLayer(mk);
    }
    layer.addTo(map);
    // Body class drives the CSS filter that mutes the tile + marker
    // panes while leaving the spiderPane untouched — see
    // `body.spider-open .leaflet-...-pane` rules in the stylesheet.
    // Filter is applied per-pane rather than via an overlay div
    // because leaflet's map-pane transform creates a stacking
    // context that breaks overlay-based dimming.
    document.body.classList.add("spider-open");
    _activeSpider = { layer, sourceLat: c.lat, sourceLng: c.lng };
    // No once-listeners here — permanent zoomstart / map.click handlers
    // (set up in initMap) close the spider when the user navigates away.
  }

  async function openLightboxFromCell(lat, lng, zoom, sampleId, opts) {
    opts = opts || {};
    const fq = _fq();
    const limitParam = opts.limit ? `&limit=${opts.limit}` : "";
    const url = `/api/photos/in-cell?lat=${lat}&lng=${lng}&zoom=${zoom}${limitParam}`
      + (fq ? "&" + fq : "");
    let list = null;
    try {
      const r = await fetch(url);
      if (r.ok) {
        const arr = await r.json();
        if (arr.length) list = arr;
      }
    } catch (_) { /* network */ }
    if (!list) {
      // Fallback: lightbox on the sample photo alone.
      window.lightbox.openForPhotoId(sampleId);
      return;
    }
    let idx = list.findIndex(p => p.id === sampleId);
    if (idx < 0) idx = 0;
    window.lightbox.openWithList(list, idx, { fromMap: true });
  }

  // --- Public surface ---------------------------------------------
  function init(deps) {
    _deps = deps || {};

    // Close the side panel from the × button + from Escape (only when
    // the lightbox isn't on top — the lightbox owns Esc when open).
    const closeBtn = $("#map-side-panel-close");
    if (closeBtn) closeBtn.addEventListener("click", closeMapSidePanel);
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      // Cluster context menu close — fast path.
      if (_clusterMenuEl) { closeClusterMenu(); return; }
      const panel = $("#map-side-panel");
      if (!panel || !panel.classList.contains("open")) return;
      if (window.lightbox && window.lightbox.isOpen()) return;
      closeMapSidePanel();
    });

    // Popup helper exposed for any inline onclick in popup HTML.
    window.__mpOpenFromMap = function (id) {
      if (map) {
        try { map.closePopup(); } catch (_) { /* ignore */ }
      }
      window.lightbox.openForPhotoId(id);
    };
  }

  async function activate() {
    if (!map) {
      await initMap();
    }
    // Leaflet measures container size at construction; the #map div
    // was display:none before the tab flip, so we kick a re-measure
    // a tick after activate() so tiles cover the viewport. Also safe
    // on subsequent activates.
    setTimeout(() => { if (map) map.invalidateSize(); }, 50);
  }

  async function reload() {
    if (!map) return;     // map never built — nothing to reload
    await centerOnHotspotAndLoad();
  }

  window.mapView = {
    init,
    activate,
    applyTheme,
    reload,
    closeSidePanel: closeMapSidePanel,
  };
})();
