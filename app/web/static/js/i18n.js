/*
 * MyPhotos translation runtime.
 *
 * Per-page usage:
 *   <script src="/js/i18n.js"></script>
 *   await i18n.init(serverDefault);   // serverDefault from /api/admin/settings or null
 *   i18n.t("tabs.roots")               -> "사진 폴더" / "Photo folders" / ...
 *   i18n.applyTranslations()           -> rewrite every [data-i18n] / [data-i18n-title]
 *                                         / [data-i18n-placeholder] in the DOM
 *
 * Language resolution priority:
 *   1. localStorage["myphotos-lang"] (the user's pick, sticky across reloads)
 *   2. serverDefault   (admin's `app.default_language`)
 *   3. navigator.languages -> first SUPPORTED match
 *   4. "ko"            (original-source language)
 *
 * Missing-key behaviour: t("foo.bar") returns "foo.bar" when the key isn't
 * in the loaded catalog. That's loud on purpose — typos surface as visible
 * strings instead of silently disappearing.
 */
(function () {
  "use strict";

  // Order here is the order the language picker renders.
  const SUPPORTED = [
    { code: "ko",    name: "한국어" },
    { code: "en",    name: "English" },
    { code: "ja",    name: "日本語" },
    { code: "zh-CN", name: "中文（简体）" },
    { code: "zh-TW", name: "中文（繁體）" },
    { code: "fr",    name: "Français" },
    { code: "de",    name: "Deutsch" },
    { code: "es",    name: "Español" },
    { code: "ru",    name: "Русский" },
    { code: "pt",    name: "Português" },
  ];
  const LS_KEY = "myphotos-lang";

  let _catalog = {};
  let _currentLang = "ko";
  // Fired after every successful loadCatalog so panel-specific code can
  // refresh anything it already painted with the previous catalog.
  const _changeListeners = new Set();

  function _lookup(key) {
    let cur = _catalog;
    if (!cur || typeof cur !== "object") return null;
    for (const seg of key.split(".")) {
      if (cur && typeof cur === "object" && seg in cur) {
        cur = cur[seg];
      } else {
        return null;
      }
    }
    return typeof cur === "string" ? cur : null;
  }

  function t(key, params) {
    let s = _lookup(key);
    if (s === null) return key;
    if (params) {
      for (const k of Object.keys(params)) {
        // Simple {name} interpolation. Escape the brace so multiple
        // matches of the same key all get replaced.
        const re = new RegExp("\\{" + k.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&") + "\\}", "g");
        s = s.replace(re, String(params[k]));
      }
    }
    return s;
  }

  function applyTranslations(scope) {
    const root = scope || document;
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const s = _lookup(el.getAttribute("data-i18n"));
      if (s !== null) el.textContent = s;
    });
    root.querySelectorAll("[data-i18n-html]").forEach((el) => {
      // For labels that intentionally contain markup (e.g. <b>, <code>).
      // Caller is responsible for the catalog value being trusted.
      const s = _lookup(el.getAttribute("data-i18n-html"));
      if (s !== null) el.innerHTML = s;
    });
    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const s = _lookup(el.getAttribute("data-i18n-title"));
      if (s !== null) el.title = s;
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const s = _lookup(el.getAttribute("data-i18n-placeholder"));
      if (s !== null) el.placeholder = s;
    });
    root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      const s = _lookup(el.getAttribute("data-i18n-aria"));
      if (s !== null) el.setAttribute("aria-label", s);
    });
  }

  async function loadCatalog(lang) {
    // Pure catalog load — does NOT touch localStorage. Used by both
    // bootstrap (which shouldn't pretend the user picked anything) and
    // setUserLang() (which writes the marker separately). That split
    // keeps "did the user explicitly pick a language" as a clean signal
    // for honouring the admin-configured default on first visit.
    if (!SUPPORTED.some((s) => s.code === lang)) {
      console.warn("i18n: unsupported lang, falling back to ko:", lang);
      lang = "ko";
    }
    try {
      const r = await fetch(`/i18n/${lang}.json`, { cache: "no-store" });
      if (!r.ok) throw new Error(`catalog fetch ${r.status}`);
      _catalog = await r.json();
      _currentLang = lang;
      try { document.documentElement.lang = lang; } catch (_) {}
      applyTranslations();
      for (const fn of _changeListeners) {
        try { fn(lang); } catch (e) { console.error("i18n listener:", e); }
      }
    } catch (e) {
      console.warn("i18n: failed to load", lang, e);
    }
  }

  // Persist the user's explicit pick + activate it. localStorage is
  // ONLY written here — bootstrap uses loadCatalog directly so it can
  // still distinguish "user actually chose this" from "we defaulted".
  async function setUserLang(lang) {
    try { localStorage.setItem(LS_KEY, lang); } catch (_) {}
    await loadCatalog(lang);
  }

  function getUserPick() {
    try { return localStorage.getItem(LS_KEY) || null; }
    catch (_) { return null; }
  }

  function _resolveBrowserLang() {
    const langs = navigator.languages || [navigator.language || ""];
    for (const raw of langs) {
      if (!raw) continue;
      // Exact match first ("zh-CN" hits "zh-CN" before falling back to base "zh").
      if (SUPPORTED.some((s) => s.code === raw)) return raw;
      const base = raw.split("-")[0].toLowerCase();
      const hit = SUPPORTED.find((s) =>
        s.code.toLowerCase() === base ||
        s.code.toLowerCase().split("-")[0] === base
      );
      if (hit) return hit.code;
    }
    return null;
  }

  async function init(serverDefault) {
    const pick = getUserPick();
    let lang;
    if (pick && SUPPORTED.some((s) => s.code === pick)) {
      lang = pick;
    } else {
      lang = serverDefault || _resolveBrowserLang() || "ko";
    }
    await loadCatalog(lang);
  }

  function onChange(fn) { _changeListeners.add(fn); }
  function offChange(fn) { _changeListeners.delete(fn); }
  function getCurrentLang() { return _currentLang; }
  function getSupportedLangs() { return SUPPORTED.slice(); }

  window.i18n = {
    t, applyTranslations, loadCatalog, setUserLang, getUserPick, init,
    onChange, offChange,
    getCurrentLang, getSupportedLangs,
  };
})();
