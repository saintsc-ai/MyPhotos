/*
 * MyPhotos shared front-end helpers.
 *
 * Per-page usage:
 *   <script src="/js/common.js"></script>   <!-- AFTER /js/i18n.js -->
 *
 * Exposes globals consumed by both index.html and admin.html:
 *   $(sel)                    -> document.querySelector shortcut
 *   escapeHtml(s)             -> "<a>&" → "&lt;a&gt;&amp;"  (null-safe)
 *   escapeAttr(s)             -> same as escapeHtml; kept as a separate
 *                                name because the call sites read better
 *                                (`title="${escapeAttr(...)}"` vs html
 *                                injection inside an element body)
 *   _t(key, fallback)         -> i18n lookup, returns fallback Korean
 *                                when i18n hasn't loaded yet or key is
 *                                missing
 *   _tn(key, fallback, params)-> _t + simple {name} interpolation
 *
 * NOT included (intentionally panel-specific, divergence is real):
 *   fmtBytes / fmtTime / fmtDate  -> admin wants timezone-aware
 *                                    full timestamps + "—" for missing;
 *                                    gallery wants raw ISO slices.
 *                                    Don't force a single shape.
 *
 *   friendlyError / api wrappers  -> only index.html has friendlyError
 *                                    today, and unifying fetch patterns
 *                                    is a bigger Phase-3b refactor.
 */
(function () {
  "use strict";

  // querySelector shortcut. Used everywhere — short, ergonomic, and
  // intentionally NOT a jQuery-style wrapper. Returns the raw element
  // (or null) so callers can `.addEventListener`, `.textContent = ...`
  // directly.
  window.$ = (sel) => document.querySelector(sel);

  // HTML-safe escaping. `s ?? ""` swallows undefined / null without
  // emitting the literal strings "undefined" / "null" inside markup —
  // a class of XSS-ish bug we hit a few times before adding the
  // coalesce.
  function _escape(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  window.escapeHtml = _escape;
  window.escapeAttr = _escape;

  // i18n lookups — defer to i18n.js when it has loaded. Both helpers
  // RETURN the fallback Korean when:
  //   - window.i18n / i18n.t doesn't exist (script-load race)
  //   - i18n.t returns the key unchanged (key missing from catalog)
  // That second case is i18n.js's "loud" missing-key behaviour; here
  // we silence it for JS-built strings since we always have the
  // original Korean handy as `fallback`.
  window._t = function (key, fallback) {
    if (window.i18n && i18n.t) {
      const v = i18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback;
  };
  window._tn = function (key, fallback, params) {
    let s = window._t(key, fallback);
    if (params) {
      for (const k of Object.keys(params)) {
        s = s.replace(new RegExp("\\{" + k + "\\}", "g"), String(params[k]));
      }
    }
    return s;
  };
})();
