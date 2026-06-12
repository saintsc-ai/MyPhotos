/*
 * Admin → Shares panel.
 *
 * Per-page usage:
 *   <script src="/js/panels/shares.js"></script>
 *   sharesPanel.init({ isActive });
 *   sharesPanel.load();    // on tab activation
 *   sharesPanel.onHide();  // on tab deactivation
 *
 * Phase 4b extraction (the last of the four admin panels). Owns:
 *   - filter (status dropdown) + sort (per-column toggle) state
 *   - _sharesById cache (so the edit modal pre-fills without a fetch)
 *   - _sharesArr accumulator (kept in sync with the rendered <tbody>)
 *   - the createInfScroll instance + right-rail minimap
 *   - the toolbar (status filter, purge-inactive button, count badge)
 *   - the sortable table shell + per-row rendering
 *   - per-row action handlers (copy link, edit, revoke / hard-delete)
 *   - the edit modal (open / close / save patch)
 *
 * External dependencies (loaded as globals before this file):
 *   - $, escapeHtml, escapeAttr, _t, _tn       (/js/common.js)
 *   - api / friendlyError                        (/js/api.js)
 *   - createInfScroll                            (/js/inf-scroll.js)
 *   - createScrollMinimap, computeLogicalInfo    (/js/scroll-minimap.js)
 *   - window._fmtShareDate / _fmtShareDateShort  (inline shared util)
 *   - window.showMsg                             (inline shared util)
 *
 * Public surface (window.sharesPanel):
 *   init({ isActive })
 *   load()
 *   jumpToFrac(frac)
 *   onHide()
 */
(function () {
  "use strict";

  const PAGE_SIZE = 100;

  // State (private to this module).
  const byId = new Map();
  let arr = [];
  let sort = { col: "created_at", dir: "desc" };
  let filter = "";              // "" = all
  let inactiveCount = 0;
  let inf = null;
  let minimap = null;
  let isActive = () => false;

  // ---- helpers ---------------------------------------------------

  function _shareStatus(s) {
    // Returns both the raw Korean label (used as a filter key — the
    // server-side /page status filter accepts these literal Korean
    // strings) and a translated display label for the badge text.
    if (s.revoked) return { cls: "badge-err", label: "취소됨", display: _t("shares.status_revoked", "취소됨") };
    if (s.expires_at && new Date(s.expires_at) < new Date()) {
      return { cls: "badge-err", label: "만료됨", display: _t("shares.status_expired", "만료됨") };
    }
    if (s.max_downloads && s.download_count >= s.max_downloads) {
      return { cls: "badge-err", label: "한도소진", display: _t("shares.status_quota", "한도소진") };
    }
    return { cls: "badge-ok", label: "활성", display: _t("shares.status_active", "활성") };
  }

  function _getCols() {
    return [
      { key: "status",     label: _t("shares.col_status",    "상태"),   sortable: true,  sortKey: (s) => _shareStatus(s).label },
      { key: "owner",      label: _t("shares.col_owner",     "소유자"), sortable: true,  sortKey: (s) => (s.created_by_username || "").toLowerCase() },
      { key: "title",      label: _t("shares.col_title",     "제목"),   sortable: true,  sortKey: (s) => (s.title || "").toLowerCase() },
      { key: "count",      label: _t("shares.col_count",     "장수"),   sortable: true, num: true, sortKey: (s) => s.photo_count },
      { key: "created_at", label: _t("shares.col_created",   "생성"),   sortable: true,  sortKey: (s) => s.created_at || "" },
      { key: "expires_at", label: _t("shares.col_expires",   "만료"),   sortable: true,  sortKey: (s) => s.expires_at || "" },
      { key: "views",      label: _t("shares.col_views",     "조회"),   sortable: true, num: true, sortKey: (s) => s.view_count },
      { key: "downloads",  label: _t("shares.col_downloads", "다운"),   sortable: true, num: true, sortKey: (s) => s.download_count },
      { key: "actions",    label: "",                                  sortable: false },
    ];
  }

  function _queryString() {
    const qs = new URLSearchParams();
    if (filter) qs.set("status", filter);
    qs.set("sort", sort.col || "created_at");
    qs.set("dir", sort.dir || "desc");
    return qs.toString();
  }

  function _refreshStatus() {
    const status = $("#shares-status");
    if (!status || !inf) return;
    const total = inf.getTotal();
    const loaded = arr.length;
    if (inf.isLoading()) {
      status.textContent = loaded
        ? _t("common.loading_more", "더 불러오는 중…")
        : _t("common.loading", "불러오는 중…");
      return;
    }
    if (total <= 0) { status.textContent = ""; return; }
    if (inf.isBotDone()) {
      if (loaded >= total) {
        status.textContent = _tn("common.end_reached", "총 {total}개 — 끝에 도달",
          { total: total.toLocaleString() });
      } else {
        status.textContent = _tn("common.end_reached_partial",
          "{shown} / {total}개 — 끝에 도달",
          { shown: loaded.toLocaleString(), total: total.toLocaleString() });
      }
    } else {
      status.textContent = _tn("common.of_total", "{shown} / {total}개",
        { shown: loaded.toLocaleString(), total: total.toLocaleString() });
    }
  }

  // ---- rendering -------------------------------------------------

  function _renderToolbar(shown, total) {
    // value stays the literal Korean status key (the /page status filter
    // accepts these strings), the option label is translated for display.
    const labelMap = {
      "": _t("common.all", "(전체)"),
      "활성":     _t("shares.status_active",  "활성"),
      "만료됨":   _t("shares.status_expired", "만료됨"),
      "취소됨":   _t("shares.status_revoked", "취소됨"),
      "한도소진": _t("shares.status_quota",   "한도소진"),
    };
    const opts = ["", "활성", "만료됨", "취소됨", "한도소진"].map(v => {
      const label = labelMap[v];
      const sel = v === filter ? " selected" : "";
      return `<option value="${escapeHtml(v)}"${sel}>${escapeHtml(label)}</option>`;
    }).join("");
    const countText = filter
      ? _tn("common.of_total", "{shown} / {total}개",
          { shown: shown.toLocaleString(), total: total.toLocaleString() })
      : _tn("shares.count_total", "{total}개",
          { total: total.toLocaleString() });
    return `
      <div class="shares-toolbar">
        <span>${escapeHtml(_t("shares.toolbar_status", "상태:"))}</span>
        <select id="shares-filter">${opts}</select>
        <button type="button" id="shares-purge-inactive" class="btn-line"
                data-i18n-title="shares.purge_inactive_title"
                title="만료 / 취소 / 한도소진된 공유링크를 영구 삭제합니다">
          ${escapeHtml(_t("shares.btn_purge_inactive", "비활성 일괄 정리"))}${inactiveCount ? ` (${inactiveCount})` : ""}
        </button>
        <span class="count">${countText}</span>
      </div>`;
  }

  function _renderTableShell() {
    const heads = _getCols().map(c => {
      const align = c.num ? ' style="text-align:right"' : "";
      if (!c.sortable) return `<th${align}>${escapeHtml(c.label)}</th>`;
      const active = sort.col === c.key;
      const arrow = active ? (sort.dir === "asc" ? "▲" : "▼") : "▲";
      const cls = "sortable" + (active ? " active" : "");
      return `<th class="${cls}" data-col="${c.key}"${align}>`
           + `${escapeHtml(c.label)}<span class="sort-arrow">${arrow}</span></th>`;
    }).join("");
    return `<table class="shares-table"><thead><tr>${heads}</tr></thead><tbody></tbody></table>`;
  }

  function _ensureShell() {
    const host = $("#shares-list");
    let tbody = host.querySelector("table.shares-table tbody");
    if (tbody) return tbody;
    // Wipe whatever placeholder is there and build toolbar + table.
    host.innerHTML = _renderToolbar(arr.length, inf ? inf.getTotal() : 0)
                   + _renderTableShell();
    _attachToolbarHandlers();
    _attachSortHandlers();
    return host.querySelector("table.shares-table tbody");
  }

  function _renderRow(s) {
    const st = _shareStatus(s);
    const title = escapeHtml(s.title
      || _tn("shares.untitled_label", "(제목 없음 · {count}장)", { count: s.photo_count }));
    const owner = s.created_by_username
      ? escapeHtml(s.created_by_username)
      : (s.created_by_user_id == null
          ? _t("shares.owner_legacy", "(레거시)")
          : `user#${s.created_by_user_id}`);
    const created = `<span title="${escapeHtml(window._fmtShareDate(s.created_at))}">${escapeHtml(window._fmtShareDateShort(s.created_at))}</span>`;
    const expires = s.expires_at
      ? `<span title="${escapeHtml(window._fmtShareDate(s.expires_at))}">${escapeHtml(window._fmtShareDateShort(s.expires_at))}</span>`
      : `<span style="color:#888">—</span>`;
    const dl = s.max_downloads
      ? `${s.download_count}/${s.max_downloads}`
      : String(s.download_count);
    // Editing a revoked share makes no sense (it's already disabled on
    // the server side too), but 삭제 stays clickable so the user can
    // *purge* revoked rows — clicking on a revoked row hard-deletes
    // instead of doing a second soft-revoke no-op.
    const editDis = s.revoked ? "disabled" : "";
    const rowCls = s.revoked ? ' class="revoked"' : "";
    const deleteLabel = s.revoked
      ? _t("shares.btn_hard_delete", "영구 삭제")
      : _t("common.delete", "삭제");
    return `
      <tr${rowCls} data-id="${s.id}">
        <td><span class="badge ${st.cls}">${escapeHtml(st.display)}</span></td>
        <td>${owner}</td>
        <td class="share-title-cell" title="${title}">${title}</td>
        <td style="text-align:right">${s.photo_count.toLocaleString()}</td>
        <td>${created}</td>
        <td>${expires}</td>
        <td style="text-align:right">${s.view_count.toLocaleString()}</td>
        <td style="text-align:right">${escapeHtml(dl)}</td>
        <td class="actions">
          <a href="${s.url_path}" target="_blank" rel="noopener">${escapeHtml(_t("shares.btn_view", "보기"))}</a>
          <button type="button" class="btn-share-copy" data-url="${escapeHtml(s.url_path)}">${escapeHtml(_t("shares.btn_copy", "복사"))}</button>
          <button type="button" class="btn-share-edit" data-id="${s.id}" ${editDis}>${escapeHtml(_t("common.edit", "편집"))}</button>
          <button type="button" class="danger btn-share-revoke" data-id="${s.id}" data-revoked="${s.revoked ? 1 : 0}">${escapeHtml(deleteLabel)}</button>
        </td>
      </tr>`;
  }

  function _appendRows(items, where /* "beforeend" | "afterbegin" */) {
    const tbody = _ensureShell();
    if (!tbody) return;
    const wrap = document.createElement("tbody");
    wrap.innerHTML = items.map(_renderRow).join("");
    const newRows = Array.from(wrap.children);
    if (where === "afterbegin") {
      // Insert in reverse so the original order is preserved at the top.
      for (let i = newRows.length - 1; i >= 0; i--) {
        tbody.insertBefore(newRows[i], tbody.firstChild);
      }
    } else {
      for (const tr of newRows) tbody.appendChild(tr);
    }
    // Scope handler binding to just the newly-added rows so the
    // earlier batches don't get duplicate listeners.
    const scope = {
      querySelectorAll: (sel) => {
        const out = [];
        for (const tr of newRows) out.push(...tr.querySelectorAll(sel));
        return out;
      },
    };
    _attachRowHandlers(scope);
  }

  // ---- handlers --------------------------------------------------

  function _attachToolbarHandlers() {
    const sel = document.getElementById("shares-filter");
    if (sel) sel.addEventListener("change", () => {
      filter = sel.value;
      load();
    });
    const purgeBtn = document.getElementById("shares-purge-inactive");
    if (purgeBtn) purgeBtn.addEventListener("click", async () => {
      if (!await window.uiConfirm(_t("shares.purge_inactive_confirm", "만료 / 취소 / 한도소진된 공유링크를 영구 삭제합니다.\n복구할 수 없습니다. 계속할까요?"), { danger: true })) return;
      purgeBtn.disabled = true;
      try {
        const r = await fetch("/api/shares/purge-inactive", { method: "POST" });
        if (!r.ok) {
          window.showMsg("shares-msg", "err",
            _tn("shares.purge_failed", "정리 실패 ({status})", { status: r.status }));
          purgeBtn.disabled = false;
          return;
        }
        const d = await r.json();
        if (!d.total) {
          window.showMsg("shares-msg", "ok",
            _t("shares.purge_nothing", "정리할 항목이 없습니다."));
          purgeBtn.disabled = false;
          return;
        }
        window.showMsg("shares-msg", "ok", _tn(
          "shares.purge_result",
          "{total}개 정리 (취소 {revoked} · 만료 {expired} · 한도소진 {cap})",
          {
            total: d.total.toLocaleString(),
            revoked: d.revoked,
            expired: d.expired,
            cap: d.cap_reached,
          }));
        await load();
      } catch (e) {
        window.showMsg("shares-msg", "err",
          _t("common.network_error", "네트워크 오류"));
        purgeBtn.disabled = false;
      }
    });
  }

  function _attachSortHandlers() {
    document.querySelectorAll(".shares-table th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        const col = th.dataset.col;
        if (sort.col === col) {
          sort.dir = sort.dir === "asc" ? "desc" : "asc";
        } else {
          sort = { col, dir: "asc" };
        }
        load();
      });
    });
  }

  function _attachRowHandlers(scope) {
    const root = scope || document;
    root.querySelectorAll(".btn-share-copy").forEach(b =>
      b.addEventListener("click", () => {
        const url = new URL(b.dataset.url, location.origin).href;
        navigator.clipboard?.writeText(url).then(
          () => window.showMsg("shares-msg", "ok",
            _tn("shares.link_copied", "링크 복사됨: {url}", { url })),
          () => window.showMsg("shares-msg", "err",
            _tn("shares.copy_failed", "복사 실패 — 수동으로 선택하세요: {url}", { url })),
        );
      })
    );
    root.querySelectorAll(".btn-share-edit").forEach(b =>
      b.addEventListener("click", () => _openEditModal(parseInt(b.dataset.id, 10)))
    );
    root.querySelectorAll(".btn-share-revoke").forEach(b =>
      b.addEventListener("click", async () => {
        const id = parseInt(b.dataset.id, 10);
        const isRevoked = b.dataset.revoked === "1";
        // Active row → soft revoke (link stops working, row stays as
        // audit trail). Already-revoked row → hard delete (purge the
        // record + cascade ShareItems) so the list doesn't grow
        // forever. There's no background cleanup job.
        const msg = isRevoked
          ? _t("shares.confirm_hard_delete",
              "이 공유링크 기록을 영구 삭제합니다. (이미 취소된 상태)\n복구할 수 없습니다. 계속할까요?")
          : _t("shares.confirm_revoke",
              "이 공유링크를 취소합니다. 같은 token으로 복구할 수 없습니다. 계속할까요?");
        if (!await window.uiConfirm(msg, { danger: true })) return;
        b.disabled = true;
        try {
          const url = `/api/shares/${id}` + (isRevoked ? "?hard=true" : "");
          const r = await fetch(url, { method: "DELETE" });
          if (!r.ok) {
            const failKey = isRevoked ? "shares.hard_delete_failed" : "shares.revoke_failed";
            const failFb = isRevoked
              ? "삭제 실패 ({status})"
              : "취소 실패 ({status})";
            window.showMsg("shares-msg", "err",
              _tn(failKey, failFb, { status: r.status }));
            b.disabled = false;
            return;
          }
          if (isRevoked) {
            window.showMsg("shares-msg", "ok",
              _t("shares.hard_delete_done", "영구 삭제 완료"));
            const row = b.closest("tr, .share-row, [data-share-id]");
            if (row) row.remove();
            else load();
          } else {
            await load();
          }
        } catch (e) {
          window.showMsg("shares-msg", "err",
            _t("common.network_error", "네트워크 오류"));
          b.disabled = false;
        }
      })
    );
  }

  // ---- edit modal ------------------------------------------------

  function _toLocalInputValue(iso) {
    // datetime-local needs YYYY-MM-DDTHH:MM in the browser's local tz.
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function _localInputToIso(v) {
    if (!v) return null;
    const d = new Date(v);  // browser interprets as local tz
    if (isNaN(d)) return null;
    return d.toISOString();
  }

  function _openEditModal(id) {
    const s = byId.get(id);
    if (!s) return;
    $("#share-modal").dataset.id = String(id);
    $("#sm-title").value = s.title || "";
    $("#sm-password").value = "";  // never round-trip the hash
    $("#sm-password").placeholder = s.has_password
      ? _t("share_modal.password_keep_ph",
          "(기존 비밀번호 유지 — 비우고 저장하면 제거)")
      : _t("share_modal.password_none_ph",
          "(비우면 비밀번호 없음)");
    $("#sm-expires").value = _toLocalInputValue(s.expires_at);
    $("#sm-max-dl").value = s.max_downloads || "";
    $("#share-modal").hidden = false;
  }

  function _closeEditModal() {
    $("#share-modal").hidden = true;
  }

  function _wireEditModal() {
    $("#share-modal-close")?.addEventListener("click", _closeEditModal);
    $("#sm-cancel")?.addEventListener("click", _closeEditModal);
    $("#share-modal")?.addEventListener("click", (e) => {
      if (e.target === $("#share-modal")) _closeEditModal();
    });
    $("#sm-save")?.addEventListener("click", async () => {
      const id = parseInt($("#share-modal").dataset.id, 10);
      if (!id) return;
      // Build a patch payload: only send fields the user actually
      // touched relative to the original to avoid stomping unrelated
      // values. For 비밀번호 we always send because empty means clear.
      const orig = byId.get(id);
      const patch = {};
      const title = $("#sm-title").value.trim();
      if ((title || null) !== (orig.title || null)) patch.title = title || null;
      const pw = $("#sm-password").value;
      patch.password = pw || null;
      const expIso = _localInputToIso($("#sm-expires").value);
      if (expIso !== (orig.expires_at || null)) patch.expires_at = expIso;
      const mxRaw = $("#sm-max-dl").value;
      const mx = mxRaw ? parseInt(mxRaw, 10) : null;
      if (mx !== (orig.max_downloads || null)) patch.max_downloads = mx;
      try {
        const r = await fetch(`/api/shares/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        });
        if (!r.ok) {
          const txt = await r.text();
          alert(_tn("common.save_failed", "저장 실패 ({status})",
            { status: r.status }) + "\n" + txt);
          return;
        }
        _closeEditModal();
        await load();
      } catch (e) {
        alert(_t("common.network_error", "네트워크 오류") + ": " + e.message);
      }
    });
  }

  // ---- inf-scroll factory ----------------------------------------

  function _buildInf() {
    inf = createInfScroll({
      pageSize: PAGE_SIZE,
      isActive: () => isActive(),
      topSentinelId: "shares-sentinel-top",
      bottomSentinelId: "shares-sentinel",
      fetchPage: async (p) => {
        const qs = new URLSearchParams(_queryString());
        qs.set("page", String(p));
        qs.set("page_size", String(PAGE_SIZE));
        const r = await fetch("/api/shares/page?" + qs.toString());
        if (!r.ok) {
          const err = new Error("HTTP " + r.status);
          err.httpStatus = r.status;
          throw err;
        }
        const data = await r.json();
        inactiveCount = data.inactive_count || 0;
        return { items: data.items || [], total: data.total || 0 };
      },
      onAppend: (items, isFirstEver) => {
        const host = $("#shares-list");
        if (isFirstEver && !items.length && arr.length === 0) {
          const emptyMsg = filter
            ? _t("shares.empty_filtered", "조건에 맞는 공유링크가 없습니다.")
            : _t("shares.empty_none", "생성된 공유링크가 없습니다.");
          host.innerHTML = _renderToolbar(0, inf ? inf.getTotal() : 0)
                         + `<div class="empty" style="padding:14px">${escapeHtml(emptyMsg)}</div>`;
          _attachToolbarHandlers();
          if (inf) inf.markBotDone();
          $("#shares-status").textContent = "";
          return;
        }
        for (const s of items) byId.set(s.id, s);
        arr.push(...items);
        _appendRows(items, "beforeend");
      },
      onPrepend: (items) => {
        for (const s of items) byId.set(s.id, s);
        arr.unshift(...items);
        _appendRows(items, "afterbegin");
      },
      onClear: () => {
        $("#shares-list").innerHTML = "";
        arr = [];
        byId.clear();
      },
      onAfterLoad: () => _refreshStatus(),
      onError: (e) => {
        const status = $("#shares-status");
        if (!status) return;
        status.textContent = e && e.httpStatus
          ? _tn("common.load_failed", "로드 실패 ({status})", { status: e.httpStatus })
          : _t("common.network_error", "네트워크 오류") + (e && e.message ? ": " + e.message : "");
      },
    });
  }

  // ---- public entry points --------------------------------------

  async function load() {
    if (!inf) _buildInf();
    const host = $("#shares-list");
    host.innerHTML = `<div class="empty" style="padding:14px">${escapeHtml(_t("common.loading", "불러오는 중..."))}</div>`;
    $("#shares-status").textContent = "";
    await inf.start();
    _refreshStatus();
    if (minimap) {
      await minimap.loadHistogram(_queryString());
      minimap.show();
    }
  }

  async function jumpToFrac(frac) {
    if (!inf || inf.getTotal() <= 0) return;
    $("#shares-status").textContent = _t("common.navigating", "이동 중…");
    await inf.jumpToFrac(frac);
    _refreshStatus();
    if (minimap) minimap.updateThumb();
  }

  function onHide() {
    if (minimap) minimap.hide();
  }

  function init(opts) {
    isActive = (opts && opts.isActive) || (() => false);

    const r = $("#btn-shares-reload");
    if (r) r.addEventListener("click", load);

    _wireEditModal();

    minimap = createScrollMinimap({
      indicatorId: "shares-scroll-indicator",
      histogramUrl: "/api/shares/month-histogram",
      getActive: isActive,
      jumpToFrac,
      logicalInfoProvider: () => computeLogicalInfo(
        inf ? inf.getTotal() : 0,
        inf ? inf.getFirstOffset() : 0,
        arr.length,
      ),
    });
  }

  window.sharesPanel = { init, load, jumpToFrac, onHide };
})();
