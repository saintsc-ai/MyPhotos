/*
 * MyPhotos shared bidirectional infinite-scroll factory.
 *
 * Per-page usage:
 *   <script src="/js/inf-scroll.js"></script>
 *   const inf = createInfScroll({ ... });
 *
 * Originally lived inline in admin.html. Extracted here (Phase 4a)
 * so the four admin panels (duplicates / trash / audit / shares)
 * AND any future caller can drop the same machinery in without
 * re-implementing.
 *
 * What this owns:
 *   - the page cursors (firstPage .. page)
 *   - the top + bottom IntersectionObservers
 *   - race-safety (an epoch counter discards results from a fetch
 *     that was in flight when jumpTo() was called, so a stale page
 *     can't overwrite the current cursor or splice into the wrong
 *     list)
 *   - synchronous scroll-anchor bookkeeping on prepend so the user's
 *     visible content doesn't jump as earlier rows land above it
 *
 * What the caller passes:
 *   pageSize           — items per fetch
 *   isActive           — () => bool, am I the visible tab?
 *   topSentinelId      — DOM id for the upward sentinel (or null)
 *   bottomSentinelId   — DOM id for the downward sentinel
 *   fetchPage          — async (pageNum) => { items: [], total: N }
 *   onAppend           — (items, isFirstEverLoad) => render at end
 *   onPrepend          — (items) => render at start
 *   onClear            — () => wipe the list DOM
 *   onAfterLoad?       — ({ direction, page, firstPage, total, items })
 *   onError?           — (err, direction) => void
 *
 * Why bidirectional: jumpToFrac drops the user mid-list. Without an
 * upward sentinel they're stuck — scrolling up does nothing, only
 * the bottom sentinel reels in more.
 */
(function () {
  "use strict";

  function createInfScroll({
    pageSize,
    isActive,
    topSentinelId,
    bottomSentinelId,
    fetchPage,
    onAppend,
    onPrepend,
    onClear,
    onAfterLoad,
    onError,
  }) {
    // Cursors describe the LOADED slice of the full list:
    //   firstPage .. page    — pages currently rendered (inclusive)
    // For a fresh-from-top load both equal 1 after the first fetch.
    // After jumpTo(N) both equal N until the user scrolls.
    let page = 0;
    let firstPage = 1;
    let total = 0;
    let loading = false;
    let topDone = true;        // initially: nothing above page 1 to load
    let botDone = false;
    let epoch = 0;             // bumped by jumpTo() / start() — stale
                               // in-flight fetches compare and bail
    let topObs = null;
    let botObs = null;
    let firstEverLoad = true;  // for `onAppend(items, true)` on page 1

    async function _doFetch(direction) {
      if (loading) return;
      if (direction === "down" && botDone) return;
      if (direction === "up" && topDone) return;
      const want = direction === "down" ? page + 1 : firstPage - 1;
      if (want < 1) { topDone = true; return; }
      loading = true;
      const myEpoch = epoch;
      let data;
      try {
        data = await fetchPage(want);
      } catch (e) {
        // Only release `loading` if we're STILL the current generation.
        // A stale fetch that returns after jumpTo() must not clear the
        // loading flag — the new generation's _doFetch owns it now.
        if (myEpoch === epoch) loading = false;
        if (onError) onError(e, direction);
        return;
      }
      if (myEpoch !== epoch) {
        // jumpTo() / start() ran while we were in flight — discard
        // results WITHOUT touching `loading`, for the same reason.
        return;
      }
      total = (data && data.total != null) ? data.total : total;
      const items = (data && data.items) || [];
      if (direction === "down") {
        page = want;
        onAppend(items, firstEverLoad);
        firstEverLoad = false;
        const maxPage = pageSize > 0 ? Math.max(1, Math.ceil(total / pageSize)) : 1;
        if (page >= maxPage || items.length === 0) botDone = true;
      } else {
        firstPage = want;
        // Scroll anchor: record height/scrollY BEFORE prepend, then
        // read scrollHeight AFTER (forces a synchronous reflow but
        // that's one cheap layout) and shift scrollY by the height
        // delta. Synchronous so the browser never paints the new
        // DOM with the old scrollY in between — no visual jump.
        // Modern browsers' built-in scroll-anchoring usually does
        // this automatically; our scrollTo lands at the same value
        // so it's a no-op there. Belt and braces.
        const se = document.scrollingElement || document.documentElement;
        const beforeH = se.scrollHeight;
        const beforeY = window.scrollY;
        onPrepend(items);
        const newH = se.scrollHeight;
        const delta = newH - beforeH;
        if (delta !== 0) {
          window.scrollTo({ top: beforeY + delta, behavior: "instant" });
        }
        if (firstPage <= 1 || items.length === 0) topDone = true;
      }
      loading = false;
      if (onAfterLoad) onAfterLoad({ direction, page, firstPage, total, items });
    }

    function _ensureObs() {
      const top = topSentinelId && document.getElementById(topSentinelId);
      const bot = bottomSentinelId && document.getElementById(bottomSentinelId);
      // The pattern: ALWAYS disconnect first inside the callback,
      // ALWAYS re-observe in `finally` (unless we've hit a done flag).
      // The re-observe is what re-arms the IO with a fresh initial-
      // state notification — without it, an early return on
      // botDone/loading would leave the observer "consumed" (it
      // already fired its initial callback and won't refire until
      // intersection state changes). For a post-jump load where the
      // sentinel is already in view AND stays in view (e.g. jump to
      // the LAST page → top sentinel never leaves the viewport),
      // that would mean the upward observer never fires again. So
      // disconnect+reobserve regardless of the loading check.
      if (top && !topObs) {
        topObs = new IntersectionObserver(async (entries) => {
          if (!entries.some(e => e.isIntersecting)) return;
          if (!isActive()) return;
          topObs.disconnect();
          try {
            if (topDone || loading) return;
            await _doFetch("up");
          } finally {
            if (!topDone) topObs.observe(top);
          }
        }, { rootMargin: "200px" });
      }
      if (bot && !botObs) {
        botObs = new IntersectionObserver(async (entries) => {
          if (!entries.some(e => e.isIntersecting)) return;
          if (!isActive()) return;
          botObs.disconnect();
          try {
            if (botDone || loading) return;
            await _doFetch("down");
          } finally {
            if (!botDone) botObs.observe(bot);
          }
        }, { rootMargin: "200px" });
      }
      // Always (re-)observe — observe() on an already-observed target
      // is a no-op per spec, so this is the simplest way to handle
      // both first-time setup AND re-arming after start()/jumpTo()
      // disconnected the observer.
      if (topObs && top && !topDone) topObs.observe(top);
      if (botObs && bot && !botDone) botObs.observe(bot);
    }

    // Public — fresh load from page 1. Used by initial panel load and
    // by reload buttons. Bumps epoch so any prior in-flight fetch is
    // discarded on return.
    async function start() {
      epoch++;
      if (topObs) topObs.disconnect();
      if (botObs) botObs.disconnect();
      page = 0; firstPage = 1; total = 0;
      topDone = true; botDone = false; loading = false; firstEverLoad = true;
      onClear();
      _ensureObs();
      await _doFetch("down");
    }

    // Public — jump to a specific page and reset the window around it.
    // After this, scrolling DOWN reels in page+1, scrolling UP reels in
    // page-1 (with scroll anchoring).
    async function jumpTo(targetPage) {
      if (targetPage < 1) targetPage = 1;
      epoch++;
      if (topObs) topObs.disconnect();
      if (botObs) botObs.disconnect();
      page = targetPage - 1;
      firstPage = targetPage;
      topDone = targetPage <= 1;
      botDone = false;
      loading = false;
      firstEverLoad = true;     // treat the post-jump first append as
                                // "first" so shell-building renderers
                                // (audit/shares) can rebuild as needed
      onClear();
      window.scrollTo({ top: 0, behavior: "instant" });
      _ensureObs();
      await _doFetch("down");
    }

    function jumpToFrac(frac) {
      if (total <= 0) return Promise.resolve();
      frac = Math.max(0, Math.min(1, frac));
      const off = Math.min(total - 1, Math.floor(frac * total));
      const tp = Math.floor(off / pageSize) + 1;
      return jumpTo(tp);
    }

    return {
      start, jumpTo, jumpToFrac,
      // Read-only accessors for status text / minimap hookups.
      getPage:       () => page,
      getFirstPage:  () => firstPage,
      getTotal:      () => total,
      getFirstOffset: () => (firstPage - 1) * pageSize,
      isLoading:     () => loading,
      isTopDone:     () => topDone,
      isBotDone:     () => botDone,
      // Force-mark bottom-done (used when the panel detects an empty
      // result and shows its own "empty" UI).
      markBotDone:   () => { botDone = true; if (botObs) botObs.disconnect(); },
      // Adjust total externally — used when the panel removes items
      // in place (e.g. trash restore/purge) and needs the minimap +
      // status text to reflect the new size without a full reload.
      setTotal:      (n) => { total = Math.max(0, n | 0); },
    };
  }

  window.createInfScroll = createInfScroll;
})();
