/*
 * Admin → Trash panel.
 *
 * Per-page usage:
 *   <script src="/js/panels/trash.js"></script>
 *   trashPanel.init({ isActive, currentUser });
 *   // tab activation:
 *   trashPanel.load();
 *   // tab deactivation:
 *   trashPanel.onHide();
 *
 * Phase 4b extraction. Owns everything trash-specific:
 *   - the trashItems[] array of currently-loaded rows
 *   - the trashSelected Set of selected photo ids
 *   - the createInfScroll instance + minimap
 *   - tile HTML / append / in-place prune
 *   - selection toggling (click + rubber-band drag)
 *   - the bulk action buttons (restore / delete / empty / select-all / clear)
 *   - the storage usage line
 *
 * Public surface (window.trashPanel):
 *   init({ isActive, currentUser })
 *     isActive    — () => bool, am I the visible tab?
 *     currentUser — admin.html's currentUser ref (read for the
 *                   admin-only "전체" deleter column)
 *   load()                 — full reload (entry from tab + reload button)
 *   jumpToFrac(frac)       — minimap callback
 *   onHide()               — hide minimap when tab is switched away
 *   clearSelectionAndReload() — used by the dup-cleanup callback that
 *                               wants a clean slate before reloading
 *
 * Dependencies (loaded as globals before this file):
 *   - $, escapeHtml, escapeAttr, _t, _tn        (/js/common.js)
 *   - api / friendlyError                         (/js/api.js)
 *   - createInfScroll                             (/js/inf-scroll.js)
 *   - createScrollMinimap, computeLogicalInfo     (/js/scroll-minimap.js)
 */
(function () {
  "use strict";

  const PAGE_SIZE = 60;

  // State (private to this module).
  const selected = new Set();
  let items = [];
  let inf = null;
  let minimap = null;
  let isActive = () => false;
  let currentUser = null;

  // ---- formatting helpers (trash-only) ---------------------------

  function _fmtBytes(n) {
    if (!n) return "0B";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + "GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + "MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + "KB";
    return n + "B";
  }

  function _updateUsageLine(data) {
    const el = $("#trash-usage");
    if (!el) return;
    const used = data.trash_bytes || 0;
    const free = data.disk_free_bytes || 0;
    const total = data.disk_total_bytes || 0;
    // Warn when free space is getting close to the 1GB safety floor
    // that _move_to_trash refuses below.
    const lowFree = free && free < 2 * 1024 ** 3;
    const usedHtml = _tn("trash.usage_used", "휴지통: <b>{bytes}</b>",
      { bytes: _fmtBytes(used) });
    el.innerHTML =
      `<span>${usedHtml}</span>` +
      (total
        ? ` · <span style="${lowFree ? 'color:#c66' : 'color:#aaa'}">` +
          _tn("trash.usage_free", "여유: {free} / {total}",
            { free: _fmtBytes(free), total: _fmtBytes(total) }) +
          (lowFree ? " ⚠" : "") +
          `</span>`
        : "");
  }

  // ---- tile rendering --------------------------------------------

  function _tileHTML(it) {
    const isSel = selected.has(it.id);
    const present = it.trash_present
      ? ""
      : `<span title="${escapeHtml(_t("trash.warn_missing_original", "원본 파일이 없습니다"))}" style="color:#c66">⚠</span>`;
    const sizeKb = it.file_size ? Math.round(it.file_size / 1024) + "KB" : "—";
    const deletedAt = it.deleted_at
      ? it.deleted_at.replace("T", " ").slice(0, 16)
      : "—";
    // 삭제자 only shown to admin in the "전체" view — when filtered
    // to the caller's own deletions it's always the caller, so adding
    // it would just be noise.
    const showDeleter = currentUser && currentUser.is_admin
      && $("#trash-scope-all")?.checked !== false
      && it.deleted_by;
    const deleter = showDeleter ? `👤 ${it.deleted_by}` : null;
    const meta = [it.root_label || "?", sizeKb, deletedAt, deleter]
      .filter(Boolean).join(" · ");
    const border = isSel ? '#3a6db0' : 'transparent';
    const check = isSel ? '#3a6db0' : 'rgba(0,0,0,.6)';
    const tick = isSel ? '✓' : '';
    return `
      <div class="trash-tile" data-id="${it.id}"
           style="position:relative;border:2px solid ${border};border-radius:6px;overflow:hidden;cursor:pointer;background:#1a1a1a">
        <div style="aspect-ratio:1/1;background:#000;overflow:hidden">
          <img src="/api/photos/${it.id}/thumb?size=256" loading="lazy" draggable="false"
               style="width:100%;height:100%;object-fit:cover;user-select:none"
               onerror="this.style.display='none';this.parentElement.innerHTML='<div style=padding:20px;color:#666;font-size:11px;text-align:center>썸네일<br>없음</div>'">
        </div>
        <div style="padding:6px 8px;font-size:11px">
          <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#eee" title="${escapeAttr(it.rel_path)}">${present} ${escapeAttr(it.filename)}</div>
          <div style="color:#888;margin-top:2px">${escapeAttr(meta)}</div>
        </div>
        <div class="trash-check" style="position:absolute;top:6px;left:6px;width:22px;height:22px;border-radius:50%;background:${check};border:2px solid #fff;display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:bold">${tick}</div>
      </div>
    `;
  }

  function _appendTiles(newItems) {
    const list = $("#trash-list");
    let grid = list.querySelector(".trash-grid");
    if (!grid) {
      list.innerHTML = `<div class="trash-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;padding:8px"></div>`;
      grid = list.querySelector(".trash-grid");
    }
    grid.insertAdjacentHTML("beforeend", newItems.map(_tileHTML).join(""));
  }

  function _prependTiles(newItems) {
    const list = $("#trash-list");
    let grid = list.querySelector(".trash-grid");
    if (!grid) {
      // No grid yet (post-jump cleared, or first ever) → build it
      // and insert via append below.
      _appendTiles(newItems);
      return;
    }
    grid.insertAdjacentHTML("afterbegin", newItems.map(_tileHTML).join(""));
  }

  function _removeTilesInPlace(ids) {
    if (!ids || !ids.length) return;
    const idSet = new Set(ids.map(Number));
    for (const id of idSet) {
      const tile = document.querySelector(`.trash-tile[data-id="${id}"]`);
      if (tile) tile.remove();
    }
    for (let i = items.length - 1; i >= 0; i--) {
      if (idSet.has(Number(items[i].id))) items.splice(i, 1);
    }
    if (inf) inf.setTotal(Math.max(0, inf.getTotal() - idSet.size));
    _refreshStatus();
    const grid = document.querySelector(".trash-grid");
    if (grid && grid.children.length === 0) {
      // Page is now empty — pull the next page so the user keeps
      // working without re-orienting from the top.
      load();
    }
  }

  function _repaintTile(id) {
    const tile = document.querySelector(`.trash-tile[data-id="${id}"]`);
    if (!tile) return;
    const isSel = selected.has(id);
    tile.style.borderColor = isSel ? '#3a6db0' : 'transparent';
    const badge = tile.querySelector('.trash-check');
    if (badge) {
      badge.style.background = isSel ? '#3a6db0' : 'rgba(0,0,0,.6)';
      badge.textContent = isSel ? '✓' : '';
    }
  }

  function _repaintAllTiles() {
    document.querySelectorAll(".trash-tile").forEach(tile => {
      _repaintTile(parseInt(tile.dataset.id, 10));
    });
  }

  // ---- status text -----------------------------------------------

  function _refreshStatus() {
    const status = $("#trash-status");
    if (!status || !inf) return;
    const total = inf.getTotal();
    const loaded = items.length;
    if (inf.isLoading()) {
      status.textContent = loaded
        ? _t("common.loading_more", "더 불러오는 중…")
        : _t("common.loading", "불러오는 중…");
      return;
    }
    if (total <= 0) { status.textContent = ""; return; }
    if (inf.isBotDone()) {
      if (loaded >= total) {
        status.textContent = _tn("trash.end_reached", "총 {total}장 — 끝에 도달",
          { total: total.toLocaleString() });
      } else {
        status.textContent = _tn("trash.end_reached_partial",
          "{shown} / {total}장 — 끝에 도달",
          { shown: loaded.toLocaleString(), total: total.toLocaleString() });
      }
    } else {
      status.textContent = _tn("trash.progress", "{shown} / {total}장",
        { shown: loaded.toLocaleString(), total: total.toLocaleString() });
    }
  }

  function _updateButtons() {
    const n = selected.size;
    $("#trash-count").textContent = n
      ? _tn("trash.selected_count", "{count}개 선택됨", { count: n })
      : _t("trash.selected_none", "선택 없음");
    $("#btn-trash-restore").disabled = n === 0;
    $("#btn-trash-delete").disabled = n === 0;
  }

  // ---- inf-scroll factory ----------------------------------------

  function _buildInf() {
    inf = createInfScroll({
      pageSize: PAGE_SIZE,
      isActive: () => isActive(),
      topSentinelId: "trash-sentinel-top",
      bottomSentinelId: "trash-sentinel",
      fetchPage: async (p) => {
        // `all=true` only meaningful for admin — server ignores it for
        // non-admin (they always see their own). Toggle checkbox is
        // hidden for non-admin so this is just defensive.
        const allParam = (currentUser && currentUser.is_admin
          && $("#trash-scope-all")?.checked !== false) ? "&all=true" : "";
        const res = await fetch(
          `/api/admin/trash?page=${p}&page_size=${PAGE_SIZE}${allParam}`,
        );
        if (!res.ok) {
          const err = new Error("HTTP " + res.status);
          err.httpStatus = res.status;
          throw err;
        }
        const data = await res.json();
        // Trash usage line gets updated from every page response since
        // the server always includes it.
        _updateUsageLine(data);
        return { items: data.items || [], total: data.total || 0 };
      },
      onAppend: (newItems, isFirstEver) => {
        if (isFirstEver && !newItems.length && items.length === 0) {
          $("#trash-list").innerHTML =
            `<div class="empty" style="padding:14px">${escapeHtml(_t("trash.empty", "휴지통이 비어있습니다."))}</div>`;
          if (inf) inf.markBotDone();
          $("#trash-status").textContent = "";
          return;
        }
        _appendTiles(newItems);
        items.push(...newItems);
      },
      onPrepend: (newItems) => {
        _prependTiles(newItems);
        items.unshift(...newItems);
      },
      onClear: () => {
        $("#trash-list").innerHTML = "";
        items = [];
      },
      onAfterLoad: () => _refreshStatus(),
      onError: (e) => {
        const status = $("#trash-status");
        if (!status) return;
        status.textContent = e && e.httpStatus
          ? _tn("common.load_failed", "로드 실패 ({status})", { status: e.httpStatus })
          : _t("common.error", "오류") + (e && e.message ? ": " + e.message : "");
      },
    });
  }

  // ---- public entry points --------------------------------------

  async function load() {
    if (!inf) _buildInf();
    $("#trash-status").textContent = _t("common.loading", "불러오는 중…");
    await inf.start();
    _refreshStatus();
    _updateButtons();
    if (minimap) { await minimap.loadHistogram(); minimap.show(); }
  }

  async function jumpToFrac(frac) {
    if (!inf || inf.getTotal() <= 0) return;
    $("#trash-status").textContent = _t("common.navigating", "이동 중…");
    await inf.jumpToFrac(frac);
    _refreshStatus();
    if (minimap) minimap.updateThumb();
  }

  function clearSelectionAndReload() {
    selected.clear();
    load();
  }

  function onHide() {
    if (minimap) minimap.hide();
  }

  // ---- init: wire DOM listeners + build minimap -----------------

  function _wireSelectionHandlers() {
    // Tile click → toggle selection (delegated; tiles are async).
    $("#trash-list").addEventListener("click", (e) => {
      const tile = e.target.closest(".trash-tile");
      if (!tile) return;
      const id = parseInt(tile.dataset.id, 10);
      if (!id) return;
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
      _repaintTile(id);
      _updateButtons();
    });
    _wireDragSelect();
    _wireActionButtons();
  }

  function _wireDragSelect() {
    // Rubber-band selection. Always additive: a drag never wipes prior
    // clicks. To start fresh, use the "선택 해제" button. Auto-scroll
    // near viewport edges so the user can lasso beyond the initial
    // viewport.
    let drag = null;
    const EDGE = 50, MAX_DELTA = 24;

    $("#trash-list").addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest("button, input")) return;
      drag = {
        startPageX: e.clientX + window.scrollX,
        startPageY: e.clientY + window.scrollY,
        curClientX: e.clientX,
        curClientY: e.clientY,
        active: false,
        box: null,
      };
    });

    function recompute() {
      if (!drag || !drag.active) return;
      const curPageX = drag.curClientX + window.scrollX;
      const curPageY = drag.curClientY + window.scrollY;
      const px1 = Math.min(drag.startPageX, curPageX);
      const py1 = Math.min(drag.startPageY, curPageY);
      const px2 = Math.max(drag.startPageX, curPageX);
      const py2 = Math.max(drag.startPageY, curPageY);

      drag.box.style.left = (px1 - window.scrollX) + "px";
      drag.box.style.top  = (py1 - window.scrollY) + "px";
      drag.box.style.width  = (px2 - px1) + "px";
      drag.box.style.height = (py2 - py1) + "px";

      document.querySelectorAll(".trash-tile").forEach(tile => {
        const r = tile.getBoundingClientRect();
        const tx1 = r.left + window.scrollX;
        const ty1 = r.top + window.scrollY;
        const tx2 = r.right + window.scrollX;
        const ty2 = r.bottom + window.scrollY;
        if (tx2 >= px1 && tx1 <= px2 && ty2 >= py1 && ty1 <= py2) {
          const id = parseInt(tile.dataset.id, 10);
          if (id && !selected.has(id)) {
            selected.add(id);
            _repaintTile(id);
          }
        }
      });
      _updateButtons();
    }

    function autoScrollTick() {
      if (!drag || !drag.active) return;
      const y = drag.curClientY;
      const h = window.innerHeight;
      let delta = 0;
      if (y < EDGE) {
        delta = -Math.round(MAX_DELTA * (1 - y / EDGE));
      } else if (y > h - EDGE) {
        delta = Math.round(MAX_DELTA * (1 - (h - y) / EDGE));
      }
      if (delta !== 0) {
        window.scrollBy(0, delta);
        recompute();
      }
      requestAnimationFrame(autoScrollTick);
    }

    document.addEventListener("mousemove", (e) => {
      if (!drag) return;
      drag.curClientX = e.clientX;
      drag.curClientY = e.clientY;
      if (!drag.active) {
        const dx = (e.clientX + window.scrollX) - drag.startPageX;
        const dy = (e.clientY + window.scrollY) - drag.startPageY;
        // 6px deadzone so a normal click doesn't morph into a 1-tile drag.
        if (Math.hypot(dx, dy) < 6) return;
        drag.active = true;
        document.body.classList.add("trash-drag-selecting");
        drag.box = document.createElement("div");
        drag.box.className = "drag-select-box";
        document.body.appendChild(drag.box);
        requestAnimationFrame(autoScrollTick);
      }
      recompute();
    });

    document.addEventListener("mouseup", () => {
      if (!drag) return;
      const wasActive = drag.active;
      if (drag.box) drag.box.remove();
      document.body.classList.remove("trash-drag-selecting");
      drag = null;
      // Eat the trailing click event the browser fires after a drag —
      // otherwise the tile under the cursor at mouseup would toggle
      // its selection a second time, undoing the drag's effect.
      if (wasActive) {
        const swallow = (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
          document.removeEventListener("click", swallow, true);
        };
        document.addEventListener("click", swallow, true);
      }
    });
  }

  function _wireActionButtons() {
    $("#btn-trash-reload").addEventListener("click", load);

    $("#btn-trash-select-all").addEventListener("click", () => {
      // "전체 선택" = every item loaded so far. To literally cover every
      // trashed photo regardless of scroll position, use "휴지통 비우기".
      for (const it of items) selected.add(it.id);
      _repaintAllTiles();
      _updateButtons();
    });

    $("#btn-trash-clear-sel").addEventListener("click", () => {
      const prev = [...selected];
      selected.clear();
      prev.forEach(_repaintTile);
      _updateButtons();
    });

    $("#btn-trash-restore").addEventListener("click", async () => {
      if (!selected.size) return;
      const ids = [...selected];
      if (!await window.uiConfirm(_tn("trash.confirm_restore",
        "{count}개 사진을 원래 위치로 복구합니다. 계속할까요?",
        { count: ids.length }))) return;
      const msg = $("#trash-msg");
      msg.className = "msg";
      msg.textContent = _t("trash.restoring", "복구 중...");
      const res = await fetch("/api/admin/trash/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo_ids: ids }),
      });
      if (!res.ok) {
        msg.className = "msg err";
        msg.textContent = _tn("trash.restore_failed",
          "복구 실패 ({status})", { status: res.status });
        return;
      }
      const d = await res.json();
      let line = _tn("trash.restore_result",
        "복구 {ok}장 · 실패 {fail}장",
        { ok: d.restored, fail: d.failed });
      if (d.failed) {
        const fails = d.results.filter(r => !r.ok).slice(0, 3)
          .map(r => `#${r.photo_id}: ${r.reason}`).join(" · ");
        line += ` — ${fails}${d.failed > 3 ? " …" : ""}`;
      }
      msg.className = d.failed ? "msg" : "msg ok";
      msg.textContent = line;
      // In-place: only the successfully-restored tiles disappear.
      // Failed ones stay selected so the admin can retry / inspect.
      const okIds = (d.results || []).filter(r => r.ok).map(r => r.photo_id);
      _removeTilesInPlace(okIds);
      okIds.forEach(id => selected.delete(id));
      _updateButtons();
    });

    $("#btn-trash-delete").addEventListener("click", async () => {
      if (!selected.size) return;
      const ids = [...selected];
      if (!await window.uiConfirm(_tn("trash.confirm_purge",
        "{count}개 사진을 영구 삭제합니다. 파일 + DB 행 + 코멘트/태그/별점 모두 지워지며 되돌릴 수 없습니다. 계속할까요?",
        { count: ids.length }), { danger: true })) return;
      const msg = $("#trash-msg");
      msg.className = "msg";
      msg.textContent = _t("trash.purging", "삭제 중...");
      const res = await fetch("/api/admin/trash/delete-permanently", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo_ids: ids }),
      });
      if (!res.ok) {
        msg.className = "msg err";
        msg.textContent = _tn("common.delete_failed",
          "삭제 실패 ({status})", { status: res.status });
        return;
      }
      const d = await res.json();
      msg.className = d.failed ? "msg" : "msg ok";
      msg.textContent = _tn("trash.purge_result",
        "영구삭제 {ok}장 · 실패 {fail}장",
        { ok: d.purged, fail: d.failed });
      if (d.purged > 0 && d.failed === 0) {
        _removeTilesInPlace(ids);
        selected.clear();
        _updateButtons();
      } else {
        // Mixed result — safest to refetch so the survivors are
        // accurately reflected.
        selected.clear();
        load();
      }
    });

    $("#btn-trash-empty").addEventListener("click", async () => {
      if (!await window.uiConfirm(_t("trash.empty_confirm_1", "휴지통의 모든 사진을 영구 삭제합니다. 되돌릴 수 없습니다. 계속할까요?"), { danger: true })) return;
      if (!await window.uiConfirm(_t("trash.empty_confirm_2", "정말로 휴지통 전체를 비우시겠습니까?"), { danger: true })) return;
      const msg = $("#trash-msg");
      msg.className = "msg"; msg.textContent = _t("trash.emptying", "비우는 중...");
      const res = await fetch("/api/admin/trash/empty", { method: "POST" });
      if (!res.ok) {
        msg.className = "msg err";
        msg.textContent = _tn("trash.empty_failed",
          "비우기 실패 ({status})", { status: res.status });
        return;
      }
      const d = await res.json();
      msg.className = d.failed ? "msg" : "msg ok";
      msg.textContent = _tn("trash.purge_result",
        "영구삭제 {ok}장 · 실패 {fail}장",
        { ok: d.purged, fail: d.failed });
      selected.clear();
      load();
    });
  }

  function init(opts) {
    isActive = (opts && opts.isActive) || (() => false);
    currentUser = (opts && opts.currentUser) || null;

    _wireSelectionHandlers();

    minimap = createScrollMinimap({
      indicatorId: "trash-scroll-indicator",
      histogramUrl: "/api/admin/trash/index-histogram",
      getActive: isActive,
      jumpToFrac,
      logicalInfoProvider: () => computeLogicalInfo(
        inf ? inf.getTotal() : 0,
        inf ? inf.getFirstOffset() : 0,
        items.length,
      ),
    });
  }

  window.trashPanel = { init, load, jumpToFrac, onHide, clearSelectionAndReload };
})();
