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

  // --- Centered in-app dialogs ------------------------------------------
  // Native alert()/confirm()/prompt() are pinned by the browser to the top
  // of the window (under the address bar) and can't be repositioned. These
  // helpers render the same prompts as a centered modal instead.
  //   uiAlert(msg)                       -> Promise<void>
  //   uiConfirm(msg, {danger,okLabel..}) -> Promise<boolean>
  //   uiPrompt(msg, defaultValue)        -> Promise<string|null>
  // uiConfirm/uiPrompt are async, so call sites use `await`. window.alert
  // is overridden to re-center every existing alert() call for free;
  // confirm()/prompt() can't be transparently overridden (they're
  // synchronous), so those sites are converted to await individually.
  const _DLG_STYLE_ID = "ui-dialog-style";
  function _ensureDialogStyle() {
    if (document.getElementById(_DLG_STYLE_ID)) return;
    const el = document.createElement("style");
    el.id = _DLG_STYLE_ID;
    el.textContent = [
      ".ui-dialog-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.6);",
      "display:flex;align-items:center;justify-content:center;z-index:100000;padding:16px;}",
      ".ui-dialog-box{background:#1f1f1f;color:#eee;border:1px solid #333;border-radius:10px;",
      "width:100%;max-width:380px;padding:20px 22px 16px;box-shadow:0 10px 40px rgba(0,0,0,0.5);font-size:14px;",
      "font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,sans-serif;}",
      ".ui-dialog-msg{white-space:pre-wrap;line-height:1.5;margin-bottom:14px;word-break:break-word;}",
      ".ui-dialog-input{width:100%;box-sizing:border-box;padding:8px 10px;margin-bottom:14px;",
      "background:#111;color:#eee;border:1px solid #3a3a3a;border-radius:6px;font-size:14px;}",
      ".ui-dialog-chips{display:flex;flex-wrap:wrap;gap:6px;margin:-6px 0 14px;}",
      ".ui-dialog-chip{padding:4px 10px;background:#262a30;color:#dadde2;border:1px solid #3a4150;",
      "border-radius:14px;font-size:12px;cursor:pointer;line-height:1.3;max-width:160px;",
      "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}",
      ".ui-dialog-chip:hover{background:#323843;color:#fff;border-color:#536179;}",
      ".ui-dialog-chips-label{font-size:11px;color:#8a8f99;width:100%;margin-bottom:2px;}",
      "body.light .ui-dialog-chip{background:#eef0f3;color:#1a1a1a;border-color:#c8ccd1;}",
      "body.light .ui-dialog-chip:hover{background:#e0e3e8;border-color:#9aa1ad;}",
      "body.light .ui-dialog-chips-label{color:#5a6068;}",
      ".ui-dialog-row{display:flex;justify-content:flex-end;gap:8px;}",
      ".ui-dialog-btn{padding:7px 16px;border-radius:6px;border:1px solid #3a3a3a;background:#2a2a2a;",
      "color:#eee;cursor:pointer;font-size:13px;}",
      ".ui-dialog-btn:hover{background:#343434;}",
      ".ui-dialog-btn.primary{background:#5967ff;border-color:#5967ff;color:#fff;}",
      ".ui-dialog-btn.primary:hover{background:#6f7bff;}",
      ".ui-dialog-btn.danger{background:#e42b2e;border-color:#e42b2e;color:#fff;}",
      ".ui-dialog-btn.danger:hover{background:#ef4043;}",
      "body.light .ui-dialog-box{background:#fff;color:#1a1a1a;border-color:#d6d8db;}",
      "body.light .ui-dialog-input{background:#fff;color:#1a1a1a;border-color:#c8ccd1;}",
      "body.light .ui-dialog-btn{background:#eceef1;color:#1a1a1a;border-color:#d0d3d8;}",
      "body.light .ui-dialog-btn:hover{background:#e0e3e8;}",
      "body.light .ui-dialog-btn.primary{background:#323f53;border-color:#323f53;color:#fff;}",
      "body.light .ui-dialog-btn.primary:hover{background:#3d4d66;}",
      "body.light .ui-dialog-btn.danger{background:#e42b2e;border-color:#e42b2e;color:#fff;}",
    ].join("");
    (document.head || document.documentElement).appendChild(el);
  }

  // kind: "alert" | "confirm" | "prompt"
  function _openDialog(kind, message, opts) {
    opts = opts || {};
    _ensureDialogStyle();
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "ui-dialog-overlay";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      const box = document.createElement("div");
      box.className = "ui-dialog-box";
      const msg = document.createElement("div");
      msg.className = "ui-dialog-msg";
      msg.textContent = message == null ? "" : String(message);
      box.appendChild(msg);

      let input = null;
      if (kind === "prompt") {
        input = document.createElement("input");
        input.className = "ui-dialog-input";
        input.type = "text";
        if (opts.defaultValue != null) input.value = String(opts.defaultValue);
        box.appendChild(input);
        // Optional chip row for recent / suggested values. Click =
        // fill the input and submit immediately (the user explicitly
        // picked, so don't make them press Enter too).
        const sugg = Array.isArray(opts.suggestions)
          ? opts.suggestions.filter((s) => s != null && String(s).trim())
          : [];
        if (sugg.length) {
          const wrap = document.createElement("div");
          wrap.className = "ui-dialog-chips";
          if (opts.suggestionsLabel) {
            const lbl = document.createElement("div");
            lbl.className = "ui-dialog-chips-label";
            lbl.textContent = opts.suggestionsLabel;
            wrap.appendChild(lbl);
          }
          sugg.forEach((s) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "ui-dialog-chip";
            chip.textContent = String(s);
            chip.title = String(s);
            chip.addEventListener("click", () => {
              input.value = String(s);
              onOk();
            });
            wrap.appendChild(chip);
          });
          box.appendChild(wrap);
        }
      }

      const row = document.createElement("div");
      row.className = "ui-dialog-row";
      let cancelBtn = null;
      if (kind !== "alert") {
        cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "ui-dialog-btn";
        cancelBtn.textContent = opts.cancelLabel || window._t("common.cancel", "취소");
        row.appendChild(cancelBtn);
      }
      const okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "ui-dialog-btn " + (opts.danger ? "danger" : "primary");
      okBtn.textContent = opts.okLabel || window._t("common.ok", "확인");
      row.appendChild(okBtn);
      box.appendChild(row);
      overlay.appendChild(box);
      document.body.appendChild(overlay);

      function cleanup() {
        document.removeEventListener("keydown", onKey, true);
        overlay.remove();
      }
      function done(val) { cleanup(); resolve(val); }
      function onOk() {
        done(kind === "confirm" ? true : kind === "prompt" ? (input ? input.value : "") : undefined);
      }
      function onCancel() {
        done(kind === "confirm" ? false : kind === "prompt" ? null : undefined);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel(); }
        else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); onOk(); }
      }
      okBtn.addEventListener("click", onOk);
      if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
      overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) onCancel(); });
      document.addEventListener("keydown", onKey, true);
      (input || okBtn).focus();
    });
  }

  window.uiAlert = (message, opts) => _openDialog("alert", message, opts);
  window.uiConfirm = (message, opts) => _openDialog("confirm", message, opts);
  window.uiPrompt = (message, defaultValue, opts) =>
    _openDialog("prompt", message, Object.assign({ defaultValue: defaultValue }, opts || {}));

  // Re-center every native alert() with no call-site changes. Non-blocking
  // (returns a Promise), which is safe here: no alert() in this app is
  // immediately followed by navigation/reload that relied on it blocking.
  window.alert = function (message) { window.uiAlert(message); };
})();
