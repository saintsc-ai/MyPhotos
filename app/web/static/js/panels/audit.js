/*
 * Admin → Audit log panel.
 *
 * Per-page usage:
 *   <script src="/js/panels/audit.js"></script>
 *   auditPanel.init({ isActive: () => currentTab() === "audit" });
 *   // tab activation:
 *   auditPanel.load();
 *   // tab deactivation:
 *   auditPanel.onHide();
 *
 * Extracted from admin.html (Phase 4b). Owns:
 *   - filter state (user / action / resource_type)
 *   - users cache (single fetch on first load)
 *   - the createInfScroll instance (page cursors + observers)
 *   - the createScrollMinimap instance (right-rail histogram)
 *   - row-level rendering (table shell + each <tr>)
 *   - DOMContentLoaded button handlers (reload / apply filter / purge)
 *
 * Dependencies (all loaded as globals before this file):
 *   - $, escapeHtml, escapeAttr, _t, _tn        (/js/common.js)
 *   - api / friendlyError                         (/js/api.js)
 *   - createInfScroll                             (/js/inf-scroll.js)
 *   - createScrollMinimap, computeLogicalInfo     (/js/scroll-minimap.js)
 *
 * Public surface (window.auditPanel):
 *   init({ isActive })   — wire button handlers + build minimap.
 *                          isActive: () => bool (am I the visible tab?)
 *   load()               — public entrypoint; lazy-loads the users
 *                          dropdown then resets + reloads page 1
 *   jumpToFrac(frac)     — minimap callback; jumps to the page
 *                          containing the fractional offset
 *   onHide()             — call when tab is switched away so the
 *                          minimap goes dormant
 */
(function () {
  "use strict";

  const PAGE_SIZE = 100;
  const ACTIONS = [
    "photo.trash", "photo.restore", "photo.purge", "photo.visibility",
    "share.create", "share.revoke", "share.purge",
    "acl.root.set", "acl.root.delete",
    "acl.folder.set", "acl.folder.delete",
  ];

  let filter = { user_id: "", action: "", resource_type: "" };
  let usersCache = null;
  let inf = null;
  let minimap = null;
  let isActive = () => false;     // set by init()

  function _filterQS() {
    const qs = new URLSearchParams();
    if (filter.user_id)       qs.set("user_id", filter.user_id);
    if (filter.action)        qs.set("action", filter.action);
    if (filter.resource_type) qs.set("resource_type", filter.resource_type);
    return qs;
  }

  function _refreshStatus() {
    const status = $("#audit-status");
    if (!status || !inf) return;
    const total = inf.getTotal();
    const loaded = $("#audit-list").querySelectorAll("tbody tr").length;
    if (inf.isLoading()) {
      status.textContent = loaded
        ? _t("common.loading_more", "더 불러오는 중…")
        : _t("common.loading", "불러오는 중…");
      return;
    }
    if (total <= 0) {
      status.textContent = _t("audit.total_zero", "총 0건");
      return;
    }
    if (inf.isBotDone()) {
      if (loaded >= total) {
        status.textContent = _tn("audit.end_reached", "총 {total}건 — 끝에 도달",
          { total: total.toLocaleString() });
      } else {
        status.textContent = _tn("audit.end_reached_partial",
          "{shown} / {total}건 — 끝에 도달",
          { shown: loaded.toLocaleString(), total: total.toLocaleString() });
      }
    } else {
      status.textContent = _tn("audit.progress", "{shown} / {total}건",
        { shown: loaded.toLocaleString(), total: total.toLocaleString() });
    }
  }

  function _ensureShell() {
    const host = $("#audit-list");
    if (host.querySelector("tbody")) return host.querySelector("tbody");
    host.innerHTML = `<table style="width:100%;font-size:12px">
      <thead><tr>
        <th>${escapeHtml(_t("audit.col_time", "시각"))}</th>
        <th>${escapeHtml(_t("audit.col_user", "사용자"))}</th>
        <th>${escapeHtml(_t("audit.col_action", "액션"))}</th>
        <th>${escapeHtml(_t("audit.col_resource", "리소스"))}</th>
        <th>${escapeHtml(_t("audit.col_detail", "상세"))}</th>
      </tr></thead>
      <tbody></tbody></table>`;
    return host.querySelector("tbody");
  }

  function _fmtTs(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} `
         + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function _rowsHtml(items) {
    return items.map(it => {
      const detail = it.detail
        ? (typeof it.detail === "string" ? it.detail : JSON.stringify(it.detail))
        : "";
      return `
        <tr>
          <td style="white-space:nowrap;color:#888">${escapeHtml(_fmtTs(it.ts))}</td>
          <td>${escapeHtml(it.username || "(unknown)")}</td>
          <td><code>${escapeHtml(it.action)}</code></td>
          <td><code>${escapeHtml(it.resource_type)}</code>${
              it.resource_id ? " #" + escapeHtml(String(it.resource_id)) : ""}</td>
          <td style="font-size:11px;color:#aaa;word-break:break-all">${escapeHtml(detail)}</td>
        </tr>`;
    }).join("");
  }

  function _buildInf() {
    inf = createInfScroll({
      pageSize: PAGE_SIZE,
      isActive: () => isActive(),
      topSentinelId: "audit-sentinel-top",
      bottomSentinelId: "audit-sentinel",
      fetchPage: async (p) => {
        const qs = _filterQS();
        qs.set("page", String(p));
        qs.set("page_size", String(PAGE_SIZE));
        const r = await fetch("/api/admin/audit?" + qs.toString());
        if (!r.ok) {
          const err = new Error("HTTP " + r.status);
          err.httpStatus = r.status;
          throw err;
        }
        const data = await r.json();
        return { items: data.items || [], total: data.total || 0 };
      },
      onAppend: (items, isFirstEver) => {
        const host = $("#audit-list");
        if (isFirstEver && !items.length && !host.querySelector("tbody")) {
          host.innerHTML = `<div class="empty" style="padding:14px">${escapeHtml(_t("audit.no_records", "기록 없음"))}</div>`;
          if (inf) inf.markBotDone();
          $("#audit-status").textContent = _t("audit.total_zero", "총 0건");
          return;
        }
        const tbody = _ensureShell();
        tbody.insertAdjacentHTML("beforeend", _rowsHtml(items));
      },
      onPrepend: (items) => {
        const tbody = _ensureShell();
        tbody.insertAdjacentHTML("afterbegin", _rowsHtml(items));
      },
      onClear: () => { $("#audit-list").innerHTML = ""; },
      onAfterLoad: () => _refreshStatus(),
      onError: (e) => {
        const status = $("#audit-status");
        if (!status) return;
        status.textContent = e && e.httpStatus
          ? _tn("common.load_failed", "로드 실패 ({status})", { status: e.httpStatus })
          : _t("common.error", "오류") + (e && e.message ? ": " + e.message : "");
      },
    });
  }

  async function load() {
    if (usersCache === null) {
      try {
        const r = await fetch("/api/admin/users");
        usersCache = r.ok ? await r.json() : [];
      } catch (_) { usersCache = []; }
      const userSel = $("#audit-user");
      const allLabel = _t("common.all", "(전체)");
      userSel.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>`
        + usersCache.map(u =>
            `<option value="${u.id}">${escapeHtml(u.username)}</option>`
          ).join("");
      const actSel = $("#audit-action");
      actSel.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>`
        + ACTIONS.map(a =>
            `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`
          ).join("");
    }
    await _resetAndReload();
  }

  async function _resetAndReload() {
    if (!inf) _buildInf();
    $("#audit-list").innerHTML = `<div class="empty" style="padding:14px">${escapeHtml(_t("common.loading", "불러오는 중..."))}</div>`;
    $("#audit-status").textContent = "";
    await inf.start();
    _refreshStatus();
    if (minimap) { await minimap.loadHistogram(_filterQS().toString()); minimap.show(); }
  }

  async function jumpToFrac(frac) {
    if (!inf || inf.getTotal() <= 0) return;
    $("#audit-status").textContent = _t("common.navigating", "이동 중…");
    await inf.jumpToFrac(frac);
    _refreshStatus();
    if (minimap) minimap.updateThumb();
  }

  function init(opts) {
    isActive = (opts && opts.isActive) || (() => false);

    // Button handlers
    const reload = $("#btn-audit-reload");
    if (reload) reload.addEventListener("click", load);

    const apply = $("#btn-audit-apply");
    if (apply) apply.addEventListener("click", () => {
      filter = {
        user_id: $("#audit-user").value,
        action: $("#audit-action").value,
        resource_type: $("#audit-rtype").value,
      };
      _resetAndReload();
    });

    const purge = $("#btn-audit-purge");
    if (purge) purge.addEventListener("click", async () => {
      if (!await window.uiConfirm(_t("audit.purge_confirm",
        "90일 이전 활동 로그를 영구 삭제합니다. 계속할까요?"), { danger: true })) return;
      purge.disabled = true;
      try {
        const r = await fetch("/api/admin/audit/purge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ days: 90 }),
        });
        if (!r.ok) {
          alert(_tn("audit.purge_failed", "정리 실패 ({status})",
            { status: r.status }));
          return;
        }
        const d = await r.json();
        alert(_tn("audit.purge_done", "{count}건 삭제됨",
          { count: d.deleted.toLocaleString() }));
        await load();
      } finally {
        purge.disabled = false;
      }
    });

    // Right-rail minimap. Lives entirely inside the panel now.
    minimap = createScrollMinimap({
      indicatorId: "audit-scroll-indicator",
      histogramUrl: "/api/admin/audit/month-histogram",
      getActive: isActive,
      jumpToFrac,
      logicalInfoProvider: () => computeLogicalInfo(
        inf ? inf.getTotal() : 0,
        inf ? inf.getFirstOffset() : 0,
        document.querySelectorAll("#audit-list tbody tr").length,
      ),
    });
  }

  function onHide() {
    if (minimap) minimap.hide();
  }

  window.auditPanel = { init, load, jumpToFrac, onHide };
})();
