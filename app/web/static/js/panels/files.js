/*
 * Files panel — the document explorer (kind='file' roots).
 *
 * Windows-Explorer-style: left folder tree + right file list, breadcrumb,
 * and a search box. Read-only for now (browse / search / download); write
 * ops (upload / new folder / rename / move / delete) get wired in later on
 * writable roots. Talks to the /api/files read API (routes_files.py).
 *
 * IIFE exposing window.filesPanel = { init, activate }. Owns #files-view.
 */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const _t = (k, f) => (window._t ? window._t(k, f) : f);

  let treeEl, crumbsEl, listEl, searchEl, countEl;
  let roots = [];
  let loaded = false;
  let cur = { rootId: null, path: "" };
  let _searchTimer = null;

  // ---- helpers ----------------------------------------------------------
  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1).replace(/\.0$/, "") + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(1).replace(/\.0$/, "") + " GB";
  }
  function fmtDate(iso) {
    if (!iso) return "";
    return iso.slice(0, 10);
  }
  function iconFor(ext, mime) {
    const e = (ext || "").toLowerCase();
    if (["pdf"].includes(e)) return "📕";
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
  function setCount(nFiles, nFolders, isSearch) {
    if (!countEl) return;
    if (isSearch) {
      countEl.textContent = (window._tn
        ? window._tn("files.n_results", "검색 {n}개", { n: nFiles })
        : "검색 " + nFiles + "개");
      return;
    }
    const files = (window._tn ? window._tn("files.n_files", "파일 {n}개", { n: nFiles })
                              : "파일 " + nFiles + "개");
    const folders = nFolders
      ? (window._tn ? window._tn("files.n_folders", "폴더 {n} · ", { n: nFolders })
                    : "폴더 " + nFolders + " · ")
      : "";
    countEl.textContent = folders + files;
  }

  function kindLabel(ext, mime) {
    if (ext) return ext.toUpperCase();
    if (mime) return mime;
    return _t("files.kind_file", "파일");
  }

  // ---- data -------------------------------------------------------------
  async function loadRoots() {
    roots = await window.api.get("/api/files/roots");
    return roots;
  }
  function listFolder(rootId, path) {
    const q = "root_id=" + encodeURIComponent(rootId) +
      "&path=" + encodeURIComponent(path || "");
    return window.api.get("/api/files/list?" + q);
  }

  // ---- folder tree (left) ----------------------------------------------
  function renderTreeRoots() {
    treeEl.innerHTML = "";
    if (!roots.length) {
      treeEl.innerHTML =
        `<div class="fx-empty">${_t("files.no_roots", "파일 폴더가 없습니다")}</div>`;
      return;
    }
    for (const r of roots) {
      treeEl.appendChild(makeNode(r.id, "", r.label, true));
    }
  }
  // A tree node: caret + label. Children lazy-loaded on first expand.
  function makeNode(rootId, path, label, isRoot) {
    const node = document.createElement("div");
    node.className = "fx-node";
    const row = document.createElement("div");
    row.className = "fx-node-row";
    row.innerHTML =
      `<span class="fx-caret">▸</span>` +
      `<span class="fx-ico">${isRoot ? "🗂" : "📁"}</span>` +
      `<span class="fx-node-label">${escapeHtml(label)}</span>`;
    const children = document.createElement("div");
    children.className = "fx-children";
    children.style.display = "none";
    children.dataset.loaded = "0";
    node.appendChild(row);
    node.appendChild(children);

    const caret = row.querySelector(".fx-caret");
    async function toggle() {
      const open = children.style.display !== "none";
      if (open) {
        children.style.display = "none";
        caret.textContent = "▸";
        return;
      }
      caret.textContent = "▾";
      children.style.display = "";
      if (children.dataset.loaded === "0") {
        children.dataset.loaded = "1";
        try {
          const data = await listFolder(rootId, path);
          for (const sub of data.folders) {
            const childPath = path ? path + "/" + sub : sub;
            children.appendChild(makeNode(rootId, childPath, sub, false));
          }
          if (!data.folders.length) {
            children.innerHTML = `<div class="fx-node-empty">—</div>`;
          }
        } catch (e) {
          children.innerHTML = `<div class="fx-node-empty">!</div>`;
        }
      }
    }
    caret.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    row.addEventListener("click", () => {
      treeEl.querySelectorAll(".fx-node-row.sel").forEach(x => x.classList.remove("sel"));
      row.classList.add("sel");
      openFolder(rootId, path);
    });
    return node;
  }

  // ---- breadcrumb + list (right) ---------------------------------------
  function renderCrumbs(rootId, path) {
    const root = roots.find(r => r.id === rootId);
    const parts = path ? path.split("/") : [];
    const crumbs = [`<button class="fx-crumb" data-path="">${escapeHtml(root ? root.label : "?")}</button>`];
    let acc = "";
    for (const p of parts) {
      acc = acc ? acc + "/" + p : p;
      crumbs.push(`<span class="fx-sep">›</span>` +
        `<button class="fx-crumb" data-path="${escapeAttr(acc)}">${escapeHtml(p)}</button>`);
    }
    crumbsEl.innerHTML = crumbs.join("");
    crumbsEl.querySelectorAll(".fx-crumb").forEach(b => {
      b.addEventListener("click", () => openFolder(rootId, b.dataset.path));
    });
  }

  function rowHtml(cells, cls) {
    return `<div class="fx-row ${cls || ""}">` +
      cells.map(c => `<div class="fx-cell">${c}</div>`).join("") + `</div>`;
  }

  async function openFolder(rootId, path) {
    cur = { rootId, path: path || "" };
    searchEl.value = "";
    renderCrumbs(rootId, cur.path);
    listEl.innerHTML = `<div class="fx-loading">${_t("common.loading", "불러오는 중…")}</div>`;
    let data;
    try {
      data = await listFolder(rootId, cur.path);
    } catch (e) {
      listEl.innerHTML = `<div class="fx-loading">${_t("files.load_fail", "불러오기 실패")}</div>`;
      return;
    }
    const rows = [];
    rows.push(
      `<div class="fx-row fx-head">` +
      `<div class="fx-cell">${_t("files.col_name", "이름")}</div>` +
      `<div class="fx-cell fx-num">${_t("files.col_size", "크기")}</div>` +
      `<div class="fx-cell">${_t("files.col_mtime", "수정일")}</div>` +
      `<div class="fx-cell">${_t("files.col_kind", "종류")}</div></div>`);
    // Parent-up row when not at root.
    if (cur.path) {
      const parent = cur.path.includes("/") ? cur.path.slice(0, cur.path.lastIndexOf("/")) : "";
      rows.push(`<div class="fx-row fx-folder fx-up" data-path="${escapeAttr(parent)}">` +
        `<div class="fx-cell">📁 ..</div><div class="fx-cell"></div>` +
        `<div class="fx-cell"></div><div class="fx-cell"></div></div>`);
    }
    for (const sub of data.folders) {
      const childPath = cur.path ? cur.path + "/" + sub : sub;
      rows.push(`<div class="fx-row fx-folder" data-path="${escapeAttr(childPath)}">` +
        `<div class="fx-cell">📁 ${escapeHtml(sub)}</div><div class="fx-cell fx-num"></div>` +
        `<div class="fx-cell"></div><div class="fx-cell">${_t("files.kind_folder", "폴더")}</div></div>`);
    }
    for (const f of data.files) {
      rows.push(`<div class="fx-row fx-file" data-id="${f.id}">` +
        `<div class="fx-cell">${iconFor(f.ext, f.mime)} ${escapeHtml(f.filename)}</div>` +
        `<div class="fx-cell fx-num">${fmtSize(f.size)}</div>` +
        `<div class="fx-cell">${fmtDate(f.mtime)}</div>` +
        `<div class="fx-cell">${escapeHtml(kindLabel(f.ext, f.mime))}</div></div>`);
    }
    if (!data.folders.length && !data.files.length) {
      rows.push(`<div class="fx-empty">${_t("files.empty_folder", "빈 폴더")}</div>`);
    }
    listEl.innerHTML = rows.join("");
    setCount(data.files.length, data.folders.length, false);
  }

  async function runSearch(q) {
    q = (q || "").trim();
    if (!q) { openFolder(cur.rootId, cur.path); return; }
    crumbsEl.innerHTML =
      `<span class="fx-crumb-static">🔍 "${escapeHtml(q)}"</span>`;
    listEl.innerHTML = `<div class="fx-loading">${_t("common.loading", "불러오는 중…")}</div>`;
    let data;
    try {
      const url = "/api/files/search?q=" + encodeURIComponent(q) +
        (cur.rootId ? "&root_id=" + encodeURIComponent(cur.rootId) : "");
      data = await window.api.get(url);
    } catch (e) {
      listEl.innerHTML = `<div class="fx-loading">${_t("files.load_fail", "불러오기 실패")}</div>`;
      return;
    }
    const rows = [`<div class="fx-row fx-head">` +
      `<div class="fx-cell">${_t("files.col_name", "이름")}</div>` +
      `<div class="fx-cell fx-num">${_t("files.col_size", "크기")}</div>` +
      `<div class="fx-cell">${_t("files.col_path", "경로")}</div>` +
      `<div class="fx-cell">${_t("files.col_kind", "종류")}</div></div>`];
    for (const f of data.results) {
      rows.push(`<div class="fx-row fx-file" data-id="${f.id}">` +
        `<div class="fx-cell">${iconFor(f.ext, f.mime)} ${escapeHtml(f.filename)}</div>` +
        `<div class="fx-cell fx-num">${fmtSize(f.size)}</div>` +
        `<div class="fx-cell fx-path">${escapeHtml(f.rel_path)}</div>` +
        `<div class="fx-cell">${escapeHtml(kindLabel(f.ext, f.mime))}</div></div>`);
    }
    if (!data.results.length) {
      rows.push(`<div class="fx-empty">${_t("files.no_results", "검색 결과 없음")}</div>`);
    }
    listEl.innerHTML = rows.join("");
    setCount(data.results.length, 0, true);
  }

  function download(fileId) {
    const a = document.createElement("a");
    a.href = "/api/files/" + fileId + "/download";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function selectedFileIds() {
    return [...listEl.querySelectorAll(".fx-file.sel")]
      .map(r => parseInt(r.dataset.id, 10))
      .filter(n => !isNaN(n));
  }

  function shareSelected() {
    const ids = selectedFileIds();
    if (!ids.length) {
      window.uiAlert(_t("files.share_none", "공유할 파일을 선택하세요 (Ctrl/⌘+클릭으로 다중 선택)"));
      return;
    }
    _openShareModal(ids);
  }

  // Share-options dialog (title / password / expiry / max downloads) —
  // mirrors the photo share modal's options. Self-contained (inline styles)
  // so it needs no extra CSS.
  function _openShareModal(ids) {
    const back = document.createElement("div");
    back.className = "fx-modal-back";
    back.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);" +
      "display:flex;align-items:center;justify-content:center;z-index:9999";
    const inStyle = "width:100%;box-sizing:border-box;margin:2px 0 8px;padding:6px;" +
      "background:#141414;color:#eee;border:1px solid #333;border-radius:4px;font-family:inherit";
    const box = document.createElement("div");
    box.style.cssText = "background:#1e1e1e;color:#eee;border:1px solid #333;" +
      "border-radius:8px;padding:18px;width:360px;max-width:92vw;font-size:13px";
    const D = _t("files.days", "일");
    box.innerHTML =
      `<div style="font-weight:600;margin-bottom:12px">🔗 ${_t("files.share", "공유")} · ${ids.length}${_t("files.count_suffix", "개")}</div>` +
      `<label>${_t("files.share_title_label", "제목")}</label>` +
      `<input id="fxs-title" style="${inStyle}">` +
      `<label>${_t("files.share_pw", "암호 (선택)")}</label>` +
      `<input id="fxs-pw" type="password" autocomplete="new-password" style="${inStyle}">` +
      `<label>${_t("files.share_expiry", "만료")}</label>` +
      `<select id="fxs-exp" style="${inStyle}">` +
        `<option value="0">${_t("files.exp_never", "무기한")}</option>` +
        `<option value="1">1${D}</option><option value="7" selected>7${D}</option>` +
        `<option value="30">30${D}</option><option value="365">365${D}</option></select>` +
      `<label>${_t("files.share_maxdl", "최대 다운로드 수 (선택)")}</label>` +
      `<input id="fxs-max" type="number" min="1" style="${inStyle}">` +
      `<div id="fxs-result" style="display:none">` +
        `<label>${_t("files.share_link", "공유 링크")}</label>` +
        `<input id="fxs-url" readonly style="${inStyle}"></div>` +
      `<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:6px">` +
        `<button id="fxs-cancel" style="padding:6px 12px">${_t("common.close", "닫기")}</button>` +
        `<button id="fxs-go" class="primary" style="padding:6px 12px">${_t("files.share_make", "링크 만들기")}</button></div>`;
    back.appendChild(box);
    document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    box.querySelector("#fxs-cancel").addEventListener("click", close);
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
    });
    const go = box.querySelector("#fxs-go");
    go.addEventListener("click", async () => {
      const body = { file_ids: ids };
      const title = box.querySelector("#fxs-title").value.trim();
      if (title) body.title = title;
      const pw = box.querySelector("#fxs-pw").value;
      if (pw) body.password = pw;
      const exp = parseInt(box.querySelector("#fxs-exp").value, 10);
      if (exp > 0) body.expires_in_days = exp;
      const mx = parseInt(box.querySelector("#fxs-max").value, 10);
      if (mx > 0) body.max_downloads = mx;
      go.disabled = true;
      try {
        const res = await window.api.post("/api/shares/files", body);
        const url = location.origin + res.url_path;
        box.querySelector("#fxs-result").style.display = "";
        const inp = box.querySelector("#fxs-url");
        inp.value = url;
        inp.focus();
        inp.select();
        go.textContent = _t("files.share_done", "완료 ✓ — 링크 복사");
      } catch (e) {
        go.disabled = false;
        window.uiAlert(_t("files.share_fail", "공유 생성 실패"));
      }
    });
  }

  function refresh() { openFolder(cur.rootId, cur.path); }

  async function doUpload(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    fd.append("root_id", cur.rootId);
    fd.append("path", cur.path || "");
    for (const f of fileList) fd.append("files", f);
    try {
      const r = await window.fetch("/api/files/upload", { method: "POST", body: fd });
      if (!r.ok) throw new Error(r.status);
      refresh();
    } catch (e) {
      window.uiAlert(_t("files.upload_fail", "업로드 실패 (읽기 전용 폴더이거나 권한 없음)"));
    }
  }

  async function doDelete() {
    const ids = selectedFileIds();
    if (!ids.length) { window.uiAlert(_t("files.select_first", "파일을 선택하세요")); return; }
    const ok = await window.uiConfirm(
      (window._tn ? window._tn("files.delete_confirm", "{n}개 파일을 영구 삭제할까요?", { n: ids.length })
                  : _t("files.delete_confirm", "선택한 파일을 영구 삭제할까요?")));
    if (!ok) return;
    try { await window.api.post("/api/files/delete", { file_ids: ids }); refresh(); }
    catch (e) { window.uiAlert(_t("files.delete_fail", "삭제 실패")); }
  }

  async function doRename() {
    const rows = [...listEl.querySelectorAll(".fx-file.sel")];
    if (rows.length !== 1) {
      window.uiAlert(_t("files.rename_one", "이름을 바꿀 파일 하나만 선택하세요"));
      return;
    }
    const id = parseInt(rows[0].dataset.id, 10);
    const curName = rows[0].querySelector(".fx-cell").textContent.trim().replace(/^\S+\s+/, "");
    const name = await window.uiPrompt(_t("files.rename_prompt", "새 이름"), curName);
    if (!name || !name.trim()) return;
    try { await window.api.post("/api/files/" + id + "/rename", { new_name: name.trim() }); refresh(); }
    catch (e) { window.uiAlert(_t("files.rename_fail", "이름 변경 실패 (같은 이름이 있거나 읽기 전용)")); }
  }

  // ---- wiring -----------------------------------------------------------
  function init() {
    treeEl = $("#fx-tree");
    crumbsEl = $("#fx-crumbs");
    listEl = $("#fx-list");
    searchEl = $("#fx-search");
    countEl = $("#fx-count");
    if (!listEl) return;

    // Action buttons in the bar. Writability is enforced server-side
    // (409 on readonly roots); the buttons stay visible for simplicity.
    const bar = searchEl.parentNode;
    if (bar && !bar.querySelector(".fx-share-btn")) {
      const upInput = document.createElement("input");
      upInput.type = "file"; upInput.multiple = true; upInput.style.display = "none";
      upInput.addEventListener("change", () => { doUpload(upInput.files); upInput.value = ""; });
      bar.appendChild(upInput);
      const mkBtn = (cls, label, title, fn) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = cls; b.textContent = label;
        if (title) b.title = title;
        b.addEventListener("click", fn);
        return b;
      };
      bar.insertBefore(mkBtn("fx-act-btn", "⬆ " + _t("files.upload", "업로드"),
        _t("files.upload_title", "현재 폴더에 업로드"), () => upInput.click()), searchEl);
      bar.insertBefore(mkBtn("fx-act-btn", "✎ " + _t("files.rename", "이름변경"), "", doRename), searchEl);
      bar.insertBefore(mkBtn("fx-act-btn", "🗑 " + _t("files.delete", "삭제"), "", doDelete), searchEl);
      bar.insertBefore(mkBtn("fx-share-btn", "🔗 " + _t("files.share", "공유"),
        _t("files.share_title", "선택한 파일 공유 링크 만들기"), shareSelected), searchEl);
    }

    // Row interactions: click selects (Ctrl/⌘ or Shift-click extends for
    // multi-select), double click enters folder / downloads file. Delegated
    // so re-rendered rows keep working.
    listEl.addEventListener("click", (e) => {
      const row = e.target.closest(".fx-row");
      if (!row || row.classList.contains("fx-head")) return;
      if (!(e.ctrlKey || e.metaKey || e.shiftKey)) {
        listEl.querySelectorAll(".fx-row.sel").forEach(x => x.classList.remove("sel"));
        row.classList.add("sel");
      } else {
        row.classList.toggle("sel");
      }
    });
    listEl.addEventListener("dblclick", (e) => {
      const row = e.target.closest(".fx-row");
      if (!row) return;
      if (row.classList.contains("fx-folder")) {
        openFolder(cur.rootId, row.dataset.path);
      } else if (row.classList.contains("fx-file")) {
        download(parseInt(row.dataset.id, 10));
      }
    });
    searchEl.addEventListener("input", () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => runSearch(searchEl.value), 250);
    });
  }

  async function activate() {
    if (!loaded) {
      loaded = true;
      // initModes() already fetched roots to decide whether to show the mode
      // switch — reuse them so the tree renders instantly instead of waiting
      // on a second /api/files/roots round-trip.
      if (!roots.length) {
        try { await loadRoots(); } catch (e) { roots = []; }
      }
      renderTreeRoots();
      if (roots.length) {
        const first = treeEl.querySelector(".fx-node-row");
        if (first) first.classList.add("sel");
        openFolder(roots[0].id, "");
      } else {
        crumbsEl.innerHTML = "";
        listEl.innerHTML =
          `<div class="fx-empty">${_t("files.no_roots", "파일 폴더가 없습니다")}</div>`;
      }
    }
  }

  // escapeHtml/escapeAttr come from common.js; provide a tiny fallback so
  // this module still renders if loaded standalone (harness/tests).
  const escapeHtml = window.escapeHtml || ((s) => String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
  const escapeAttr = window.escapeAttr || escapeHtml;

  window.filesPanel = { init, activate, hasRoots: () => roots.length > 0, loadRoots };
})();
