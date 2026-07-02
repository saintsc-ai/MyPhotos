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

  let treeEl, crumbsEl, listEl, searchEl;
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
      `<span class="fx-node-label">${isRoot ? "🗂" : "📁"} ${escapeHtml(label)}</span>`;
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
  }

  function download(fileId) {
    const a = document.createElement("a");
    a.href = "/api/files/" + fileId + "/download";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ---- wiring -----------------------------------------------------------
  function init() {
    treeEl = $("#fx-tree");
    crumbsEl = $("#fx-crumbs");
    listEl = $("#fx-list");
    searchEl = $("#fx-search");
    if (!listEl) return;

    // Row interactions: single click selects, double click enters folder /
    // downloads file. Delegated so re-rendered rows keep working.
    listEl.addEventListener("click", (e) => {
      const row = e.target.closest(".fx-row");
      if (!row || row.classList.contains("fx-head")) return;
      listEl.querySelectorAll(".fx-row.sel").forEach(x => x.classList.remove("sel"));
      row.classList.add("sel");
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
      try {
        await loadRoots();
      } catch (e) {
        roots = [];
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
