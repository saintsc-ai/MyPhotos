/*
 * MyPhotos right-rail scroll minimap factory.
 *
 * Per-page usage:
 *   <script src="/js/scroll-minimap.js"></script>
 *   const minimap = createScrollMinimap({ ... });
 *
 * Originally lived inline in admin.html. Extracted here (Phase 4a)
 * — generic, panel-agnostic factory. Each panel that opts in
 * (duplicates / audit / trash / shares on admin; potentially others
 * later) instantiates one with its own histogram endpoint +
 * jumpToFrac handler. Visibility is gated on `getActive()` so the
 * global mousemove / scroll / drag handlers can stay idle while
 * the user is on a non-minimap panel.
 *
 * Required DOM (per-instance):
 *   <div id="...-scroll-indicator">
 *     <div data-role="track">
 *       <div data-role="thumb"></div>
 *     </div>
 *     <div data-role="marks"></div>
 *     <div data-role="label"></div>
 *   </div>
 *
 * Config:
 *   indicatorId         — id of the wrapper div above
 *   histogramUrl?       — GET endpoint returning [{label|year, count}, ...]
 *   histogramProvider?  — async (qs) => same shape (preempts histogramUrl;
 *                          used by panels with client-side data)
 *   getActive           — () => bool (which tab am I?)
 *   jumpToFrac          — async (frac) => void (panel's page jumper)
 *   logicalInfoProvider?— () => { frac, visibleFrac } | null
 *                          See updateThumb's comments for what these
 *                          override and why.
 *
 * Returns: { loadHistogram, show, hide, updateThumb, renderMarks, pinAt }
 */
(function () {
  "use strict";

  function createScrollMinimap({
    indicatorId, histogramUrl, histogramProvider, getActive, jumpToFrac,
    logicalInfoProvider,
  }) {
    // `histogramProvider` (async () => [{label|year, count}]) takes
    // precedence over `histogramUrl` — used by panels whose data is
    // already client-side (e.g. shares) so we skip an extra fetch.
    const indicator = document.getElementById(indicatorId);
    if (!indicator) return null;
    const track  = indicator.querySelector('[data-role="track"]');
    const thumb  = indicator.querySelector('[data-role="thumb"]');
    const marks  = indicator.querySelector('[data-role="marks"]');
    const label  = indicator.querySelector('[data-role="label"]');

    let buckets = [];
    let totalCount = 0;
    let dragging = false;
    let labelHideTimer = null;
    let nearLeaveTimer = null;
    // jumpToFrac kicks off a load that resets the list (trashItems=[]
    // etc.) and scrolls back to top. That makes scrollY collapse to 0
    // so the default scrollY-driven thumb position would snap to the
    // top — visually losing the "I'm halfway through" hint the user
    // just expressed by dragging. Pinning the thumb at the requested
    // fraction holds it in place until the user scrolls deliberately.
    let pinFrac = null;
    // True for a short window after pinAt(): the programmatic
    // scrollTo({top:0}) inside jumpToFrac fires a scroll event that
    // would otherwise immediately clear the pin we just set.
    let pinHoldUntil = 0;

    async function loadHistogram(qs) {
      try {
        let data;
        if (typeof histogramProvider === "function") {
          data = await histogramProvider(qs);
        } else if (histogramUrl) {
          const url = histogramUrl + (qs ? `?${qs}` : "");
          const r = await fetch(url);
          if (!r.ok) return;
          data = await r.json();
        } else {
          return;
        }
        let off = 0;
        buckets = (Array.isArray(data) ? data : []).map(b => {
          const out = { ...b, startOffset: off };
          off += (b.count || 0);
          return out;
        });
        totalCount = off;
        renderMarks();
      } catch (_) { /* leave previous buckets in place */ }
    }

    function _labelFor(b) {
      if (b.year !== undefined && b.year !== null) return String(b.year);
      if (b.year === null) return "—";   // explicit no-date bucket
      if (b.label !== undefined && b.label !== null && String(b.label) !== "") {
        return String(b.label);
      }
      return "—";
    }

    function renderMarks() {
      if (!buckets.length || totalCount <= 0) {
        if (marks) marks.innerHTML = "";
        return;
      }
      const trackH = track.clientHeight;
      if (trackH <= 0) {
        requestAnimationFrame(renderMarks);
        return;
      }
      const maxCount = Math.max(...buckets.map(b => b.count || 0), 1);
      const MAX_BAR_PX = 28, MIN_BAR_PX = 3, MIN_SPACING_PX = 14;
      const parts = [];
      let lastTopPx = -Infinity;
      const n = buckets.length;
      for (let i = 0; i < n; i++) {
        const b = buckets[i];
        const midOffset = b.startOffset + (b.count || 0) / 2;
        const topPx = (midOffset / totalCount) * trackH;
        if (i !== 0 && i !== n - 1 && topPx - lastTopPx < MIN_SPACING_PX) continue;
        const barPx = Math.max(
          MIN_BAR_PX,
          Math.round(Math.sqrt((b.count || 0) / maxCount) * MAX_BAR_PX),
        );
        const txt = _labelFor(b);
        const offsetFrac = b.startOffset / totalCount;
        parts.push(
          `<div class="scroll-year-mark" style="top:${topPx}px" ` +
          `data-frac="${offsetFrac}" ` +
          `title="${escapeAttr(txt)} (${(b.count||0).toLocaleString()})">` +
            `<span class="scroll-year-bar" style="width:${barPx}px"></span>` +
            `<span>${escapeHtml(txt)}</span>` +
          `</div>`
        );
        lastTopPx = topPx;
      }
      marks.innerHTML = parts.join("");
      marks.querySelectorAll(".scroll-year-mark").forEach(el => {
        el.addEventListener("click", async (e) => {
          e.stopPropagation();
          const frac = parseFloat(el.dataset.frac);
          if (!isNaN(frac)) {
            pinAt(frac);
            if (jumpToFrac) await jumpToFrac(frac);
          }
          showBriefly();
        });
      });
    }

    function _bucketAtFrac(frac) {
      if (!buckets.length || totalCount <= 0) return null;
      const off = Math.floor(frac * totalCount);
      for (const b of buckets) {
        if (off < b.startOffset + (b.count || 0)) return b;
      }
      return buckets[buckets.length - 1];
    }

    function updateThumb() {
      if (!getActive()) return;
      const trackH = track.clientHeight;
      if (trackH <= 0) return;
      const se = document.scrollingElement || document.documentElement;
      const scrollH = se.scrollHeight;
      const viewportH = window.innerHeight;
      const range = Math.max(1, scrollH - viewportH);
      // Pin (set by jumpToFrac drag/click) wins for its 500ms
      // grace window; after that, prefer the panel's own logical
      // info when it can compute one (so the thumb reflects
      // "we're on item 25,700 of 25,963" even though only a
      // single page is rendered). Otherwise fall back to the
      // viewport-scroll-based estimate.
      let frac;
      let visibleFrac = viewportH / Math.max(viewportH, scrollH);
      if (typeof logicalInfoProvider === "function") {
        const info = logicalInfoProvider();
        if (info != null) {
          if (info.frac != null) {
            frac = Math.max(0, Math.min(1, info.frac));
          }
          if (info.visibleFrac != null) {
            visibleFrac = Math.max(0, Math.min(1, info.visibleFrac));
          }
        }
      }
      if (pinFrac !== null) frac = pinFrac;
      if (frac == null) frac = Math.min(1, window.scrollY / range);
      const thumbH = Math.max(24, Math.round(visibleFrac * trackH));
      const maxTop = Math.max(0, trackH - thumbH);
      const top = Math.min(maxTop, Math.round(frac * trackH));
      thumb.style.height = thumbH + "px";
      thumb.style.top = top + "px";
      // Floating label tracks the bucket the thumb is currently in.
      const b = _bucketAtFrac(frac);
      if (b) {
        label.textContent = _labelFor(b);
        const trackRect = track.getBoundingClientRect();
        label.style.top = (trackRect.top + top + thumbH / 2) + "px";
      }
    }

    function showBriefly() {
      indicator.classList.add("active");
      clearTimeout(labelHideTimer);
      labelHideTimer = setTimeout(() => {
        if (!dragging) indicator.classList.remove("active");
      }, 900);
    }

    // Scroll-driven thumb update — passive, cheap.
    window.addEventListener("scroll", () => {
      if (!getActive()) return;
      // Release the pin only if the user is scrolling deliberately
      // (not the programmatic scrollTo({top:0}) inside jumpToFrac).
      if (pinFrac !== null && Date.now() > pinHoldUntil) {
        pinFrac = null;
      }
      updateThumb();
      showBriefly();
    }, { passive: true });

    // Called by jumpToFrac after the target page has been requested.
    // Pins the thumb at frac so the user sees their drag stick even
    // though only one page is actually rendered.
    function pinAt(frac) {
      pinFrac = Math.max(0, Math.min(1, frac));
      pinHoldUntil = Date.now() + 500;   // ride out the scrollTo
      updateThumb();
    }

    // Near-edge reveal (mouse only).
    const NEAR_PX = 120;
    window.addEventListener("mousemove", (e) => {
      if (!getActive()) return;
      const near = e.clientX > window.innerWidth - NEAR_PX;
      if (near) {
        clearTimeout(nearLeaveTimer);
        nearLeaveTimer = null;
        indicator.classList.add("near");
      } else if (indicator.classList.contains("near") && !nearLeaveTimer) {
        nearLeaveTimer = setTimeout(() => {
          nearLeaveTimer = null;
          if (!dragging) indicator.classList.remove("near");
        }, 350);
      }
    }, { passive: true });

    // Compute a fraction (0..1) for a viewport-Y clicked / dragged
    // inside the track.
    function _fracFromY(clientY) {
      const r = track.getBoundingClientRect();
      const trackH = r.height;
      if (trackH <= 0) return 0;
      const y = Math.max(0, Math.min(trackH, clientY - r.top));
      return y / trackH;
    }
    // Visually move the thumb to a fraction without triggering a
    // page jump — used during drag so the thumb tracks the cursor
    // but we don't fire a network request on every mousemove.
    function _previewFrac(frac) {
      pinAt(frac);
    }
    // Click anywhere on the track (but not on the thumb or a label)
    // → one-shot jump.
    track.addEventListener("click", (e) => {
      if (e.target.closest(".scroll-year-mark")) return;
      if (e.target.closest(".scroll-thumb")) return;
      const frac = _fracFromY(e.clientY);
      pinAt(frac);
      if (jumpToFrac) jumpToFrac(frac);
      showBriefly();
    });
    // Drag-to-scrub. While dragging, just move the thumb; only fire
    // the actual page jump on release so we don't hammer the server
    // with a fetch per mousemove pixel. Mouse and touch share the
    // same begin/move/end via _startDrag so they stay in lockstep.
    function _startDrag() {
      dragging = true;
      indicator.classList.add("dragging");
      thumb.classList.add("dragging");
      let lastFrac = pinFrac;
      const onMouseMove = (ev) => {
        lastFrac = _fracFromY(ev.clientY);
        _previewFrac(lastFrac);
      };
      const onTouchMove = (ev) => {
        const t = ev.touches[0]; if (!t) return;
        lastFrac = _fracFromY(t.clientY);
        _previewFrac(lastFrac);
        ev.preventDefault();   // suppress page scroll while scrubbing
      };
      const cleanup = () => {
        dragging = false;
        indicator.classList.remove("dragging");
        thumb.classList.remove("dragging");
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
        document.removeEventListener("touchmove", onTouchMove);
        document.removeEventListener("touchend", onTouchEnd);
        document.removeEventListener("touchcancel", onTouchEnd);
        if (lastFrac !== null && jumpToFrac) jumpToFrac(lastFrac);
      };
      const onMouseUp = () => cleanup();
      const onTouchEnd = () => cleanup();
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
      document.addEventListener("touchmove", onTouchMove, { passive: false });
      document.addEventListener("touchend", onTouchEnd);
      document.addEventListener("touchcancel", onTouchEnd);
    }
    thumb.addEventListener("mousedown", (e) => {
      e.preventDefault();
      _startDrag();
    });
    thumb.addEventListener("touchstart", (e) => {
      const t = e.touches[0]; if (!t) return;
      // Seed lastFrac so a touch-then-release without movement
      // still jumps to wherever the thumb already sits.
      pinAt(_fracFromY(t.clientY));
      _startDrag();
      e.preventDefault();
    }, { passive: false });

    function show() {
      indicator.classList.remove("hidden");
      // Two raf ticks: first lets layout settle (panel just became
      // .active), second lets the track's clientHeight be real.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        renderMarks();
        updateThumb();
      }));
    }
    function hide() { indicator.classList.add("hidden"); }

    return { loadHistogram, show, hide, updateThumb, renderMarks, pinAt };
  }

  window.createScrollMinimap = createScrollMinimap;
})();
