/*
 * File explorer — one reusable component used both for the embedded file mode
 * (#files-view) AND for extra floating windows (WinBox). Same UI everywhere:
 * left folder tree + breadcrumb + search + toolbar + list + right-click menu.
 *
 * createExplorer(host, opts) builds the whole UI into `host` with class
 * selectors (no global ids) so many instances can coexist. Drag a selection
 * from any explorer (list) onto any other explorer's list / folder row / tree
 * node to copy or move (drop shows a 복사 / 이동 / 취소 menu → /api/files/
 * copy|move). Photo mode hides all windows; file mode shows them.
 *
 * Exposes:
 *   window.filesPanel  = { init, activate, hasRoots, loadRoots }  (base, compat)
 *   window.fileWindows = { openNew, setVisible, count }
 */
(function () {
  "use strict";

  const _t = (k, f) => (window._t ? window._t(k, f) : f);
  const _tn = (k, f, p) => (window._tn ? window._tn(k, f, p) : f);
  const escapeHtml = window.escapeHtml || ((s) => String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
  const escapeAttr = window.escapeAttr || escapeHtml;
  const DT_MIME = "application/x-myphotos-files";

  // ---- shared helpers ---------------------------------------------------
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
  function kindLabel(ext, mime) {
    if (ext) return ext.toUpperCase();
    if (mime) return mime;
    return _t("files.kind_file", "파일");
  }

  // ---- shared popup menu (context menu + drop menu) ---------------------
  function _popup(x, y, items) {
    const old = document.getElementById("fx-popup");
    if (old) old.remove();
    const el = document.createElement("div");
    el.id = "fx-popup";
    el.className = "folder-menu";
    el.style.cssText = "position:fixed;z-index:2147483647;left:0;top:0";
    el.innerHTML = items.map(it => it.sep
      ? `<div class="folder-menu-sep"></div>`
      : `<button type="button" class="folder-menu-item${it.danger ? " danger" : ""}">${escapeHtml(it.label)}</button>`
    ).join("");
    document.body.appendChild(el);
    const PAD = 4, r = el.getBoundingClientRect();
    el.style.left = Math.min(x, innerWidth - r.width - PAD) + "px";
    el.style.top = Math.min(y, innerHeight - r.height - PAD) + "px";
    const close = () => { el.remove(); document.removeEventListener("mousedown", od, true); };
    const od = (ev) => { if (!el.contains(ev.target)) close(); };
    setTimeout(() => document.addEventListener("mousedown", od, true), 0);
    const btns = [...el.querySelectorAll(".folder-menu-item")];
    let i = 0;
    items.forEach(it => { if (it.sep) return; btns[i++].addEventListener("click", () => { close(); if (it.fn) it.fn(); }); });
  }

  // ---- shared roots cache + instance registry --------------------------
  let _rootsCache = [];
  async function _fetchRoots() { _rootsCache = await window.api.get("/api/files/roots"); return _rootsCache; }
  const _instances = new Set();
  function _refreshMatching(rootId, path) {
    for (const ex of _instances) { if (ex.matches(rootId, path)) ex.reload(); }
  }
  async function _doTransfer(action, ids, srcRootId, destRootId, destPath, srcPath) {
    try {
      const r = await fetch("/api/files/" + (action === "move" ? "move" : "copy"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: ids, dest_root_id: destRootId, dest_path: destPath }),
      });
      if (!r.ok) { let d = "HTTP " + r.status; try { d = (await r.json()).detail || d; } catch (_) {} window.uiAlert(d); return; }
      const data = await r.json().catch(() => ({}));
      const n = action === "move" ? (data.moved || 0) : (data.copied || 0);
      if (!n && (data.skipped || 0)) window.uiAlert(_t("fw.all_skipped", "옮길 수 있는 항목이 없습니다 (읽기전용/중복/동일 위치)."));
      _refreshMatching(destRootId, destPath);
      if (action === "move") _refreshMatching(srcRootId, srcPath);
    } catch (e) { window.uiAlert(e.message); }
  }
  function _transferMenu(x, y, p, destRootId, destPath) {
    _popup(x, y, [
      { label: "📋 " + _t("fw.copy", "복사"), fn: () => _doTransfer("copy", p.ids, p.rootId, destRootId, destPath || "", p.path || "") },
      { label: "✂️ " + _t("fw.move", "이동"), fn: () => _doTransfer("move", p.ids, p.rootId, destRootId, destPath || "", p.path || "") },
      { sep: true },
      { label: _t("common.cancel", "취소") },
    ]);
  }

  // ======================================================================
  // The explorer component
  // ======================================================================
  function createExplorer(host, opts) {
    opts = opts || {};
    const persist = !!opts.persist;      // tree-state localStorage (base only)
    host.classList.add("fx-host");
    host.innerHTML =
      `<div class="fx-tree"></div>
       <div class="fx-main">
         <div class="fx-bar">
           <div class="fx-crumbs"></div>
           <span class="fx-count"></span>
           <input type="search" class="fx-search" placeholder="${escapeAttr(_t("files.search_ph", "파일 검색…"))}">
         </div>
         <div class="fx-list"></div>
       </div>`;
    const treeEl = host.querySelector(".fx-tree");
    const crumbsEl = host.querySelector(".fx-crumbs");
    const countEl = host.querySelector(".fx-count");
    const searchEl = host.querySelector(".fx-search");
    const listEl = host.querySelector(".fx-list");

    let roots = _rootsCache;
    let cur = { rootId: null, path: "" };
    let _searchTimer = null;
    let _selAnchor = null;
    let _folderData = null, _searchData = null;
    let _sort = { col: "name", dir: "asc" };
    const SORT_KEY = "myphotos-fx-sort";
    try { const s = JSON.parse(localStorage.getItem(SORT_KEY) || "null"); if (s && s.col) _sort = { col: s.col, dir: s.dir === "desc" ? "desc" : "asc" }; } catch (_) {}
    function _saveSort() { try { localStorage.setItem(SORT_KEY, JSON.stringify(_sort)); } catch (_) {} }

    // tree state (only the base persists it)
    const TREE_KEY = "myphotos-fx-tree";
    let _expanded = new Set();
    const _ek = (r, p) => r + "\t" + (p || "");
    function _saveTreeState() {
      if (!persist) return;
      try { localStorage.setItem(TREE_KEY, JSON.stringify({ rootId: cur.rootId, path: cur.path, expanded: [..._expanded] })); } catch (_) {}
    }
    function _loadTreeState() { if (!persist) return null; try { return JSON.parse(localStorage.getItem(TREE_KEY) || "null"); } catch (_) { return null; } }
    function _findNode(rootId, path) {
      for (const n of treeEl.querySelectorAll(".fx-node")) { if (n.__rootId === rootId && (n.__path || "") === (path || "")) return n; }
      return null;
    }

    function setCount(nFiles, nFolders, isSearch) {
      if (isSearch) { countEl.textContent = _tn("files.n_results", "검색 {n}개", { n: nFiles }); return; }
      const files = _tn("files.n_files", "파일 {n}개", { n: nFiles });
      const folders = nFolders ? _tn("files.n_folders", "폴더 {n} · ", { n: nFolders }) : "";
      countEl.textContent = folders + files;
    }

    function listFolder(rootId, path) {
      return window.api.get("/api/files/list?root_id=" + encodeURIComponent(rootId) + "&path=" + encodeURIComponent(path || ""));
    }

    // ---- folder tree ---------------------------------------------------
    function renderTreeRoots() {
      treeEl.innerHTML = "";
      if (!roots.length) { treeEl.innerHTML = `<div class="fx-empty">${_t("files.no_roots", "파일 폴더가 없습니다")}</div>`; return; }
      for (const r of roots) treeEl.appendChild(makeNode(r.id, "", r.label, true));
    }
    function makeNode(rootId, path, label, isRoot, hasChildren = true) {
      const node = document.createElement("div");
      node.className = "fx-node";
      const row = document.createElement("div");
      row.className = "fx-node-row fx-drop-node";
      row.__rootId = rootId; row.__path = path;
      row.innerHTML =
        (hasChildren ? `<span class="fx-caret">▸</span>` : `<span class="fx-caret fx-caret-leaf"></span>`) +
        `<span class="fx-ico">${isRoot ? "🗂" : "📁"}</span>` +
        `<span class="fx-node-label">${escapeHtml(label)}</span>`;
      const children = document.createElement("div");
      children.className = "fx-children"; children.style.display = "none"; children.dataset.loaded = "0";
      node.appendChild(row); node.appendChild(children);
      const caret = row.querySelector(".fx-caret");
      node.__rootId = rootId; node.__path = path;
      async function _loadChildren() {
        if (children.dataset.loaded !== "0") return;
        children.dataset.loaded = "1";
        try {
          const data = await listFolder(rootId, path);
          const withKids = new Set(data.folders_with_children || []);
          for (const sub of data.folders) {
            const childPath = path ? path + "/" + sub : sub;
            children.appendChild(makeNode(rootId, childPath, sub, false, withKids.has(sub)));
          }
          if (!data.folders.length) children.innerHTML = `<div class="fx-node-empty">—</div>`;
        } catch (e) { children.innerHTML = `<div class="fx-node-empty">!</div>`; }
      }
      async function expand() { if (children.style.display !== "none") return; caret.textContent = "▾"; children.style.display = ""; await _loadChildren(); _expanded.add(_ek(rootId, path)); }
      function collapse() { children.style.display = "none"; caret.textContent = "▸"; _expanded.delete(_ek(rootId, path)); }
      if (hasChildren) {
        node.__expand = expand;
        caret.addEventListener("click", async (e) => { e.stopPropagation(); if (children.style.display === "none") await expand(); else collapse(); _saveTreeState(); });
      }
      row.addEventListener("click", () => {
        treeEl.querySelectorAll(".fx-node-row.sel").forEach(x => x.classList.remove("sel"));
        row.classList.add("sel"); openFolder(rootId, path);
      });
      return node;
    }

    // ---- breadcrumb ----------------------------------------------------
    function renderCrumbs(rootId, path) {
      const root = roots.find(r => r.id === rootId);
      const parts = path ? path.split("/") : [];
      const crumbs = [`<button class="fx-crumb" data-path="">${escapeHtml(root ? root.label : "?")}</button>`];
      let acc = "";
      for (const p of parts) { acc = acc ? acc + "/" + p : p; crumbs.push(`<span class="fx-sep">›</span><button class="fx-crumb" data-path="${escapeAttr(acc)}">${escapeHtml(p)}</button>`); }
      crumbsEl.innerHTML = crumbs.join("");
      crumbsEl.querySelectorAll(".fx-crumb").forEach(b => b.addEventListener("click", () => openFolder(rootId, b.dataset.path)));
    }

    // ---- sorting -------------------------------------------------------
    function _applySortClick(col) {
      if (_sort.col === col) _sort.dir = _sort.dir === "asc" ? "desc" : "asc";
      else _sort = { col, dir: (col === "size" || col === "mtime") ? "desc" : "asc" };
      _saveSort();
    }
    function _cmpFiles(a, b) {
      const dir = _sort.dir === "desc" ? -1 : 1;
      const nameOf = (x) => String(x.filename || "").toLowerCase();
      if (_sort.col === "name") return nameOf(a).localeCompare(nameOf(b)) * dir;
      let c;
      if (_sort.col === "size") c = (a.size || 0) - (b.size || 0);
      else if (_sort.col === "mtime") c = String(a.mtime || "").localeCompare(String(b.mtime || ""));
      else if (_sort.col === "kind") c = String(a.ext || a.mime || "").toLowerCase().localeCompare(String(b.ext || b.mime || "").toLowerCase());
      else if (_sort.col === "path") c = String(a.rel_path || "").toLowerCase().localeCompare(String(b.rel_path || "").toLowerCase());
      else c = 0;
      if (c !== 0) return c * dir;
      return nameOf(a).localeCompare(nameOf(b));
    }
    function _cmpFolderNames(a, b) { const c = String(a).toLowerCase().localeCompare(String(b).toLowerCase()); return (_sort.col === "name" && _sort.dir === "desc") ? -c : c; }
    function _headCell(col, label, extraCls) {
      const active = _sort.col === col, arrow = active ? (_sort.dir === "asc" ? " ▲" : " ▼") : "";
      return `<div class="fx-cell${extraCls ? " " + extraCls : ""} fx-sort" data-sort="${col}" style="cursor:pointer;user-select:none">${escapeHtml(label)}<span class="fx-sort-arrow">${arrow}</span></div>`;
    }
    function _fileRowHtml(f, withPath) {
      const third = withPath ? `<div class="fx-cell fx-path">${escapeHtml(f.rel_path)}</div>` : `<div class="fx-cell">${escapeHtml(fmtDate(f.mtime))}</div>`;
      return `<div class="fx-row fx-file" draggable="true" data-id="${f.id}"><div class="fx-cell">${iconFor(f.ext, f.mime)} ${escapeHtml(f.filename)}</div>` +
        `<div class="fx-cell fx-num">${escapeHtml(fmtSize(f.size))}</div>${third}` + (withPath ? "" : `<div class="fx-cell">${escapeHtml(kindLabel(f.ext, f.mime))}</div>`) + `</div>`;
    }

    // ---- folder view ---------------------------------------------------
    async function openFolder(rootId, path) {
      cur = { rootId, path: path || "" };
      _selAnchor = null; _saveTreeState(); searchEl.value = "";
      renderCrumbs(rootId, cur.path);
      listEl.innerHTML = `<div class="fx-loading">${_t("common.loading", "불러오는 중…")}</div>`;
      let data;
      try { data = await listFolder(rootId, cur.path); } catch (e) { listEl.innerHTML = `<div class="fx-loading">${_t("files.load_fail", "불러오기 실패")}</div>`; return; }
      _folderData = data; _renderFolderRows();
      if (opts.onTitle) opts.onTitle(_title());
    }
    function _renderFolderRows() {
      const data = _folderData; if (!data) return;
      const rows = [`<div class="fx-row fx-head">` + _headCell("name", _t("files.col_name", "이름")) + _headCell("size", _t("files.col_size", "크기"), "fx-num") + _headCell("mtime", _t("files.col_mtime", "수정일")) + _headCell("kind", _t("files.col_kind", "종류")) + `</div>`];
      if (cur.path) {
        const parent = cur.path.includes("/") ? cur.path.slice(0, cur.path.lastIndexOf("/")) : "";
        rows.push(`<div class="fx-row fx-folder fx-drop-folder fx-up" data-path="${escapeAttr(parent)}"><div class="fx-cell">📁 ..</div><div class="fx-cell"></div><div class="fx-cell"></div><div class="fx-cell"></div></div>`);
      }
      for (const sub of [...data.folders].sort(_cmpFolderNames)) {
        const cp = cur.path ? cur.path + "/" + sub : sub;
        rows.push(`<div class="fx-row fx-folder fx-drop-folder" data-path="${escapeAttr(cp)}"><div class="fx-cell">📁 ${escapeHtml(sub)}</div><div class="fx-cell fx-num"></div><div class="fx-cell"></div><div class="fx-cell">${_t("files.kind_folder", "폴더")}</div></div>`);
      }
      for (const f of [...data.files].sort(_cmpFiles)) rows.push(_fileRowHtml(f, false));
      if (!data.folders.length && !data.files.length) rows.push(`<div class="fx-empty">${_t("files.empty_folder", "빈 폴더")}</div>`);
      listEl.innerHTML = rows.join(""); _selAnchor = null;
      setCount(data.files.length, data.folders.length, false);
    }

    // ---- search --------------------------------------------------------
    async function runSearch(q) {
      q = (q || "").trim();
      if (!q) { openFolder(cur.rootId, cur.path); return; }
      crumbsEl.innerHTML = `<span class="fx-crumb-static">🔍 "${escapeHtml(q)}"</span>`;
      listEl.innerHTML = `<div class="fx-loading">${_t("common.loading", "불러오는 중…")}</div>`;
      let data;
      try { data = await window.api.get("/api/files/search?q=" + encodeURIComponent(q) + (cur.rootId ? "&root_id=" + encodeURIComponent(cur.rootId) : "")); }
      catch (e) { listEl.innerHTML = `<div class="fx-loading">${_t("files.load_fail", "불러오기 실패")}</div>`; return; }
      _searchData = data; _renderSearchRows();
    }
    function _renderSearchRows() {
      const data = _searchData; if (!data) return;
      const rows = [`<div class="fx-row fx-head">` + _headCell("name", _t("files.col_name", "이름")) + _headCell("size", _t("files.col_size", "크기"), "fx-num") + _headCell("path", _t("files.col_path", "경로")) + _headCell("kind", _t("files.col_kind", "종류")) + `</div>`];
      for (const f of [...data.results].sort(_cmpFiles)) rows.push(_fileRowHtml(f, true));
      if (!data.results.length) rows.push(`<div class="fx-empty">${_t("files.no_results", "검색 결과 없음")}</div>`);
      listEl.innerHTML = rows.join(""); _selAnchor = null;
      setCount(data.results.length, 0, true);
    }
    const inSearch = () => !!searchEl.value.trim();
    function refresh() { if (inSearch()) runSearch(searchEl.value); else openFolder(cur.rootId, cur.path); }

    // ---- file ops ------------------------------------------------------
    function download(fileId) { const a = document.createElement("a"); a.href = "/api/files/" + fileId + "/download"; a.rel = "noopener"; document.body.appendChild(a); a.click(); a.remove(); }
    function selectedFileIds() { return [...listEl.querySelectorAll(".fx-file.sel")].map(r => parseInt(r.dataset.id, 10)).filter(n => !isNaN(n)); }
    function shareSelected() {
      const ids = selectedFileIds();
      if (!ids.length) { window.uiAlert(_t("files.share_none", "공유할 파일을 선택하세요 (Ctrl/⌘+클릭으로 다중 선택)")); return; }
      if (typeof window.openFileShareModal === "function") window.openFileShareModal(ids);
      else window.uiAlert(_t("files.share_unavail", "공유 UI를 사용할 수 없습니다"));
    }
    async function doUpload(fileList) {
      if (!fileList || !fileList.length) return;
      const fd = new FormData(); fd.append("root_id", cur.rootId); fd.append("path", cur.path || "");
      for (const f of fileList) fd.append("files", f);
      try { const r = await window.fetch("/api/files/upload", { method: "POST", body: fd }); if (!r.ok) throw new Error(r.status); refresh(); }
      catch (e) { window.uiAlert(_t("files.upload_fail", "업로드 실패 (읽기 전용 폴더이거나 권한 없음)")); }
    }
    async function doDelete() {
      const ids = selectedFileIds();
      if (!ids.length) { window.uiAlert(_t("files.select_first", "파일을 선택하세요")); return; }
      if (!await window.uiConfirm(_tn("files.delete_confirm", "{n}개 파일을 영구 삭제할까요?", { n: ids.length }), { danger: true })) return;
      try { await window.api.post("/api/files/delete", { file_ids: ids }); refresh(); }
      catch (e) { window.uiAlert(_t("files.delete_fail", "삭제 실패")); }
    }
    async function doRename() {
      const rows = [...listEl.querySelectorAll(".fx-file.sel")];
      if (rows.length !== 1) { window.uiAlert(_t("files.rename_one", "이름을 바꿀 파일 하나만 선택하세요")); return; }
      const id = parseInt(rows[0].dataset.id, 10);
      const curName = rows[0].querySelector(".fx-cell").textContent.trim().replace(/^\S+\s+/, "");
      const name = await window.uiPrompt(_t("files.rename_prompt", "새 이름"), curName);
      if (!name || !name.trim()) return;
      try { await window.api.post("/api/files/" + id + "/rename", { new_name: name.trim() }); refresh(); }
      catch (e) { window.uiAlert(_t("files.rename_fail", "이름 변경 실패 (같은 이름이 있거나 읽기 전용)")); }
    }

    // ---- toolbar buttons ----------------------------------------------
    const bar = searchEl.parentNode;
    const upInput = document.createElement("input");
    upInput.type = "file"; upInput.multiple = true; upInput.style.display = "none";
    upInput.addEventListener("change", () => { doUpload(upInput.files); upInput.value = ""; });
    bar.appendChild(upInput);
    const mkBtn = (label, title, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = "fx-act-btn"; b.textContent = label; if (title) b.title = title; b.addEventListener("click", fn); return b; };
    bar.insertBefore(mkBtn("🗔 " + _t("files.new_window", "새 창"), _t("files.new_window_title", "현재 폴더를 새 탐색기 창으로 열기 (창간 드래그로 복사/이동)"), () => window.fileWindows && window.fileWindows.openNew(cur.rootId, cur.path)), searchEl);
    bar.insertBefore(mkBtn("🔄 " + _t("files.refresh", "새로고침"), _t("files.refresh_title", "현재 폴더 새로고침"), () => refresh()), searchEl);
    bar.insertBefore(mkBtn("⬆ " + _t("files.upload", "업로드"), _t("files.upload_title", "현재 폴더에 업로드"), () => upInput.click()), searchEl);
    bar.insertBefore(mkBtn("✎ " + _t("files.rename", "이름변경"), "", doRename), searchEl);
    bar.insertBefore(mkBtn("🗑 " + _t("files.delete", "삭제"), "", doDelete), searchEl);
    const shareBtn = mkBtn("🔗 " + _t("files.share", "공유"), _t("files.share_title", "선택한 파일 공유 링크 만들기"), shareSelected);
    shareBtn.classList.add("fx-share-btn");
    bar.insertBefore(shareBtn, searchEl);

    // ---- list interactions --------------------------------------------
    listEl.addEventListener("click", (e) => {
      const sortCell = e.target.closest(".fx-sort[data-sort]");
      if (sortCell) { _applySortClick(sortCell.getAttribute("data-sort")); if (inSearch()) _renderSearchRows(); else _renderFolderRows(); return; }
      const row = e.target.closest(".fx-row");
      if (!row || row.classList.contains("fx-head") || !row.classList.contains("fx-file")) return;
      const rows = [...listEl.querySelectorAll(".fx-file")];
      if (e.shiftKey && _selAnchor && rows.includes(_selAnchor)) {
        const a = rows.indexOf(_selAnchor), b = rows.indexOf(row), lo = Math.min(a, b), hi = Math.max(a, b);
        rows.forEach(x => x.classList.remove("sel")); for (let i = lo; i <= hi; i++) rows[i].classList.add("sel");
        const sel = window.getSelection && window.getSelection(); if (sel) sel.removeAllRanges();
      } else if (e.ctrlKey || e.metaKey) { row.classList.toggle("sel"); _selAnchor = row; }
      else { rows.forEach(x => x.classList.remove("sel")); row.classList.add("sel"); _selAnchor = row; }
    });
    listEl.addEventListener("dblclick", (e) => {
      const folder = e.target.closest(".fx-row.fx-folder"); if (folder) { openFolder(cur.rootId, folder.dataset.path); return; }
      const file = e.target.closest(".fx-row.fx-file"); if (file) download(parseInt(file.dataset.id, 10));
    });
    searchEl.addEventListener("input", () => { clearTimeout(_searchTimer); _searchTimer = setTimeout(() => runSearch(searchEl.value), 250); });

    listEl.addEventListener("contextmenu", (e) => {
      const row = e.target.closest(".fx-row");
      const refreshItem = { label: _t("files.refresh", "새로고침"), fn: refresh };
      if (!row || row.classList.contains("fx-head")) { e.preventDefault(); _popup(e.clientX, e.clientY, [refreshItem, { label: _t("files.upload", "업로드"), fn: () => upInput.click() }]); return; }
      e.preventDefault();
      if (row.classList.contains("fx-up")) { _popup(e.clientX, e.clientY, [refreshItem, { label: _t("files.upload", "업로드"), fn: () => upInput.click() }]); return; }
      if (!row.classList.contains("sel")) { listEl.querySelectorAll(".fx-row.sel").forEach(x => x.classList.remove("sel")); row.classList.add("sel"); }
      if (row.classList.contains("fx-folder")) {
        _popup(e.clientX, e.clientY, [{ label: _t("files.ctx_open", "열기"), fn: () => openFolder(cur.rootId, row.dataset.path) }, { sep: true }, refreshItem]);
      } else if (row.classList.contains("fx-file")) {
        const id = parseInt(row.dataset.id, 10);
        _popup(e.clientX, e.clientY, [
          { label: _t("files.ctx_download", "다운로드"), fn: () => download(id) },
          { label: _t("files.share", "공유"), fn: shareSelected }, { sep: true },
          { label: _t("files.rename", "이름변경"), fn: doRename },
          { label: _t("files.delete", "삭제"), danger: true, fn: doDelete }, { sep: true }, refreshItem,
        ]);
      }
    });

    // ---- drag & drop: internal copy/move + OS-file upload -------------
    function _payload(e) { const raw = e.dataTransfer.getData(DT_MIME) || e.dataTransfer.getData("text/plain"); if (!raw) return null; try { const p = JSON.parse(raw); return (p && Array.isArray(p.ids)) ? p : null; } catch (_) { return null; } }
    const hasInternal = (e) => [...(e.dataTransfer.types || [])].includes(DT_MIME) || [...(e.dataTransfer.types || [])].includes("text/plain");
    const hasFiles = (e) => [...(e.dataTransfer.types || [])].includes("Files");

    listEl.addEventListener("dragstart", (e) => {
      const row = e.target.closest(".fx-file"); if (!row) return;
      if (!row.classList.contains("sel")) { listEl.querySelectorAll(".fx-file.sel").forEach(x => x.classList.remove("sel")); row.classList.add("sel"); _selAnchor = row; }
      const payload = JSON.stringify({ rootId: cur.rootId, path: cur.path, ids: selectedFileIds() });
      e.dataTransfer.setData(DT_MIME, payload); e.dataTransfer.setData("text/plain", payload); e.dataTransfer.effectAllowed = "copyMove";
    });
    listEl.addEventListener("dragover", (e) => {
      if (!hasInternal(e) && !hasFiles(e)) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "copy";
      const fld = e.target.closest(".fx-drop-folder");
      listEl.querySelectorAll(".fx-drag-over").forEach(x => x.classList.remove("fx-drag-over"));
      (fld || listEl).classList.add("fx-drag-over");
    });
    listEl.addEventListener("dragleave", (e) => { if (!listEl.contains(e.relatedTarget)) listEl.classList.remove("fx-drag-over"); });
    listEl.addEventListener("drop", (e) => {
      listEl.querySelectorAll(".fx-drag-over").forEach(x => x.classList.remove("fx-drag-over")); listEl.classList.remove("fx-drag-over");
      const p = _payload(e);
      if (p && p.ids.length) {
        e.preventDefault(); e.stopPropagation();
        const fld = e.target.closest(".fx-drop-folder");
        const destPath = fld ? fld.dataset.path : cur.path;
        if (p.rootId === cur.rootId && (p.path || "") === (destPath || "") && !fld) return;
        _transferMenu(e.clientX, e.clientY, p, cur.rootId, destPath || "");
        return;
      }
      const files = e.dataTransfer && e.dataTransfer.files;   // OS drag-in upload
      if (files && files.length) { e.preventDefault(); e.stopPropagation(); doUpload(files); }
    });
    // tree node drop target (copy/move into that folder)
    treeEl.addEventListener("dragover", (e) => { if (!hasInternal(e)) return; const n = e.target.closest(".fx-drop-node"); if (!n) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; treeEl.querySelectorAll(".fx-drag-over").forEach(x => x.classList.remove("fx-drag-over")); n.classList.add("fx-drag-over"); });
    treeEl.addEventListener("dragleave", (e) => { if (!treeEl.contains(e.relatedTarget)) treeEl.querySelectorAll(".fx-drag-over").forEach(x => x.classList.remove("fx-drag-over")); });
    treeEl.addEventListener("drop", (e) => {
      const n = e.target.closest(".fx-drop-node"); if (!n) return;
      treeEl.querySelectorAll(".fx-drag-over").forEach(x => x.classList.remove("fx-drag-over"));
      const p = _payload(e); if (!p || !p.ids.length) return;
      e.preventDefault(); e.stopPropagation();
      if (p.rootId === n.__rootId && (p.path || "") === (n.__path || "")) return;
      _transferMenu(e.clientX, e.clientY, p, n.__rootId, n.__path || "");
    });

    // ---- restore / activate -------------------------------------------
    async function restoreTree(st) {
      if (!st || st.rootId == null || !roots.some(r => r.id === st.rootId)) return false;
      _expanded = new Set(st.expanded || []);
      const keys = (st.expanded || []).map(k => { const [r, p] = k.split("\t"); return { r: parseInt(r, 10), p: p || "" }; })
        .sort((a, b) => (a.p ? a.p.split("/").length : 0) - (b.p ? b.p.split("/").length : 0));
      for (const { r, p } of keys) { const node = _findNode(r, p); if (node && node.__expand) { try { await node.__expand(); } catch (_) {} } }
      const selNode = _findNode(st.rootId, st.path || "");
      if (selNode) { treeEl.querySelectorAll(".fx-node-row.sel").forEach(x => x.classList.remove("sel")); const rr = selNode.querySelector(".fx-node-row"); if (rr) rr.classList.add("sel"); }
      await openFolder(st.rootId, st.path || "");
      return true;
    }
    let _loaded = false;
    async function _ensureTree() {
      if (_loaded) return roots.length > 0;
      _loaded = true;
      if (!roots.length) { try { roots = _rootsCache.length ? _rootsCache : await _fetchRoots(); } catch (e) { roots = []; } }
      renderTreeRoots();
      if (!roots.length) { crumbsEl.innerHTML = ""; listEl.innerHTML = `<div class="fx-empty">${_t("files.no_roots", "파일 폴더가 없습니다")}</div>`; return false; }
      return true;
    }
    async function activate(rootId, path) {
      // Re-entering file mode must NOT rebuild the tree (would collapse it) —
      // build once, then just no-op on subsequent activations of the base.
      const alreadyLoaded = _loaded;
      if (!await _ensureTree()) return;
      if (rootId != null) { openFolder(rootId, path || ""); return; }
      if (alreadyLoaded) return;   // base re-activation: keep current state
      const restored = persist ? await restoreTree(_loadTreeState()) : false;
      if (!restored) { const first = treeEl.querySelector(".fx-node-row"); if (first) first.classList.add("sel"); openFolder(roots[0].id, ""); }
    }
    function _title() { const r = roots.find(x => x.id === cur.rootId); return "📁 " + ((r ? r.label : "") + (cur.path ? " / " + cur.path : "") || _t("fw.title", "파일 탐색기")); }

    const inst = { activate, reload: refresh, openFolder, matches: (r, p) => cur.rootId === r && (cur.path || "") === (p || ""), title: _title, destroy: () => _instances.delete(inst) };
    _instances.add(inst);
    return inst;
  }

  // ======================================================================
  // Window manager + base/shim
  // ======================================================================
  const _wins = new Set();
  let _visible = true;
  function openNew(rootId, path) {
    if (!window.WinBox) { window.uiAlert("WinBox 미로딩"); return; }
    const body = document.createElement("div");
    const wb = new WinBox({ title: _t("fw.title", "파일 탐색기"), width: "72%", height: "72%", x: "center", y: "center", mount: body, class: ["fw-winbox"], onclose: () => { inst.destroy(); _wins.delete(w); return false; } });
    const inst = createExplorer(body, { onTitle: (t) => wb.setTitle(t) });
    const w = { wb, inst, rootEl: body.closest(".winbox") };
    if (!_visible && w.rootEl) w.rootEl.style.display = "none";
    _wins.add(w);
    try { wb.maximize(); } catch (_) {}
    inst.activate(rootId, path);
    return w;
  }
  function setVisible(v) { _visible = !!v; for (const w of _wins) if (w.rootEl) w.rootEl.style.display = _visible ? "" : "none"; }
  window.fileWindows = { openNew, setVisible, count: () => _wins.size };

  // Base explorer (embedded #files-view) — compat shim for the inline app.
  let _base = null;
  function init() { const hv = document.getElementById("files-view"); if (hv && !_base) _base = createExplorer(hv, { persist: true }); }
  function activate() { setVisible(true); if (_base) return _base.activate(); }
  window.filesPanel = { init, activate, hasRoots: () => _rootsCache.length > 0, loadRoots: _fetchRoots };
})();
