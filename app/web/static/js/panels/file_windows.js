/*
 * File explorer windows — multiple floating, movable, resizable explorer
 * windows (Windows-Explorer style) with drag-and-drop copy/move between them.
 *
 * Window chrome: WinBox.js (vendored, Apache-2.0) — draggable / resizable /
 * focus / min-max / close. We render our own explorer (root switcher +
 * breadcrumb + file list) into each window body and reuse the /api/files API.
 *
 * DnD: list rows are draggable; dropping a selection onto another window (or a
 * folder row) opens a 복사 / 이동 / 취소 menu, then calls /api/files/copy or
 * /api/files/move. Copy source may be read-only; move needs writable source +
 * dest. Cross-root transfers allowed.
 *
 * IIFE exposing window.fileWindows = { open, count }.
 */
(function () {
  "use strict";

  const _t = (k, f) => (window._t ? window._t(k, f) : f);
  const DT_MIME = "application/x-myphotos-files";
  let _seq = 0;
  const _wins = new Map();   // id -> win object (for cross-window refresh)

  // ---- small helpers (kept local; files.js keeps its own copies) --------
  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1).replace(/\.0$/, "") + " MB";
    return (n / 1073741824).toFixed(1).replace(/\.0$/, "") + " GB";
  }
  function fmtDate(iso) { return iso ? String(iso).slice(0, 10) : ""; }
  function iconFor(ext, mime) {
    const e = (ext || "").toLowerCase();
    if (e === "pdf") return "📕";
    if (["hwp", "hwpx"].includes(e)) return "📘";
    if (["doc", "docx", "rtf", "odt"].includes(e)) return "📄";
    if (["xls", "xlsx", "csv", "ods"].includes(e)) return "📊";
    if (["ppt", "pptx", "odp"].includes(e)) return "📑";
    if (["txt", "md", "log"].includes(e)) return "📃";
    if (["zip", "tar", "gz", "7z", "rar"].includes(e)) return "🗜";
    if ((mime || "").startsWith("image/")) return "🖼";
    if ((mime || "").startsWith("audio/")) return "🎵";
    if ((mime || "").startsWith("video/")) return "🎬";
    return "📄";
  }
  const esc = (s) => (window.escapeHtml ? window.escapeHtml(s) : String(s == null ? "" : s));
  const escA = (s) => (window.escapeAttr ? window.escapeAttr(s) : esc(s));

  // ---- drop action menu (복사 / 이동 / 취소) ----------------------------
  function _dropMenu(x, y, onPick) {
    const old = document.getElementById("fw-drop-menu");
    if (old) old.remove();
    const el = document.createElement("div");
    el.id = "fw-drop-menu";
    el.className = "folder-menu";
    el.style.cssText = "position:fixed;z-index:2147483647;left:" + x + "px;top:" + y + "px";
    el.innerHTML =
      `<button type="button" class="folder-menu-item" data-act="copy">📋 ${esc(_t("fw.copy", "복사"))}</button>` +
      `<button type="button" class="folder-menu-item" data-act="move">✂️ ${esc(_t("fw.move", "이동"))}</button>` +
      `<div class="folder-menu-sep"></div>` +
      `<button type="button" class="folder-menu-item" data-act="cancel">${esc(_t("common.cancel", "취소"))}</button>`;
    document.body.appendChild(el);
    // keep on-screen
    const r = el.getBoundingClientRect();
    if (r.right > innerWidth) el.style.left = (innerWidth - r.width - 6) + "px";
    if (r.bottom > innerHeight) el.style.top = (innerHeight - r.height - 6) + "px";
    const close = () => { el.remove(); document.removeEventListener("mousedown", onDoc, true); };
    const onDoc = (ev) => { if (!el.contains(ev.target)) close(); };
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
    el.querySelectorAll(".folder-menu-item").forEach(b =>
      b.addEventListener("click", () => {
        const act = b.getAttribute("data-act");
        close();
        if (act === "copy" || act === "move") onPick(act);
      }));
  }

  // Refresh any open window currently showing (rootId, path).
  function _refreshMatching(rootId, path) {
    for (const w of _wins.values()) {
      if (w.rootId === rootId && (w.path || "") === (path || "")) w.load();
    }
  }

  async function _doTransfer(action, fileIds, srcRootId, destRootId, destPath, srcPath) {
    try {
      const r = await fetch("/api/files/" + (action === "move" ? "move" : "copy"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: fileIds, dest_root_id: destRootId, dest_path: destPath }),
      });
      if (!r.ok) {
        let d = "HTTP " + r.status;
        try { d = (await r.json()).detail || d; } catch (_) {}
        window.uiAlert(d);
        return;
      }
      const data = await r.json().catch(() => ({}));
      const n = action === "move" ? (data.moved || 0) : (data.copied || 0);
      const skipped = data.skipped || 0;
      if (!n && skipped) {
        window.uiAlert(_t("fw.all_skipped", "옮길 수 있는 항목이 없습니다 (읽기전용/중복/동일 위치)."));
      }
      // refresh dest + (for move) source folders in every matching window
      _refreshMatching(destRootId, destPath);
      if (action === "move") _refreshMatching(srcRootId, srcPath);
    } catch (e) {
      window.uiAlert(e.message);
    }
  }

  // ---- one explorer window ---------------------------------------------
  function open(rootId, path, roots) {
    if (!window.WinBox) { window.uiAlert("WinBox 미로딩"); return; }
    const id = ++_seq;
    const win = { id, rootId: rootId || null, path: path || "", roots: roots || [], selAnchor: null };

    const body = document.createElement("div");
    body.className = "fw-body";
    body.innerHTML =
      `<div class="fw-bar">
         <select class="fw-root"></select>
         <button class="fw-up" title="${escA(_t("fw.up", "상위 폴더"))}">⬆</button>
         <div class="fw-crumbs"></div>
       </div>
       <div class="fw-list fx-list"></div>`;
    const rootSel = body.querySelector(".fw-root");
    const upBtn = body.querySelector(".fw-up");
    const crumbs = body.querySelector(".fw-crumbs");
    const list = body.querySelector(".fw-list");
    win.list = list;

    async function loadRoots() {
      if (win.roots.length) return win.roots;
      try { win.roots = await window.api.get("/api/files/roots"); } catch (_) { win.roots = []; }
      return win.roots;
    }
    function renderRootSel() {
      rootSel.innerHTML = win.roots.map(r =>
        `<option value="${r.id}"${r.id === win.rootId ? " selected" : ""}>${esc(r.label)}${r.readonly ? " 🔒" : ""}</option>`).join("");
    }
    function renderCrumbs() {
      const root = win.roots.find(r => r.id === win.rootId);
      const parts = win.path ? win.path.split("/") : [];
      let acc = "";
      const items = [`<a href="#" data-p="">${esc(root ? root.label : "?")}</a>`];
      for (const seg of parts) { acc = acc ? acc + "/" + seg : seg; items.push(`<span>›</span><a href="#" data-p="${escA(acc)}">${esc(seg)}</a>`); }
      crumbs.innerHTML = items.join(" ");
      crumbs.querySelectorAll("a").forEach(a =>
        a.addEventListener("click", (e) => { e.preventDefault(); nav(win.rootId, a.getAttribute("data-p")); }));
    }

    async function load() {
      await loadRoots();
      if (win.rootId == null && win.roots.length) win.rootId = win.roots[0].id;
      renderRootSel();
      win.selAnchor = null;
      list.innerHTML = `<div class="fx-loading">${esc(_t("common.loading", "불러오는 중…"))}</div>`;
      renderCrumbs();
      let data;
      try {
        data = await window.api.get("/api/files/list?root_id=" + encodeURIComponent(win.rootId) +
          "&path=" + encodeURIComponent(win.path || ""));
      } catch (e) { list.innerHTML = `<div class="fx-loading">${esc(_t("files.load_fail", "불러오기 실패"))}</div>`; return; }
      win.data = data;
      const rows = [`<div class="fx-row fx-head"><div class="fx-cell">${esc(_t("files.col_name", "이름"))}</div>` +
        `<div class="fx-cell fx-num">${esc(_t("files.col_size", "크기"))}</div>` +
        `<div class="fx-cell">${esc(_t("files.col_mtime", "수정일"))}</div></div>`];
      if (win.path) {
        const parent = win.path.includes("/") ? win.path.slice(0, win.path.lastIndexOf("/")) : "";
        rows.push(`<div class="fx-row fx-folder fw-drop-folder fx-up" data-path="${escA(parent)}"><div class="fx-cell">📁 ..</div><div class="fx-cell"></div><div class="fx-cell"></div></div>`);
      }
      for (const sub of data.folders) {
        const cp = win.path ? win.path + "/" + sub : sub;
        rows.push(`<div class="fx-row fx-folder fw-drop-folder" data-path="${escA(cp)}"><div class="fx-cell">📁 ${esc(sub)}</div><div class="fx-cell fx-num"></div><div class="fx-cell"></div></div>`);
      }
      for (const f of data.files) {
        rows.push(`<div class="fx-row fx-file" draggable="true" data-id="${f.id}"><div class="fx-cell">${iconFor(f.ext, f.mime)} ${esc(f.filename)}</div>` +
          `<div class="fx-cell fx-num">${esc(fmtSize(f.size))}</div><div class="fx-cell">${esc(fmtDate(f.mtime))}</div></div>`);
      }
      if (!data.folders.length && !data.files.length) rows.push(`<div class="fx-empty">${esc(_t("files.empty_folder", "빈 폴더"))}</div>`);
      list.innerHTML = rows.join("");
    }
    win.load = load;

    function nav(rid, p) { win.rootId = rid; win.path = p || ""; load(); }

    function selectedIds() {
      return [...list.querySelectorAll(".fx-file.sel")].map(r => parseInt(r.dataset.id, 10));
    }

    // ---- events -----------------------------------------------------
    rootSel.addEventListener("change", () => nav(parseInt(rootSel.value, 10), ""));
    upBtn.addEventListener("click", () => {
      if (!win.path) return;
      nav(win.rootId, win.path.includes("/") ? win.path.slice(0, win.path.lastIndexOf("/")) : "");
    });
    list.addEventListener("click", (e) => {
      const row = e.target.closest(".fx-row");
      if (!row || row.classList.contains("fx-head")) return;
      if (!row.classList.contains("fx-file")) return;   // folders handled on dblclick
      const rows = [...list.querySelectorAll(".fx-file")];
      if (e.shiftKey && win.selAnchor && rows.includes(win.selAnchor)) {
        const a = rows.indexOf(win.selAnchor), b = rows.indexOf(row);
        const lo = Math.min(a, b), hi = Math.max(a, b);
        rows.forEach(x => x.classList.remove("sel"));
        for (let i = lo; i <= hi; i++) rows[i].classList.add("sel");
      } else if (e.ctrlKey || e.metaKey) {
        row.classList.toggle("sel"); win.selAnchor = row;
      } else {
        rows.forEach(x => x.classList.remove("sel")); row.classList.add("sel"); win.selAnchor = row;
      }
    });
    list.addEventListener("dblclick", (e) => {
      const row = e.target.closest(".fx-row.fx-folder");
      if (row) nav(win.rootId, row.dataset.path);
    });

    // Drag source
    list.addEventListener("dragstart", (e) => {
      const row = e.target.closest(".fx-file");
      if (!row) return;
      if (!row.classList.contains("sel")) {
        list.querySelectorAll(".fx-file.sel").forEach(x => x.classList.remove("sel"));
        row.classList.add("sel"); win.selAnchor = row;
      }
      const ids = selectedIds();
      const payload = JSON.stringify({ winId: win.id, rootId: win.rootId, path: win.path, ids });
      e.dataTransfer.setData(DT_MIME, payload);
      e.dataTransfer.setData("text/plain", payload);
      e.dataTransfer.effectAllowed = "copyMove";
    });

    // Drop target — the whole list (drop into current folder) or a folder row.
    function _payload(e) {
      let raw = e.dataTransfer.getData(DT_MIME) || e.dataTransfer.getData("text/plain");
      if (!raw) return null;
      try { const p = JSON.parse(raw); return (p && Array.isArray(p.ids)) ? p : null; } catch (_) { return null; }
    }
    list.addEventListener("dragover", (e) => {
      if ([...e.dataTransfer.types].includes(DT_MIME) || [...e.dataTransfer.types].includes("text/plain")) {
        e.preventDefault(); e.dataTransfer.dropEffect = "copy";
        const fld = e.target.closest(".fw-drop-folder");
        list.querySelectorAll(".fw-drag-over").forEach(x => x.classList.remove("fw-drag-over"));
        (fld || list).classList.add("fw-drag-over");
      }
    });
    list.addEventListener("dragleave", (e) => {
      if (!list.contains(e.relatedTarget)) list.classList.remove("fw-drag-over");
    });
    list.addEventListener("drop", (e) => {
      e.preventDefault();
      list.querySelectorAll(".fw-drag-over").forEach(x => x.classList.remove("fw-drag-over"));
      list.classList.remove("fw-drag-over");
      const p = _payload(e);
      if (!p || !p.ids.length) return;
      const fld = e.target.closest(".fw-drop-folder");
      const destPath = fld ? fld.dataset.path : win.path;
      // no-op: same source folder & same root, dropped on current folder
      if (p.rootId === win.rootId && (p.path || "") === (destPath || "") && !fld) return;
      _dropMenu(e.clientX, e.clientY, (act) =>
        _doTransfer(act, p.ids, p.rootId, win.rootId, destPath || "", p.path || ""));
    });

    // ---- mount the WinBox -------------------------------------------
    const root = win.roots.find(r => r.id === win.rootId);
    const title = "📁 " + ((root ? root.label : "") + (win.path ? " / " + win.path : "") || _t("fw.title", "파일 탐색기"));
    const wb = new WinBox({
      title,
      width: 560, height: 420,
      x: "center", y: "center",
      mount: body,
      class: ["fw-winbox"],
      onclose: () => { _wins.delete(id); return false; },
    });
    win.wb = wb;
    win.setTitle = () => {
      const rr = win.roots.find(r => r.id === win.rootId);
      wb.setTitle("📁 " + ((rr ? rr.label : "") + (win.path ? " / " + win.path : "")));
    };
    // keep title in sync on nav
    const _origLoad = win.load;
    win.load = async () => { await _origLoad(); win.setTitle(); };
    _wins.set(id, win);
    win.load();
    return win;
  }

  function count() { return _wins.size; }

  window.fileWindows = { open, count };
})();
