/*
 * Admin → Duplicates panel.
 *
 * Per-page usage:
 *   <script src="/js/panels/duplicates.js"></script>
 *   duplicatesPanel.init({ isActive });
 *   duplicatesPanel.load();    // on tab activation
 *   duplicatesPanel.onHide();  // on tab deactivation
 *
 * Phase 4b extraction. Owns everything dup-specific:
 *   - the duplicate-group list + infinite-scroll instance
 *   - the right-rail year-bucket minimap
 *   - group rendering (with the "keep / drop" pin logic)
 *   - per-group trash button + in-place row removal
 *   - the 1024-px zoom modal
 *   - the server-side auto-cleanup job (enqueue, cancel, resume,
 *     poll-status + live grid diff while the job runs)
 *
 * External dependencies (loaded as globals before this file):
 *   - $, escapeHtml, escapeAttr, _t, _tn            (/js/common.js)
 *   - api / friendlyError                             (/js/api.js)
 *   - createInfScroll                                 (/js/inf-scroll.js)
 *   - createScrollMinimap, computeLogicalInfo         (/js/scroll-minimap.js)
 *   - window.fmtBytes / window._fmtShareDate /
 *     window.showMsg                                  (inline in admin.html;
 *                                                      shared with settings /
 *                                                      shares panels)
 *
 * Public surface (window.duplicatesPanel):
 *   init({ isActive })
 *   load()
 *   jumpToFrac(frac)
 *   onHide()
 */
(function () {
  "use strict";

  const PAGE_SIZE = 30;

  let inf = null;
  let minimap = null;
  let isActive = () => false;

  // Server-side cleanup job state.
  let pollTimer = null;
  let trackedJobId = null;

  // ---- DOM helpers (resolved lazily so first call works pre-load) -

  function _getZoomModal() { return document.getElementById("dup-zoom-modal"); }
  function _getZoomImg()   { return document.getElementById("dup-zoom-img"); }
  function _getZoomMeta()  { return document.getElementById("dup-zoom-meta"); }

  // ---- status text ----------------------------------------------

  function _refreshStatus() {
    const status = $("#dup-status");
    if (!status || !inf) return;
    const total = inf.getTotal();
    const loaded = $("#dup-list").querySelectorAll(".dup-group").length;
    if (inf.isLoading()) {
      status.textContent = loaded
        ? _t("common.loading_more", "더 불러오는 중…")
        : _t("common.loading", "불러오는 중…");
      return;
    }
    if (total <= 0) { status.textContent = ""; return; }
    if (inf.isBotDone()) {
      if (loaded >= total) {
        status.textContent = _tn("dup.end_reached_groups",
          "총 {total}개 그룹 — 끝에 도달",
          { total: total.toLocaleString() });
      } else {
        status.textContent = _tn("dup.end_reached_groups_partial",
          "{shown} / {total}개 그룹 — 끝에 도달",
          { shown: loaded.toLocaleString(),
            total: total.toLocaleString() });
      }
    } else {
      status.textContent = _tn("common.of_total_pages",
        "{shown} / {total}개 그룹",
        { shown: loaded.toLocaleString(),
          total: total.toLocaleString() });
    }
  }

  // ---- inf-scroll factory ----------------------------------------

  function _buildInf() {
    inf = createInfScroll({
      pageSize: PAGE_SIZE,
      isActive: () => isActive(),
      topSentinelId: "dup-sentinel-top",
      bottomSentinelId: "dup-sentinel",
      fetchPage: async (p) => {
        const r = await fetch(
          `/api/admin/duplicates/groups?page=${p}&page_size=${PAGE_SIZE}`,
        );
        if (!r.ok) {
          const err = new Error("HTTP " + r.status);
          err.httpStatus = r.status;
          throw err;
        }
        const data = await r.json();
        // factory expects `.total`; server returns `total_groups`.
        return { items: data.items || [], total: data.total_groups || 0 };
      },
      onAppend: (items, isFirstEver) => {
        if (isFirstEver && !items.length) {
          $("#dup-list").innerHTML =
            `<div class="empty" style="padding:14px">${escapeHtml(_t("dup.no_duplicates", "중복 사진이 없습니다."))}</div>`;
          if (inf) inf.markBotDone();
          $("#dup-status").textContent = "";
          return;
        }
        const wrap = document.createElement("div");
        wrap.innerHTML = items.map(_renderGroup).join("");
        $("#dup-list").appendChild(wrap);
        _attachHandlers(wrap);
      },
      onPrepend: (items) => {
        const wrap = document.createElement("div");
        wrap.innerHTML = items.map(_renderGroup).join("");
        const list = $("#dup-list");
        list.insertBefore(wrap, list.firstChild);
        _attachHandlers(wrap);
      },
      onClear: () => { $("#dup-list").innerHTML = ""; },
      onAfterLoad: () => _refreshStatus(),
      onError: (e) => {
        const status = $("#dup-status");
        if (!status) return;
        status.textContent = e && e.httpStatus
          ? _tn("common.load_failed", "로드 실패 ({status})", { status: e.httpStatus })
          : _t("common.error", "오류") + (e && e.message ? ": " + e.message : "");
      },
    });
  }

  // ---- group rendering -------------------------------------------

  function _renderGroup(g) {
    // Default keep = the first photo (server sorts by taken_at asc /
    // mtime asc, so this is the OLDEST instance — usually the
    // original). Users can click any other card to promote it.
    const keepId = g.photos[0].id;
    const previewId = g.photos[0].id;
    const previewPath = g.photos[0].rel_path;
    const previewRoot = g.photos[0].root_label || "";
    const previewV = (g.sha256 || "na").slice(0, 8);

    const _splitParent = (rel) => {
      const i = rel.lastIndexOf("/");
      return {
        parent: i >= 0 ? rel.slice(0, i + 1) : "",
        name: i >= 0 ? rel.slice(i + 1) : rel,
      };
    };
    const parts = g.photos.map(p => ({
      ..._splitParent(p.rel_path),
      rootLabel: p.root_label,
      readonly: p.root_readonly,
    }));
    const sameRoot = parts.every(x => x.rootLabel === parts[0].rootLabel);
    const sameDir = sameRoot && parts.every(x => x.parent === parts[0].parent);
    const commonHeader = sameDir
      ? `<div class="dup-common-folder">📁 <b>[${escapeHtml(parts[0].rootLabel)}]</b> /${escapeHtml(parts[0].parent)}</div>`
      : "";

    const copiesHtml = g.photos.map((p, i) => {
      const keep = i === 0;
      const cls = keep ? "dup-keep" : (p.root_readonly ? "dup-locked" : "dup-drop");
      const pinLabel = keep
        ? _t("dup.legend_keep", "★ 유지")
        : _t("dup.pin_short", "유지");
      const pinDisabled = p.root_readonly && !keep;
      const pinTitle = keep
        ? _t("dup.pin_unset_title", "다시 누르면 유지 해제 → 그룹 전체가 휴지통 대상이 됩니다")
        : _t("dup.pin_set_title", "이 위치를 유지로 지정");
      const pinDisTitle = _t("dup.pin_ro_disabled",
        "읽기전용 폴더라 트래시 대상이 될 수 없음");

      const metaParts = [];
      if (p.taken_at) metaParts.push(`📅 ${escapeHtml(window._fmtShareDate(p.taken_at))}`);
      if (p.mtime && p.mtime !== p.taken_at) {
        metaParts.push(`📂 ${escapeHtml(window._fmtShareDate(p.mtime))}`);
      }
      if (p.width && p.height) metaParts.push(`${p.width}×${p.height}`);
      if (p.camera_model) metaParts.push(`📷 ${escapeHtml(p.camera_model)}`);
      const metaInfo = metaParts.length
        ? `<span class="dup-copy-info">${metaParts.join(" · ")}</span>`
        : "";

      const roTag = p.root_readonly
        ? ` <span class="dup-tag dup-tag-locked">🔒</span>`
        : "";

      const pathHtml = sameDir
        ? `<span class="dup-copy-path">${escapeHtml(parts[i].name)}${roTag}</span>`
        : `<span class="dup-copy-path"><b>[${escapeHtml(p.root_label)}]</b>${roTag} ${escapeHtml(p.rel_path)}</span>`;

      return `
        <div class="dup-copy ${cls}" data-id="${p.id}" data-readonly="${p.root_readonly ? 1 : 0}">
          <button type="button" class="dup-pin ${keep ? 'active' : ''}"
                  data-id="${p.id}" title="${escapeHtml(pinTitle)}"
                  style="position:static"
                  ${pinDisabled ? `disabled title="${escapeHtml(pinDisTitle)}"` : ''}>
            ${escapeHtml(pinLabel)}
          </button>
          ${pathHtml}
          ${metaInfo}
        </div>`;
    }).join("");

    const sameDirNote = sameDir
      ? ` · <span style="color:#aacfff">${escapeHtml(_t("dup.same_folder", "같은 폴더"))}</span>`
      : "";
    const groupHead = _tn("dup.group_head",
      "<b>{count}</b>개 동일 · {size} · <code style=\"font-size:10px;color:#888\">{sha}…</code>",
      { count: g.count, size: window.fmtBytes(g.file_size || 0), sha: g.sha256.slice(0, 12) });
    return `
      <div class="dup-group" data-sha="${escapeHtml(g.sha256)}" data-keep-id="${keepId}">
        <div class="dup-group-head">
          <span>${groupHead}${sameDirNote}</span>
          <button type="button" class="dup-action btn-trash-group">…</button>
        </div>
        <div class="dup-group-body">
          <div class="dup-preview">
            <img src="/api/photos/${previewId}/thumb?size=256&v=${previewV}" loading="lazy" alt=""
                 onerror="this.dataset.failed=1;this.style.background='#3a1a1a';this.alt='${escapeHtml(_t("dup.thumb_missing", "썸네일 없음"))}';">
            <button type="button" class="dup-zoom" data-id="${previewId}"
                    data-path="${escapeHtml(previewPath)}"
                    data-root="${escapeHtml(previewRoot)}"
                    data-v="${previewV}"
                    title="${escapeHtml(_t("dup.zoom_in", "크게 보기"))}">🔍</button>
          </div>
          <div class="dup-copies">${commonHeader}${copiesHtml}</div>
        </div>
      </div>`;
  }

  function _refreshGroupAction(group) {
    const keepId = parseInt(group.dataset.keepId, 10);
    const hasKeep = Number.isFinite(keepId) && keepId > 0;
    const photos = group.querySelectorAll(".dup-copy");
    const ids = [];
    let skippedReadonly = 0;
    for (const el of photos) {
      const id = parseInt(el.dataset.id, 10);
      if (hasKeep && id === keepId) continue;
      if (el.dataset.readonly === "1") {
        skippedReadonly += 1;
        continue;
      }
      ids.push(id);
    }
    const btn = group.querySelector(".btn-trash-group");
    btn.dataset.ids = ids.join(",");
    btn.dataset.hasKeep = hasKeep ? "1" : "0";
    let label, disabled, title;
    if (ids.length === 0) {
      label = skippedReadonly > 0
        ? _tn("dup.action_ro_only", "읽기전용 폴더라 정리 불가 ({n}개)", { n: skippedReadonly })
        : _t("dup.action_nothing", "정리 대상 없음");
      disabled = true;
      title = skippedReadonly > 0
        ? _t("dup.action_ro_hint",
            "사진 폴더 탭에서 해당 root의 RO 토글을 끄고 다시 시도하세요")
        : "";
    } else if (!hasKeep) {
      label = skippedReadonly > 0
        ? _tn("dup.action_full_with_ro",
            "전체 {n}개 휴지통으로 (읽기전용 {ro}개 제외)",
            { n: ids.length, ro: skippedReadonly })
        : _tn("dup.action_full",
            "전체 {n}개 휴지통으로", { n: ids.length });
      disabled = false;
      title = _t("dup.action_full_title",
        "유지 선택이 해제된 상태입니다 — 그룹의 모든 사진이 휴지통으로 이동합니다");
    } else {
      label = skippedReadonly > 0
        ? _tn("dup.action_extras_with_ro",
            "유지 외 {n}개 휴지통으로 (읽기전용 {ro}개 제외)",
            { n: ids.length, ro: skippedReadonly })
        : _tn("dup.action_extras",
            "유지 외 {n}개 휴지통으로", { n: ids.length });
      disabled = false;
      title = "";
    }
    btn.textContent = label;
    btn.disabled = disabled;
    if (title) btn.setAttribute("title", title); else btn.removeAttribute("title");
  }

  function _setKeep(group, newKeepId) {
    const kid = parseInt(newKeepId, 10);
    const hasKeep = Number.isFinite(kid) && kid > 0;
    group.dataset.keepId = hasKeep ? String(kid) : "0";
    group.querySelectorAll(".dup-copy").forEach(el => {
      const id = parseInt(el.dataset.id, 10);
      const keep = hasKeep && (id === kid);
      el.classList.toggle("dup-keep", keep);
      if (!keep) {
        el.classList.toggle("dup-drop", el.dataset.readonly !== "1");
        el.classList.toggle("dup-locked", el.dataset.readonly === "1");
      } else {
        el.classList.remove("dup-drop");
        el.classList.remove("dup-locked");
      }
      const pin = el.querySelector(".dup-pin");
      if (pin) {
        pin.classList.toggle("active", keep);
        pin.textContent = keep
          ? _t("dup.legend_keep", "★ 유지")
          : _t("dup.pin_set", "유지하기");
        pin.title = keep
          ? _t("dup.pin_unset_title",
              "다시 누르면 유지 해제 → 그룹 전체가 휴지통 대상이 됩니다")
          : _t("dup.pin_set_title", "이 사진을 유지로 지정");
      }
    });
    _refreshGroupAction(group);
  }

  function _attachHandlers(scope) {
    const root = scope || document;
    root.querySelectorAll(".dup-group").forEach(_refreshGroupAction);

    root.querySelectorAll(".dup-pin").forEach(pin =>
      pin.addEventListener("click", () => {
        if (pin.disabled) return;
        const group = pin.closest(".dup-group");
        if (!group) return;
        const id = parseInt(pin.dataset.id, 10);
        if (!id) return;
        const currentKeep = parseInt(group.dataset.keepId, 10);
        if (currentKeep === id) {
          _setKeep(group, 0);          // toggle OFF — 0-keep mode
        } else {
          _setKeep(group, id);
        }
      })
    );

    root.querySelectorAll(".dup-zoom").forEach(z =>
      z.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = parseInt(z.dataset.id, 10);
        if (!id) return;
        _openZoom(id, z.dataset.path || "", z.dataset.root || "", z.dataset.v || "");
      })
    );

    root.querySelectorAll(".btn-trash-group").forEach(btn => {
      btn.addEventListener("click", async () => {
        const ids = btn.dataset.ids.split(",").map(s => parseInt(s, 10)).filter(Boolean);
        if (!ids.length) return;
        const fullSweep = btn.dataset.hasKeep === "0";
        const promptMsg = fullSweep
          ? _tn("dup.confirm_full_sweep",
              "⚠ 유지 선택이 없습니다. 이 그룹의 {count}개 모두 휴지통으로 옮깁니다. 계속할까요?",
              { count: ids.length })
          : _tn("dup.confirm_drop_extras",
              "이 그룹의 {count}개를 휴지통으로 옮깁니다. 계속할까요?",
              { count: ids.length });
        if (!await window.uiConfirm(promptMsg, { danger: true })) return;
        const group = btn.closest(".dup-group");
        const origLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = _t("dup.processing", "처리 중…");
        try {
          const r = await fetch("/api/photos/bulk-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ photo_ids: ids }),
          });
          if (!r.ok) {
            alert(_tn("common.delete_failed", "삭제 실패 ({status})", { status: r.status }));
            btn.disabled = false;
            btn.textContent = origLabel;
            return;
          }
          const data = await r.json();
          _removeRowsFromGroup(group, data.ids || []);
          const skipped = (data.skipped_readonly || []).length;
          const failed = (data.failed || []).length;
          if (skipped || failed) {
            const parts = [_tn("dup.moved_to_trash",
              "{n}개 휴지통으로 이동", { n: data.deleted || 0 })];
            if (skipped) parts.push(_tn("dup.skipped_readonly",
              "{n}개 읽기전용으로 건너뜀", { n: skipped }));
            if (failed) {
              const first = (data.failed || [])[0];
              const reason = first && first.reason
                ? first.reason
                : _t("indexing.fp_trash_reason_unknown", "원인 불명");
              parts.push(_tn("dup.failed_with_reason",
                "{n}개 실패 ({reason})", { n: failed, reason }));
            }
            alert(parts.join(" · "));
          }
        } catch (e) {
          alert(_t("common.network_error", "네트워크 오류") + ": " + e.message);
          btn.disabled = false;
          btn.textContent = origLabel;
        }
      });
    });
  }

  function _removeRowsFromGroup(group, removedIds) {
    if (!group) return;
    const removedSet = new Set((removedIds || []).map(Number));
    group.querySelectorAll(".dup-copy").forEach(row => {
      const id = parseInt(row.dataset.id, 10);
      if (removedSet.has(id)) row.remove();
    });
    const remaining = group.querySelectorAll(".dup-copy").length;
    const wholeGroupGone = remaining < 2;
    if (wholeGroupGone) {
      group.remove();
      const pager = document.getElementById("dup-pager");
      if (pager) {
        pager.innerHTML = pager.innerHTML.replace(
          /총\s+(\d+(?:,\d+)*)개 그룹/,
          (_, n) => {
            const cur = parseInt(n.replace(/,/g, ""), 10);
            return `총 ${Math.max(0, cur - 1).toLocaleString()}개 그룹`;
          },
        );
      }
    } else {
      const head = group.querySelector(".dup-group-head span");
      if (head) {
        head.innerHTML = head.innerHTML.replace(
          /<b>\d+<\/b>개 동일/,
          `<b>${remaining}</b>개 동일`,
        );
      }
      const currentKeep = parseInt(group.dataset.keepId, 10);
      if (Number.isFinite(currentKeep) && removedSet.has(currentKeep)) {
        const first = group.querySelector(".dup-copy");
        if (first) _setKeep(group, parseInt(first.dataset.id, 10));
      } else {
        _refreshGroupAction(group);
      }
    }
    const list = document.getElementById("dup-list");
    const stillVisible = list ? list.querySelectorAll(".dup-group").length : 0;
    if (stillVisible === 0) {
      load();
    }
  }

  // ---- zoom modal ------------------------------------------------

  function _openZoom(photoId, relPath, rootLabel, v) {
    const modal = _getZoomModal();
    const img = _getZoomImg();
    const meta = _getZoomMeta();
    if (!modal || !img || !meta) return;
    const vq = v ? `&v=${v}` : "";
    img.onerror = () => {
      img.alt = _t("dup.zoom_load_failed",
        "원본 미리보기를 불러올 수 없습니다 (삭제됐거나 권한/네트워크 문제)");
      img.style.background = "#3a1a1a";
    };
    img.src = `/api/photos/${photoId}/thumb?size=1024${vq}`;
    meta.textContent = rootLabel ? `[${rootLabel}]  ${relPath}` : relPath;
    modal.classList.add("show");
  }

  function _closeZoom() {
    const modal = _getZoomModal();
    const img = _getZoomImg();
    if (!modal || !img) return;
    modal.classList.remove("show");
    // Drop the src so a giant image isn't held in memory after close.
    img.removeAttribute("src");
  }

  function _wireZoomModal() {
    const modal = _getZoomModal();
    if (!modal) return;
    modal.addEventListener("click", (e) => {
      if (e.target === modal) _closeZoom();
    });
    document.getElementById("dup-zoom-close")?.addEventListener("click", _closeZoom);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && modal.classList.contains("show")) {
        _closeZoom();
      }
    });
  }

  // ---- auto-cleanup (server-side job) ---------------------------

  async function _autoCleanup() {
    const sRes = await fetch("/api/admin/duplicates/stats");
    if (!sRes.ok) {
      alert(_t("dup.alert_stats_unavailable",
        "통계를 불러오지 못해 자동정리를 시작할 수 없습니다."));
      return;
    }
    const stats = await sRes.json();
    if (!stats.duplicate_rows) {
      alert(_t("dup.alert_no_duplicates", "정리할 중복이 없습니다."));
      return;
    }
    const ok = await window.uiConfirm(_tn("dup.confirm_auto_cleanup",
      "중복 그룹 {groups}개에서 각 그룹의 가장 오래된 1개만 남기고\n" +
      "총 {rows}개를 휴지통(data/trash/)으로 옮깁니다.\n\n" +
      "이 작업은 백그라운드에서 진행됩니다. 페이지를 떠도 계속 처리되며,\n" +
      "다시 들어오면 진행률을 확인할 수 있습니다.\n\n" +
      "(기준: 촬영시각 → mtime → 폴더/경로 순서. 다른 사진을 유지하려면\n" +
      "자동정리 대신 그룹별 카드의 '유지하기' 핀을 누르세요.)\n\n" +
      "복구는 휴지통 탭에서 가능합니다. 계속할까요?",
      { groups: stats.groups.toLocaleString(),
        rows: stats.duplicate_rows.toLocaleString() }));
    if (!ok) return;

    try { localStorage.removeItem("dup-cleanup-dismissed-job"); } catch (_) {}
    const r = await fetch("/api/admin/duplicates/auto-cleanup", { method: "POST" });
    if (!r.ok) {
      window.showMsg("dup-msg", "err",
        _tn("dup.start_failed", "자동정리 시작 실패 ({status})", { status: r.status }));
      return;
    }
    const d = await r.json();
    if (d.status === "already_running") {
      window.showMsg("dup-msg", "ok",
        _t("dup.already_running", "이미 자동정리가 진행 중입니다"));
    } else {
      window.showMsg("dup-msg", "ok",
        _t("dup.started_bg", "자동정리를 시작했습니다 — 백그라운드에서 처리됩니다"));
    }
    trackedJobId = d.job_id;
    _schedulePoll(500);
  }

  async function _cancelCleanup() {
    if (!await window.uiConfirm(_t("dup.pause_confirm",
      "자동정리를 일시 중지할까요?\n(이미 휴지통으로 옮긴 사진은 그대로 유지되며, '이어서 정리'로 남은 그룹부터 재개할 수 있습니다.)"))) return;
    const r = await fetch("/api/admin/duplicates/auto-cleanup/cancel", { method: "POST" });
    if (!r.ok) {
      window.showMsg("dup-msg", "err",
        _tn("dup.pause_failed", "중지 실패 ({status})", { status: r.status }));
      return;
    }
    _schedulePoll(300);
  }

  async function _resumeCleanup() {
    try { localStorage.removeItem("dup-cleanup-dismissed-job"); } catch (_) {}
    const r = await fetch("/api/admin/duplicates/auto-cleanup", { method: "POST" });
    if (!r.ok) {
      window.showMsg("dup-msg", "err",
        _tn("dup.resume_failed", "재개 실패 ({status})", { status: r.status }));
      return;
    }
    const d = await r.json();
    trackedJobId = d.job_id;
    if (d.status === "already_running") {
      window.showMsg("dup-msg", "ok",
        _t("dup.already_running", "이미 자동정리가 진행 중입니다"));
    } else {
      window.showMsg("dup-msg", "ok",
        _t("dup.resume_started", "자동정리를 이어서 시작했습니다"));
    }
    _schedulePoll(500);
  }

  function _closeCleanupBox() {
    const box = $("#dup-cleanup-status");
    if (!box) return;
    try {
      if (trackedJobId) {
        localStorage.setItem("dup-cleanup-dismissed-job", String(trackedJobId));
      }
    } catch (_) {}
    box.style.display = "none";
  }

  function _fmtCleanupTs(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const pad = n => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function _schedulePoll(delayMs) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(_cleanupTick, delayMs == null ? 1500 : delayMs);
  }
  function _stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function _renderCleanupStatus(d) {
    const box       = $("#dup-cleanup-status");
    const bar       = $("#dup-cleanup-bar");
    const labelEl   = $("#dup-cleanup-label");
    const detail    = $("#dup-cleanup-detail");
    const cancelBtn = $("#btn-dup-cleanup-cancel");
    const resumeBtn = $("#btn-dup-cleanup-resume");
    const closeBtn  = $("#btn-dup-cleanup-close");
    if (!box) return;
    if (!d || d.status === "idle") { box.style.display = "none"; return; }

    try {
      const dismissed = parseInt(localStorage.getItem("dup-cleanup-dismissed-job") || "0", 10);
      if (dismissed === d.job_id && ["done","cancelled","failed"].includes(d.status)) {
        box.style.display = "none";
        return;
      }
    } catch (_) {}

    box.style.display = "";
    const total = d.progress_total || 0;
    const done  = d.progress_done  || 0;
    const pct   = total > 0 ? Math.min(100, Math.floor((done / total) * 100)) : 0;
    bar.style.width = `${pct}%`;
    bar.style.background =
      d.status === "failed"    ? "#a83a3a" :
      d.status === "cancelled" ? "#7a6a3a" :
      d.status === "done"      ? "#3a7a4a" :
                                 "#3b6cb0";

    const keys = {
      queued:    "dup.cleanup_queued",
      running:   "dup.cleanup_running",
      done:      "dup.cleanup_done",
      cancelled: "dup.cleanup_cancelled",
      failed:    "dup.cleanup_failed",
    };
    labelEl.textContent = keys[d.status] ? i18n.t(keys[d.status]) : d.status;

    const detailParts = [];
    if (total > 0) {
      detailParts.push(`${done.toLocaleString()} / ${total.toLocaleString()}장 (${pct}%)`);
    } else if (done > 0) {
      detailParts.push(`${done.toLocaleString()}장 처리됨`);
    }
    if (d.started_at) detailParts.push(`시작 ${_fmtCleanupTs(d.started_at)}`);
    if (d.finished_at && ["done","cancelled","failed"].includes(d.status)) {
      detailParts.push(`종료 ${_fmtCleanupTs(d.finished_at)}`);
    }
    if (d.last_error) {
      detailParts.push(`<span style="color:#f99">오류: ${escapeHtml(d.last_error)}</span>`);
    }
    detail.innerHTML = detailParts.join(" · ");

    const isLive    = d.status === "queued" || d.status === "running";
    const canResume = d.status === "cancelled" || d.status === "failed";
    cancelBtn.style.display = isLive    ? "" : "none";
    resumeBtn.style.display = canResume ? "" : "none";
    closeBtn.style.display  = isLive    ? "none" : "";
  }

  async function _cleanupTick() {
    pollTimer = null;
    let d;
    try {
      const r = await fetch("/api/admin/duplicates/cleanup-status");
      if (!r.ok) { _schedulePoll(); return; }
      d = await r.json();
    } catch { _schedulePoll(); return; }
    _renderCleanupStatus(d);

    const isLive = d.status === "queued" || d.status === "running";
    const justFinished =
      trackedJobId && d.job_id === trackedJobId && !isLive;

    if (isLive) {
      trackedJobId = d.job_id;
      const autoBtn = $("#btn-dup-auto");
      if (autoBtn) autoBtn.disabled = true;
      await _diffAndPruneGroups();
      _schedulePoll();
      return;
    }

    const autoBtn = $("#btn-dup-auto");
    if (autoBtn) autoBtn.disabled = false;

    if (justFinished) {
      const skipMsg = d.last_error
        ? ` — ${escapeHtml(d.last_error)}`
        : "";
      const done = (d.progress_done || 0).toLocaleString();
      if (d.status === "done") {
        window.showMsg("dup-msg", "ok",
          _tn("dup.finished_done", "자동정리 완료 — {done}장 처리됨", { done }));
      } else if (d.status === "cancelled") {
        window.showMsg("dup-msg", "ok",
          _tn("dup.finished_cancelled", "자동정리 취소됨 — {done}장까지 처리", { done }));
      } else {
        window.showMsg("dup-msg", "err",
          _tn("dup.finished_failed",
            "자동정리 실패{err} — {done}장까지 처리",
            { err: skipMsg, done }));
      }
      await load();
    }
    trackedJobId = null;
    _stopPoll();
  }

  async function _diffAndPruneGroups() {
    const list = document.getElementById("dup-list");
    if (!list) return;
    const nodes = Array.from(list.querySelectorAll(".dup-group[data-sha]"));
    if (!nodes.length) return;
    const wanted = Math.min(100, Math.max(nodes.length, 20));
    let liveShas;
    try {
      const r = await fetch(`/api/admin/duplicates/groups?page=1&page_size=${wanted}`);
      if (!r.ok) return;
      const data = await r.json();
      liveShas = new Set((data.items || []).map(g => g.sha256));
    } catch { return; }
    nodes.forEach((node, i) => {
      if (i >= wanted) return;
      const sha = node.getAttribute("data-sha");
      if (sha && !liveShas.has(sha)) node.remove();
    });
    try {
      const sr = await fetch("/api/admin/duplicates/stats");
      if (sr.ok) {
        const s = await sr.json();
        const stats = document.getElementById("dup-stats");
        if (stats) {
          stats.innerHTML =
            `중복 그룹 <b>${(s.groups||0).toLocaleString()}</b>개 · ` +
            `중복 행 <b>${(s.duplicate_rows||0).toLocaleString()}</b>개 · ` +
            `정리 시 회수 가능 (원본 폴더 기준) <b>${window.fmtBytes(s.wasted_bytes||0)}</b>`;
        }
      }
    } catch {}
  }

  async function _startPollIfLive() {
    try {
      const r = await fetch("/api/admin/duplicates/cleanup-status");
      if (!r.ok) return;
      const d = await r.json();
      trackedJobId = d.job_id || null;
      _renderCleanupStatus(d);
      const isLive = d.status === "queued" || d.status === "running";
      const autoBtn = $("#btn-dup-auto");
      if (autoBtn) autoBtn.disabled = isLive;
      if (isLive) _schedulePoll(800);
    } catch {}
  }

  // ---- public entry points --------------------------------------

  async function load() {
    // Stats first — independent of the list.
    try {
      const sRes = await fetch("/api/admin/duplicates/stats");
      if (!sRes.ok) {
        const failMsg = _tn("indexing.stats_load_failed_status",
          "통계 로드 실패 ({status})", { status: sRes.status });
        $("#dup-stats").innerHTML = `<span style="color:#f99">${escapeHtml(failMsg)}</span>`;
      } else {
        const s = await sRes.json();
        $("#dup-stats").innerHTML = _tn("dup.stats_line",
          "중복 그룹 <b>{groups}</b>개 · 중복 행 <b>{rows}</b>개 · 정리 시 회수 가능 (원본 폴더 기준) <b>{bytes}</b>",
          {
            groups: s.groups.toLocaleString(),
            rows: s.duplicate_rows.toLocaleString(),
            bytes: window.fmtBytes(s.wasted_bytes),
          });
      }
    } catch (e) {
      $("#dup-stats").innerHTML = `<span style="color:#f99">${escapeHtml(_t("common.network_error", "네트워크 오류"))}</span>`;
    }
    if (!inf) _buildInf();
    $("#dup-status").textContent = _t("common.loading", "불러오는 중…");
    await inf.start();
    _refreshStatus();
    _startPollIfLive();
    if (minimap) { await minimap.loadHistogram(); minimap.show(); }
  }

  async function jumpToFrac(frac) {
    if (!inf || inf.getTotal() <= 0) return;
    $("#dup-status").textContent = _t("common.navigating", "이동 중…");
    await inf.jumpToFrac(frac);
    _refreshStatus();
    if (minimap) minimap.updateThumb();
  }

  function onHide() {
    if (minimap) minimap.hide();
  }

  function init(opts) {
    isActive = (opts && opts.isActive) || (() => false);

    // Action button handlers (wired once at init).
    const r = $("#btn-dup-reload");
    if (r) r.addEventListener("click", load);
    const a = $("#btn-dup-auto");
    if (a) a.addEventListener("click", _autoCleanup);
    const cb = $("#btn-dup-cleanup-cancel");
    if (cb) cb.addEventListener("click", _cancelCleanup);
    const rb = $("#btn-dup-cleanup-resume");
    if (rb) rb.addEventListener("click", _resumeCleanup);
    const xb = $("#btn-dup-cleanup-close");
    if (xb) xb.addEventListener("click", _closeCleanupBox);

    _wireZoomModal();

    minimap = createScrollMinimap({
      indicatorId: "dup-scroll-indicator",
      histogramUrl: "/api/admin/duplicates/year-histogram",
      getActive: isActive,
      jumpToFrac,
      logicalInfoProvider: () => computeLogicalInfo(
        inf ? inf.getTotal() : 0,
        inf ? inf.getFirstOffset() : 0,
        document.querySelectorAll("#dup-list .dup-group").length
      ),
    });
  }

  window.duplicatesPanel = { init, load, jumpToFrac, onHide };
})();
