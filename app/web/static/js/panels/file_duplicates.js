/*
 * File-duplicates panel — the 파일 tab of admin "중복 관리".
 *
 * Sibling of duplicates.js (photos). Files sharing a sha256 across
 * locations are byte-identical duplicates. There are NO thumbnails / EXIF
 * here — each member is an icon + name + size + mtime + path row. And there
 * is NO trash: deletion is a permanent os.remove on writable roots (via the
 * existing /api/files/delete endpoint), so this panel is manual-select only
 * (no auto-cleanup) and warns loudly before deleting.
 *
 * IIFE exposing window.fileDuplicatesPanel = { init, load, onShow }.
 * Globals used: $, escapeHtml, escapeAttr, _t, _tn, createInfScroll (all
 * from common.js / inf-scroll.js, same as duplicates.js).
 */
(function () {
  "use strict";

  const PAGE_SIZE = 30;
  let inf = null;
  let isActive = () => false;
  let _loadedOnce = false;

  function _msg(kind, text) {
    const el = document.getElementById("fdup-msg");
    if (!el) return;
    el.className = "msg " + (kind === "ok" ? "ok" : kind === "err" ? "err" : "");
    el.textContent = text;
    if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 6000);
  }

  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1).replace(/\.0$/, "") + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(1).replace(/\.0$/, "") + " GB";
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
  const _splitParent = (rel) => {
    const i = rel.lastIndexOf("/");
    return { parent: i >= 0 ? rel.slice(0, i + 1) : "", name: i >= 0 ? rel.slice(i + 1) : rel };
  };

  // ---- status text ----------------------------------------------
  function _refreshStatus() {
    const status = $("#fdup-status");
    if (!status || !inf) return;
    const total = inf.getTotal();
    const loaded = $("#fdup-list").querySelectorAll(".dup-group").length;
    if (inf.isLoading()) {
      status.textContent = loaded ? _t("common.loading_more", "더 불러오는 중…")
                                  : _t("common.loading", "불러오는 중…");
      return;
    }
    if (total <= 0) { status.textContent = ""; return; }
    if (inf.isBotDone()) {
      status.textContent = loaded >= total
        ? _tn("dup.end_reached_groups", "총 {total}개 그룹 — 끝에 도달", { total: total.toLocaleString() })
        : _tn("dup.end_reached_groups_partial", "{shown} / {total}개 그룹 — 끝에 도달",
              { shown: loaded.toLocaleString(), total: total.toLocaleString() });
    } else {
      status.textContent = _tn("common.of_total_pages", "{shown} / {total}개 그룹",
        { shown: loaded.toLocaleString(), total: total.toLocaleString() });
    }
  }

  // ---- rendering -------------------------------------------------
  function _renderMember(m, isKeep) {
    const { parent, name } = _splitParent(m.rel_path);
    const ro = !!m.root_readonly;
    // pin: kept → ★ 유지 ; readonly → 🔒 (never a delete target) ;
    // otherwise → 삭제 대상 (clickable to promote to keep)
    let pin;
    if (isKeep) pin = `<span class="dup-legend-chip keep">${escapeHtml(_t("dup.legend_keep", "★ 유지"))}</span>`;
    else if (ro) pin = `<span class="dup-legend-chip lock">${escapeHtml(_t("dup.legend_lock", "🔒 RO"))}</span>`;
    else pin = `<span class="dup-legend-chip drop">${escapeHtml(_t("fdup.legend_drop", "→ 삭제"))}</span>`;
    const keepBtn = isKeep ? "" :
      `<button type="button" class="fdup-keep-btn" data-keep="${m.id}"
        style="font-size:11px;padding:2px 8px;margin-left:6px;background:#2a3a2a;color:#cfc;border:1px solid #3a5a3a;border-radius:4px;cursor:pointer">${escapeHtml(_t("dup.keep_pin", "유지하기"))}</button>`;
    return `<div class="fdup-member${isKeep ? " keep" : ""}${ro ? " ro" : ""}" data-id="${m.id}" data-ro="${ro ? 1 : 0}"
        style="display:flex;align-items:center;gap:8px;padding:6px 10px;border-top:1px solid #262626;font-size:12px">
      <span style="width:70px;flex:0 0 auto">${pin}</span>
      <span style="flex:0 0 auto">${iconFor(m.ext, m.mime)}</span>
      <span style="flex:1 1 auto;min-width:0">
        <span style="color:#ddd;word-break:break-all">${escapeHtml(name)}</span>
        <span style="color:#777">  ${escapeHtml(m.root_label || "")}${ro ? " 🔒" : ""} · ${escapeHtml(parent || "/")}</span>
      </span>
      <span style="flex:0 0 auto;color:#9aa">${escapeHtml(fmtSize(m.file_size))}</span>
      <span style="flex:0 0 auto;color:#778">${escapeHtml(fmtDate(m.mtime))}</span>
      ${keepBtn}
    </div>`;
  }

  function _renderGroup(g) {
    const keepId = g.files[0].id;   // oldest = suggested keep
    const membersHtml = g.files.map(m => _renderMember(m, m.id === keepId)).join("");
    const sizeEach = fmtSize(g.file_size);
    return `<div class="dup-group" data-sha="${escapeAttr(g.sha256)}" data-keep="${keepId}"
        style="border:1px solid #2b2b2b;border-radius:6px;margin:10px;overflow:hidden">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;background:#181818">
        <span style="font-size:12px;color:#aaa">
          <b style="color:#ccc">${escapeHtml(_tn("fdup.group_count", "복사본 {n}개", { n: g.count }))}</b>
          · ${escapeHtml(sizeEach)} ${escapeHtml(_t("fdup.each", "각"))}
          · <code style="color:#678">${escapeHtml((g.sha256 || "").slice(0, 12))}</code>
        </span>
        <button type="button" class="fdup-del-btn"
          style="font-size:12px;padding:4px 12px;background:#5a2a2a;color:#fff;border:1px solid #7a3a3a;border-radius:4px;cursor:pointer">
          ${escapeHtml(_t("fdup.delete_selected", "선택 삭제"))}
        </button>
      </div>
      <div class="fdup-members">${membersHtml}</div>
    </div>`;
  }

  // ---- interactions ----------------------------------------------
  function _promoteKeep(group, newKeepId) {
    group.setAttribute("data-keep", String(newKeepId));
    const keepId = Number(newKeepId);
    group.querySelectorAll(".fdup-members").forEach(() => {});
    // Re-derive each member's pin/keep state in place.
    group.querySelectorAll(".fdup-member").forEach(row => {
      const id = Number(row.getAttribute("data-id"));
      const ro = row.getAttribute("data-ro") === "1";
      const isKeep = id === keepId;
      row.classList.toggle("keep", isKeep);
      const pinSpan = row.firstElementChild;
      if (isKeep) pinSpan.innerHTML = `<span class="dup-legend-chip keep">${escapeHtml(_t("dup.legend_keep", "★ 유지"))}</span>`;
      else if (ro) pinSpan.innerHTML = `<span class="dup-legend-chip lock">${escapeHtml(_t("dup.legend_lock", "🔒 RO"))}</span>`;
      else pinSpan.innerHTML = `<span class="dup-legend-chip drop">${escapeHtml(_t("fdup.legend_drop", "→ 삭제"))}</span>`;
      // keep button: present on all non-keep rows
      let btn = row.querySelector(".fdup-keep-btn");
      if (isKeep) { if (btn) btn.remove(); }
      else if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "fdup-keep-btn";
        btn.setAttribute("data-keep", String(id));
        btn.style.cssText = "font-size:11px;padding:2px 8px;margin-left:6px;background:#2a3a2a;color:#cfc;border:1px solid #3a5a3a;border-radius:4px;cursor:pointer";
        btn.textContent = _t("dup.keep_pin", "유지하기");
        row.appendChild(btn);
      }
    });
  }

  async function _deleteGroup(group) {
    const keepId = Number(group.getAttribute("data-keep"));
    const targets = [];
    group.querySelectorAll(".fdup-member").forEach(row => {
      const id = Number(row.getAttribute("data-id"));
      const ro = row.getAttribute("data-ro") === "1";
      if (id !== keepId && !ro) targets.push(id);
    });
    if (!targets.length) {
      window.uiAlert(_t("fdup.nothing_deletable",
        "삭제할 수 있는 복사본이 없습니다 (나머지가 모두 읽기전용 폴더)."));
      return;
    }
    const ok = await window.uiConfirm(
      _tn("fdup.confirm_delete",
        "{n}개 파일을 디스크에서 영구 삭제합니다. 복구할 수 없습니다. 계속할까요?",
        { n: targets.length }),
      { danger: true });
    if (!ok) return;
    try {
      const r = await fetch("/api/files/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: targets }),
      });
      if (!r.ok) {
        let d = `HTTP ${r.status}`;
        try { d = (await r.json()).detail || d; } catch (_) {}
        _msg("err", d);
        return;
      }
      const data = await r.json().catch(() => ({}));
      _msg("ok",
        _tn("fdup.deleted", "{n}개 파일 삭제됨", { n: Number(data.deleted || 0) }));
      // Simplest correct refresh: reload the list + stats.
      load();
    } catch (e) {
      _msg("err", e.message);
    }
  }

  function _attachHandlers(scope) {
    scope.querySelectorAll(".fdup-keep-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const group = btn.closest(".dup-group");
        if (group) _promoteKeep(group, btn.getAttribute("data-keep"));
      });
    });
    scope.querySelectorAll(".fdup-del-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const group = btn.closest(".dup-group");
        if (group) _deleteGroup(group);
      });
    });
  }

  // ---- stats + inf-scroll ----------------------------------------
  async function _loadStats() {
    const el = $("#fdup-stats");
    try {
      const r = await fetch("/api/admin/file-duplicates/stats");
      if (!r.ok) { if (el) el.textContent = _t("common.error", "오류"); return; }
      const s = await r.json();
      if (el) el.innerHTML =
        `<span style="color:#ccc">${escapeHtml(_tn("fdup.stats_groups", "중복 그룹 {n}개", { n: s.groups.toLocaleString() }))}</span>
         · <span style="color:#c99">${escapeHtml(_tn("fdup.stats_rows", "여분 복사본 {n}개", { n: s.duplicate_rows.toLocaleString() }))}</span>
         · <span style="color:#9c9">${escapeHtml(_tn("fdup.stats_wasted", "낭비 용량 약 {size}", { size: fmtSize(s.wasted_bytes) }))}</span>`;
    } catch (_) { if (el) el.textContent = _t("common.error", "오류"); }
  }

  function _buildInf() {
    inf = createInfScroll({
      pageSize: PAGE_SIZE,
      isActive: () => isActive(),
      topSentinelId: "fdup-sentinel-top",
      bottomSentinelId: "fdup-sentinel",
      fetchPage: async (p) => {
        const r = await fetch(`/api/admin/file-duplicates/groups?page=${p}&page_size=${PAGE_SIZE}`);
        if (!r.ok) { const err = new Error("HTTP " + r.status); err.httpStatus = r.status; throw err; }
        const data = await r.json();
        return { items: data.items || [], total: data.total_groups || 0 };
      },
      onAppend: (items, isFirstEver) => {
        if (isFirstEver && !items.length) {
          $("#fdup-list").innerHTML =
            `<div class="empty" style="padding:14px">${escapeHtml(_t("fdup.no_duplicates", "중복 파일이 없습니다."))}</div>`;
          if (inf) inf.markBotDone();
          $("#fdup-status").textContent = "";
          return;
        }
        const wrap = document.createElement("div");
        wrap.innerHTML = items.map(_renderGroup).join("");
        $("#fdup-list").appendChild(wrap);
        _attachHandlers(wrap);
      },
      onClear: () => { $("#fdup-list").innerHTML = ""; },
      onAfterLoad: () => _refreshStatus(),
      onError: (e) => {
        const status = $("#fdup-status");
        if (status) status.textContent = e && e.httpStatus
          ? _tn("common.load_failed", "로드 실패 ({status})", { status: e.httpStatus })
          : _t("common.error", "오류");
      },
    });
  }

  // ---- public ----------------------------------------------------
  function load() {
    if (!inf) _buildInf();
    _loadedOnce = true;
    const list = $("#fdup-list");
    if (list) list.innerHTML = `<div class="empty" style="padding:14px">${escapeHtml(_t("common.loading", "불러오는 중..."))}</div>`;
    _loadStats();
    inf.start();
  }

  // Called when the 파일 tab becomes visible — lazy first load.
  function onShow() { if (!_loadedOnce) load(); }

  function init(opts) {
    isActive = (opts && opts.isActive) || (() => false);
    const rl = document.getElementById("btn-fdup-reload");
    if (rl) rl.addEventListener("click", () => load());
  }

  window.fileDuplicatesPanel = { init, load, onShow };
})();
