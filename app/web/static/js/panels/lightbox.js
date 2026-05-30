/*
 * Lightbox module — the modal viewer used by every gallery surface
 * (timeline / map / folder / topic / topic-tag / map-cell, …).
 *
 * Extracted from index.html (Phase 4c). Owns:
 *
 *   STATE
 *     - lightboxList / lightboxIndex / lightboxPhoto / lightboxFromMap
 *     - _liveActive (Live Photo toggle, per-photo, reset on every nav)
 *     - detailsVisible (persisted to localStorage)
 *     - myCurrentRating, currentTags, allTagsCache (per-photo cache)
 *     - _dupesReqId, detailsReqSeq (stale-response guards)
 *
 *   DOM
 *     #lightbox + every child (lb-img, lb-video, lb-info, lb-prev/next,
 *     lb-strip, lb-close, lb-live-toggle, lb-dupes-*, lb-info-toggle,
 *     lb-details, lb-details-body, lb-visibility-toggle, menu-download,
 *     menu-convert, menu-share, menu-rotate-cw/ccw/180, menu-delete,
 *     lb-stars, lb-rating-meta, lb-comment-*, lb-desc, lb-tags,
 *     lb-tag-input, lb-tag-suggest, lb-auto-tags-*, date-modal + form).
 *
 *   BEHAVIOUR
 *     open / close, prev / next (←/→/swipe), Esc-close, I-toggle,
 *     details panel (filename, taken_at, camera, …), inline date editor,
 *     rating stars, comments thread (add / edit / delete), description
 *     editor, tag chips with autocomplete, auto-tag chips, duplicate
 *     popover with admin "trash others" action, EXIF rotation, soft
 *     delete, Live-Photo MOV toggle, visibility cycle, filmstrip.
 *
 * Dependencies (loaded as globals before this file):
 *   - $, escapeAttr, _t, _tn       (/js/common.js)
 *   - api / friendlyError           (/js/api.js)
 *
 * Public surface (window.lightbox):
 *   init(deps)                      — wire DOM + handlers (call once
 *                                     on DOMContentLoaded). deps:
 *     getCurrentUser()              — null | {id, is_admin, …}
 *     getAppConfig()                — {map_nearby_radius_deg, _limit}
 *     thumbUrl(p, size)             — URL builder (sha-prefix cache bust)
 *     fmtTime(iso)                  — display formatter
 *     getTimelinePhotos()           — the photos[] window the gallery
 *                                     is currently showing; same ref
 *                                     across calls
 *     getTimelineTotalCount()       — totalCount (for "X / Y" counter)
 *     getTimelineDone()             — true when the timeline has loaded
 *                                     every page; gates showNext fetch
 *     loadTimelineMore()            — async; fetch one more page
 *     onPhotoDeletedFromTimeline(i) — caller updates grid, counters,
 *                                     histogram. Lightbox only mutates
 *                                     its own list/index after.
 *     openShareModal(ids)           — caller's share modal
 *     onTagChipClick(name)          — caller mutates filter state +
 *                                     applyFilters; lightbox closes
 *                                     itself after the callback.
 *
 *   openAt(index)                   — timeline entrypoint
 *   openForPhotoId(id)              — map / dupes / topic entrypoint
 *                                     (fetches /api/photos/nearby)
 *   openWithList(list, idx, opts)   — generic; opts.fromMap=true makes
 *                                     showNext NOT try to load more
 *   close()                         — close + reset state
 *   isOpen()                        — bool
 *   shiftIndex(K)                   — called by loadPrevPage so the
 *                                     index keeps pointing at the same
 *                                     photo after the array prepends.
 *                                     No-op when the active list isn't
 *                                     the timeline photos[].
 */
(function () {
  "use strict";

  // --- Constants --------------------------------------------------
  const STRIP_THUMB_SIZE = 72;      // px, matches .lb-strip-item width/height
  const STRIP_GAP = 4;              // px, matches .lb-strip gap
  const LB_VOL_KEY = "myphotos-lb-vol";
  const LB_MUTED_KEY = "myphotos-lb-vol-muted";
  const DETAILS_KEY = "myphotos-details-visible";
  const BROWSER_SAFE = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);
  const SWIPE_PX = 50;
  const _VIS_NEXT = { inherit: "private", private: "public", public: "inherit" };
  const _VIS_ICON = { inherit: "🔓", private: "🔒", public: "🌐" };

  // --- State ------------------------------------------------------
  let lightboxList = [];
  let lightboxIndex = -1;
  let lightboxPhoto = null;
  let lightboxFromMap = false;
  let _liveActive = false;
  let detailsVisible = false;
  try { detailsVisible = localStorage.getItem(DETAILS_KEY) === "1"; } catch (_) {}

  let _dupesReqId = 0;
  let detailsReqSeq = 0;
  let myCurrentRating = null;
  let currentTags = [];
  let allTagsCache = null;
  let suggestActive = -1;
  let _lbTouch = null;

  // --- DOM refs (resolved in init) --------------------------------
  let lb, lbImg, lbVideo, lbInfo, lbPrev, lbNext, lbStrip;
  let lbInfoToggle, lbDetails, lbDetailsBody;
  let lbDupesWrap, lbDupesBtn, lbDupesPop;
  let lbStars, lbRatingMeta;
  let lbCommentList, lbCommentCount, lbCommentForm, lbCommentInput;
  let lbDesc, lbTags, lbTagInput, lbTagSuggest;
  let dateModal;
  // Compat shim — earlier inline code had a real menu drop-down that
  // got replaced with flat icon buttons. Keep the no-op so any stray
  // .classList.remove("show") still works without an extra null guard.
  const lbMenu = { classList: { remove() {}, add() {}, toggle() {}, contains() { return false; } }, contains() { return false; } };

  // --- Deps -------------------------------------------------------
  let _deps = {};
  function _user() { return _deps.getCurrentUser ? _deps.getCurrentUser() : null; }
  function _cfg()  { return _deps.getAppConfig   ? _deps.getAppConfig()   : {}; }
  function _photos() { return _deps.getTimelinePhotos ? _deps.getTimelinePhotos() : []; }
  function _total()  { return _deps.getTimelineTotalCount ? _deps.getTimelineTotalCount() : 0; }
  function _done()   { return _deps.getTimelineDone ? _deps.getTimelineDone() : true; }
  function _thumb(p, size) { return _deps.thumbUrl(p, size); }
  function _fmtTime(iso)   { return _deps.fmtTime ? _deps.fmtTime(iso) : ""; }

  // --- Video prefs (volume + muted persist across photos) ---------
  function _loadVideoPrefs() {
    try {
      const v = parseFloat(localStorage.getItem(LB_VOL_KEY));
      if (!isNaN(v) && v >= 0 && v <= 1) lbVideo.volume = v;
      lbVideo.muted = localStorage.getItem(LB_MUTED_KEY) === "1";
    } catch (_) { /* private mode / disabled storage */ }
  }
  function _saveVideoPrefs() {
    try {
      localStorage.setItem(LB_VOL_KEY, String(lbVideo.volume));
      localStorage.setItem(LB_MUTED_KEY, lbVideo.muted ? "1" : "0");
    } catch (_) { /* ignore */ }
  }

  function _resetLightboxVideo() {
    try {
      lbVideo.pause();
      lbVideo.removeAttribute("src");
      lbVideo.load();           // forces release of the prior source
    } catch (_) { /* element may not have had a source */ }
  }

  // --- Open / close -----------------------------------------------
  function closeLightbox() {
    lb.classList.remove("show", "lb-isolated");
    _resetLightboxVideo();
    _liveActive = false;
    lightboxList = [];
    lightboxIndex = -1;
    lightboxPhoto = null;
    lightboxFromMap = false;
  }

  function isOpen() { return !!(lb && lb.classList.contains("show")); }

  function _normalizeExt(p) {
    let ext = (p.ext || "").toLowerCase();
    if (!ext) {
      const m = (p.filename || "").match(/\.([a-z0-9]+)$/i);
      if (m) ext = "." + m[1].toLowerCase();
    } else if (!ext.startsWith(".")) {
      ext = "." + ext;
    }
    return ext;
  }

  function updateMenuTargets() {
    const p = lightboxPhoto;
    if (!p) return;
    const ext = _normalizeExt(p);
    const base = (p.filename || "image").replace(/\.[^.]+$/, "");
    const dl = $("#menu-download");
    dl.href = `/api/photos/${p.id}/download?format=original`;
    dl.setAttribute("download", p.filename || "");
    const cv = $("#menu-convert");
    cv.href = `/api/photos/${p.id}/download?format=png`;
    cv.setAttribute("download", base + ".png");
    const showConvert = (p.media_kind === "image") && !BROWSER_SAFE.has(ext);
    cv.style.display = showConvert ? "" : "none";
  }

  function _renderLightbox() {
    const p = lightboxPhoto;
    if (!p) return;
    lb.classList.remove("lb-isolated");

    // Live Photo toggle visibility — show only when this is a still
    // (image) that has a companion video paired to it.
    const isLive = p.media_kind === "image" && p.companion_id != null;
    const liveBtn = $("#lb-live-toggle");
    if (isLive) {
      liveBtn.style.display = "";
      liveBtn.classList.toggle("active", _liveActive);
      liveBtn.textContent = _liveActive
        ? _t("lb.live_stop", "■ 정지")
        : _t("lb.live_play", "▶ Live");
    } else {
      liveBtn.style.display = "none";
      _liveActive = false;
    }

    if (isLive && _liveActive) {
      _resetLightboxVideo();
      lbImg.removeAttribute("src");
      lbImg.style.display = "none";
      lbVideo.style.display = "";
      lbVideo.poster = _thumb(p, 1024);
      lbVideo.src = `/api/photos/${p.companion_id}/original`;
      lbVideo.autoplay = true;
    } else if (p.media_kind === "video") {
      _resetLightboxVideo();
      lbImg.removeAttribute("src");
      lbImg.style.display = "none";
      lbVideo.style.display = "";
      lbVideo.poster = _thumb(p, 1024);
      lbVideo.src = `/api/photos/${p.id}/original`;
    } else {
      _resetLightboxVideo();
      lbVideo.style.display = "none";
      lbImg.style.display = "";
      lbImg.src = _thumb(p, 1024);
    }

    const dims = (p.width && p.height) ? `${p.width}×${p.height}` : "";
    const total = _total();
    const counter = lightboxFromMap
      ? _tn("lb.counter_nearby", "{idx} / {count} (이 근처)",
            { idx: lightboxIndex + 1, count: lightboxList.length })
      : `${lightboxIndex + 1} / ${total > 0 ? total.toLocaleString() : _photos().length}`;
    lbInfo.innerHTML = [
      escapeAttr(p.filename || ""), _fmtTime(p.taken_at), escapeAttr(p.camera_model || ""),
      dims, counter
    ].filter(Boolean).join(" · ");
    lb.classList.add("show");
    updateMenuTargets();
    lbMenu.classList.remove("show");
    renderFilmstrip();
    updateLightboxNav();
    if (detailsVisible) loadDetails(p.id);
    loadDuplicates(p.id);
  }

  // --- Duplicates popover -----------------------------------------
  async function loadDuplicates(photoId) {
    lbDupesWrap.style.display = "none";
    lbDupesPop.hidden = true;
    lbDupesPop.innerHTML = "";
    const myReq = ++_dupesReqId;
    try {
      const r = await fetch(`/api/photos/${photoId}/duplicates`);
      if (!r.ok) return;
      const arr = await r.json();
      if (myReq !== _dupesReqId) return;   // user advanced while we waited
      if (!Array.isArray(arr) || arr.length === 0) return;
      lbDupesBtn.textContent = _tn("lb.dupes_badge",
        "⚏ 중복 {count}", { count: arr.length });
      lbDupesWrap.style.display = "";
      const otherIds = arr.map(d => d.id);
      const u = _user();
      const trashOthersLabel = _tn("lb.trash_dupes_except_label",
        "이 사진만 남기고 나머지 {count}개 휴지통으로", { count: arr.length });
      const headerHtml = (u && u.is_admin)
        ? `<div class="lb-dupes-header"><button type="button" class="lb-dupes-action" id="lb-dupes-trash-others">${escapeAttr(trashOthersLabel)}</button></div>`
        : "";
      lbDupesPop.innerHTML = headerHtml + arr.map(d => {
        const t = d.taken_at ? d.taken_at.replace("T", " ").slice(0, 16) : "";
        return `<div class="lb-dupe-item" data-id="${d.id}">` +
          `<div class="root">[${escapeAttr(d.root_label)}]` +
          (t ? ` · ${escapeAttr(t)}` : "") + `</div>` +
          `<div class="path">${escapeAttr(d.rel_path)}</div>` +
          `</div>`;
      }).join("");
      lbDupesPop.querySelectorAll(".lb-dupe-item").forEach(el => {
        el.addEventListener("click", () => {
          const id = parseInt(el.dataset.id, 10);
          if (!id) return;
          lbDupesPop.hidden = true;
          openForPhotoId(id);
        });
      });
      const trashBtn = lbDupesPop.querySelector("#lb-dupes-trash-others");
      if (trashBtn) {
        trashBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const ok = confirm(_tn("lb.confirm_delete_dupes_except_this",
            "이 사진은 그대로 두고, 다른 위치의 {count}개를 휴지통(data/trash/)으로 옮깁니다.\n계속할까요?",
            { count: otherIds.length }));
          if (!ok) return;
          trashBtn.disabled = true;
          trashBtn.textContent = _t("dup.processing", "처리 중…");
          try {
            let body;
            try {
              body = await api.post("/api/photos/bulk-delete",
                { photo_ids: otherIds },
                { action: _t("common.delete", "삭제") });
            } catch (err) {
              alert(err.message);
              trashBtn.disabled = false;
              trashBtn.textContent = trashOthersLabel;
              return;
            }
            body = body || {};
            const skipped = (body.skipped_readonly || []).length;
            if (skipped > 0) {
              alert(
                `${body.deleted || 0}장은 휴지통으로 이동했고, ` +
                `${skipped}장은 read-only 폴더라 그대로 두었습니다.\n` +
                `(관리 → 사진 폴더에서 readonly를 풀면 이 항목들도 정리 가능)`
              );
            }
            lbDupesPop.hidden = true;
            if (skipped === 0) {
              lbDupesWrap.style.display = "none";
            } else if (lightboxPhoto && lightboxPhoto.id) {
              loadDuplicates(lightboxPhoto.id);
            }
          } catch (err) {
            alert(_t("common.network_error", "네트워크 오류") + ": " + err.message);
            trashBtn.disabled = false;
          }
        });
      }
    } catch (_) { /* offline / fetch error — keep chip hidden */ }
  }

  // --- Navigation -------------------------------------------------
  function openLightboxByIndex(idx) {
    if (idx < 0 || idx >= lightboxList.length) return;
    // Reset Live Photo play state on every nav.
    _liveActive = false;
    lightboxIndex = idx;
    lightboxPhoto = lightboxList[idx];
    _renderLightbox();
    _updateVisibilityToggle(lightboxPhoto);
  }

  function openAt(index) {
    const photos = _photos();
    if (index < 0 || index >= photos.length) return;
    lightboxList = photos;
    lightboxFromMap = false;
    openLightboxByIndex(index);
  }

  async function openForPhotoId(id) {
    let list = null, idx = -1;
    try {
      const cfg = _cfg();
      const radius = cfg.map_nearby_radius_deg ?? 0.005;
      const limit = cfg.map_nearby_limit ?? 100;
      const r = await fetch(
        `/api/photos/nearby?photo_id=${id}&radius_deg=${radius}&limit=${limit}`
      );
      if (r.ok) {
        const arr = await r.json();
        const i = arr.findIndex(p => p.id === id);
        if (i >= 0) { list = arr; idx = i; }
      }
    } catch (_) { /* network — fall through to single-photo fallback */ }

    if (!list) {
      try {
        const r = await fetch(`/api/photos/${id}`);
        if (!r.ok) { alert(await friendlyError(r, "사진 정보 조회")); return; }
        list = [await r.json()];
        idx = 0;
      } catch (e) { alert(_t("common.network_error", "네트워크 오류") + ": " + e.message); return; }
    }

    lightboxList = list;
    lightboxFromMap = true;
    openLightboxByIndex(idx);
  }

  function openWithList(list, idx, opts) {
    if (!Array.isArray(list) || !list.length) return;
    lightboxList = list;
    lightboxFromMap = !!(opts && opts.fromMap);
    if (idx < 0 || idx >= list.length) idx = 0;
    openLightboxByIndex(idx);
  }

  function showPrev() {
    if (lightboxIndex > 0) openLightboxByIndex(lightboxIndex - 1);
  }
  function showNext() {
    if (lightboxIndex < lightboxList.length - 1) {
      openLightboxByIndex(lightboxIndex + 1);
    } else if (!lightboxFromMap && !_done() && _deps.loadTimelineMore) {
      // Timeline mode at the tail of the loaded window — fetch more
      // and advance once they arrive. Map mode's list is fixed.
      const target = lightboxIndex + 1;
      _deps.loadTimelineMore().then(() => {
        if (target < lightboxList.length) openLightboxByIndex(target);
        else updateLightboxNav();
      });
    }
  }

  function updateLightboxNav() {
    lbPrev.disabled = lightboxIndex <= 0;
    const atEnd = lightboxIndex >= lightboxList.length - 1;
    // In map mode the nearby list is fixed; in timeline mode we can
    // still load more pages, so only disable next once `done` says
    // we're at the true end of the catalog.
    lbNext.disabled = atEnd && (lightboxFromMap || _done());
  }

  function shiftIndex(K) {
    // Called by the gallery's loadPrevPage after a top-prepend so the
    // lightbox keeps pointing at the same photo. Only meaningful when
    // the lightbox is iterating over the timeline window; the
    // map-nearby list is its own array and never moves.
    if (lightboxList === _photos() && lightboxIndex >= 0) {
      lightboxIndex += K;
    }
  }

  // --- Visibility cycle (P4) --------------------------------------
  function _visLabel(vis) {
    const fallbacks = {
      inherit: "공개 범위: 상속 (ACL 따름)",
      private: "공개 범위: 비공개 (본인만)",
      public:  "공개 범위: 공개 (모두 보임)",
    };
    return _t("lb.visibility_" + vis, fallbacks[vis] || "공개 범위");
  }

  function _updateVisibilityToggle(p) {
    const btn = $("#lb-visibility-toggle");
    if (!btn) return;
    const u = _user();
    const isAdmin = u && u.is_admin;
    const isOwner = u && p && p.owner_user_id === u.id;
    if (!p || (!isAdmin && !isOwner)) {
      btn.style.display = "none";
      return;
    }
    const vis = (p && p.visibility) || "inherit";
    btn.style.display = "";
    btn.textContent = _VIS_ICON[vis] || "🔓";
    btn.title = _visLabel(vis);
    btn.dataset.vis = vis;
  }

  // --- Remove (delete / dupes-trash) ------------------------------
  function removeCurrentPhoto() {
    const idx = lightboxIndex;
    if (idx < 0 || idx >= lightboxList.length) return;
    const fromTimeline = (lightboxList === _photos());
    if (fromTimeline) {
      // Outer code splices photos[], removes the tile, shifts data-idx
      // on neighbours, decrements totalCount/loadedCount, re-renders
      // controls + scroll indicator, schedules a histogram refresh.
      // The list reference is the same array so its .length already
      // shrank by 1 when we read it below.
      if (_deps.onPhotoDeletedFromTimeline) _deps.onPhotoDeletedFromTimeline(idx);
    } else {
      lightboxList.splice(idx, 1);
    }
    if (lightboxList.length === 0) {
      closeLightbox();
    } else {
      openLightboxByIndex(Math.min(idx, lightboxList.length - 1));
    }
  }

  // --- Details panel ----------------------------------------------
  function toggleDetails() {
    detailsVisible = !detailsVisible;
    lbInfoToggle.classList.toggle("active", detailsVisible);
    lbDetails.classList.toggle("show", detailsVisible);
    try { localStorage.setItem(DETAILS_KEY, detailsVisible ? "1" : "0"); } catch (_) {}
    if (detailsVisible && lightboxPhoto) {
      loadDetails(lightboxPhoto.id);
    }
  }

  function fmtBytes(b) {
    if (b == null) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0, n = b;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
  }

  async function loadDetails(photoId) {
    const my = ++detailsReqSeq;
    lbDetailsBody.innerHTML = `<dd style="color:#666">${escapeAttr(_t("common.loading", "불러오는 중..."))}</dd>`;
    myCurrentRating = null;
    paintStars(null);
    lbRatingMeta.textContent = "";
    lbCommentList.innerHTML = "";
    lbCommentCount.textContent = "";
    lbDesc.classList.add("empty");
    lbDesc.textContent = _t("lb.desc_add", "설명 추가...");
    currentTags = [];
    lbTags.querySelectorAll(".lb-tag-chip").forEach(el => el.remove());
    lbTagInput.value = "";
    lbTagSuggest.classList.remove("show");
    const autoWrap = $("#lb-auto-tags-wrap");
    if (autoWrap) { autoWrap.hidden = true; }
    const autoRow = $("#lb-auto-tags");
    if (autoRow) { autoRow.innerHTML = ""; }
    let d;
    try {
      const res = await fetch(`/api/photos/${photoId}/details`);
      if (!res.ok) { lbDetailsBody.innerHTML = `<dd style="color:#a66">${escapeAttr(_tn("common.load_failed", "로드 실패 ({status})", { status: res.status }))}</dd>`; return; }
      d = await res.json();
    } catch (e) {
      if (my !== detailsReqSeq) return;
      lbDetailsBody.innerHTML = `<dd style="color:#a66">${escapeAttr(_t("common.network_error", "네트워크 오류"))}</dd>`; return;
    }
    if (my !== detailsReqSeq) return;
    renderDetails(d);
  }

  function renderDetails(d) {
    const writeBlocked = d && d.writable === false;
    const blockReason = (d && d.write_block_reason)
      || (d && d.root_readonly ? _t("lb.root_readonly_reason", "이 폴더는 읽기 전용 모드입니다") : "");
    const _setLockable = (sel, normalTitle) => {
      const btn = document.querySelector(sel);
      if (!btn) return;
      if (writeBlocked) {
        btn.disabled = true;
        btn.title = blockReason || _t("lb.not_writable", "이 사진은 변경할 수 없습니다");
      } else {
        btn.disabled = false;
        btn.title = normalTitle;
      }
    };
    _setLockable("#menu-delete",     _t("lb.delete_to_trash", "삭제 (휴지통으로 이동)"));
    _setLockable("#menu-rotate-cw",  _t("lb.rotate_cw", "시계방향 90° 회전 (EXIF만 변경, 손실 없음)"));
    _setLockable("#menu-rotate-ccw", _t("lb.rotate_ccw", "반시계방향 90° 회전 (EXIF만 변경, 손실 없음)"));
    _setLockable("#menu-rotate-180", _t("lb.rotate_180", "180° 회전 / 반바퀴 (EXIF만 변경, 손실 없음)"));

    const rows = [];
    const push = (label, value, html = false) => {
      if (value === null || value === undefined || value === "") return;
      rows.push(`<dt>${escapeAttr(label)}</dt><dd>${html ? value : escapeAttr(String(value))}</dd>`);
    };
    push(_t("lb.field_filename", "파일명"), d.filename);
    const dateText = d.taken_at ? d.taken_at.replace("T", " ").slice(0, 19) : _t("lb.field_none", "(없음)");
    const reverted = d.taken_at_original
      ? `<span class="reverted-hint">${_t("lb.field_original", "원래:")} ${escapeAttr(d.taken_at_original.replace("T", " ").slice(0, 19))}</span>`
      : "";
    push(
      _t("lb.field_taken_at", "촬영시각"),
      `${escapeAttr(dateText)} <button type="button" class="edit-icon" data-role="edit-date" title="${escapeAttr(_t("lb.edit_date", "날짜 편집"))}">✎</button>${reverted}`,
      true
    );
    // File mtime — useful when taken_at is empty (날짜 없음 photos)
    // and as a sanity check otherwise. Stored as Photo.mtime by the
    // scanner; for files that haven't been modified after import, this
    // is effectively the file's creation date on disk.
    if (d.mtime) {
      push(_t("lb.field_file_mtime", "파일 생성일자"),
        d.mtime.replace("T", " ").slice(0, 19));
    }
    if (d.width && d.height) push(_t("lb.field_dimensions", "크기"), `${d.width} × ${d.height}`);
    push(_t("lb.field_file_size", "파일 크기"), fmtBytes(d.file_size));
    push(_t("lb.field_kind", "종류"), `${d.media_kind || ""}${d.ext ? " (" + d.ext + ")" : ""}`);
    const cam = [d.camera_make, d.camera_model].filter(Boolean).join(" ");
    push(_t("lb.field_camera", "카메라"), cam);
    push(_t("lb.field_lens", "렌즈"), d.lens);
    const shot = [];
    if (d.fnumber) shot.push(`f/${d.fnumber}`);
    if (d.exposure) shot.push(d.exposure + (/\d+\/\d+/.test(d.exposure) ? "s" : ""));
    if (d.iso) shot.push(`ISO ${d.iso}`);
    if (d.focal_length) shot.push(`${d.focal_length}mm`);
    if (shot.length) push(_t("lb.field_exposure", "노출"), shot.join(" · "));
    if (d.duration_seconds) push(_t("lb.field_duration", "영상 길이"), `${d.duration_seconds.toFixed(1)}s`);
    if (d.latitude != null && d.longitude != null) {
      const lat = d.latitude.toFixed(6), lng = d.longitude.toFixed(6);
      push(
        _t("lb.field_gps", "GPS"),
        `${lat}, ${lng}` +
        ` · <a href="https://www.openstreetmap.org/?mlat=${lat}&mlon=${lng}#map=15/${lat}/${lng}" target="_blank">${escapeAttr(_t("lb.map_link", "지도"))}</a>`,
        true
      );
    }
    push(_t("lb.field_path", "경로"), d.rel_path);
    if (d.owner_user_id != null) {
      push(_t("lb.field_uploader", "올린 사람"), d.owner_username || `#${d.owner_user_id}`);
    } else {
      push(_t("lb.field_uploader", "올린 사람"), _t("lb.field_uploader_unset", "(미지정)"));
    }
    if (d.sha256) push(_t("lb.field_sha256", "SHA-256"), `<code>${escapeAttr(d.sha256)}</code>`, true);
    push(_t("lb.field_indexed_at", "인덱스됨"), d.indexed_at ? d.indexed_at.replace("T", " ").slice(0, 19) : null);
    push(_t("lb.field_exif_extractor", "EXIF 추출기"), d.exif_extractor);
    lbDetailsBody.innerHTML = rows.length ? rows.join("") : `<dd style="color:#666">${escapeAttr(_t("lb.no_info", "정보 없음"))}</dd>`;

    const editBtn = lbDetailsBody.querySelector('[data-role="edit-date"]');
    if (editBtn) editBtn.addEventListener("click", () => openDateModal(d));

    renderDescription(d.description || "");
    renderTags(d.tags || []);
    renderAutoTags(d.auto_tags || []);
    renderRating(d.my_rating, d.rating_avg, d.rating_count);
    renderComments(d.comments || []);
  }

  // --- Rating -----------------------------------------------------
  function renderRating(myRating, avg, count) {
    myCurrentRating = myRating || null;
    paintStars(myCurrentRating);
    const parts = [];
    if (avg != null) {
      parts.push(`평균 ${avg.toFixed(1)} (${count}명)`);
    } else {
      parts.push(_t("lb.no_rating_yet", "아직 평가 없음"));
    }
    if (myCurrentRating) {
      parts.push(`<a class="clear" data-role="rating-clear">해제</a>`);
    }
    lbRatingMeta.innerHTML = parts.join(" · ");
    const clearBtn = lbRatingMeta.querySelector('[data-role="rating-clear"]');
    if (clearBtn) clearBtn.addEventListener("click", () => setRating(null));
  }

  function paintStars(filledUpTo) {
    lbStars.querySelectorAll(".lb-star").forEach(el => {
      const v = parseInt(el.dataset.v, 10);
      el.classList.toggle("filled", filledUpTo != null && v <= filledUpTo);
      el.classList.remove("preview");
    });
  }

  async function setRating(value) {
    if (!lightboxPhoto) return;
    const pid = lightboxPhoto.id;
    try {
      await api.put(`/api/photos/${pid}/rating`, { rating: value },
                    { action: _t("lb.rating_action", "평점 저장") });
    } catch (e) {
      alert(e.message);
      return;
    }
    myCurrentRating = value;
    loadDetails(pid);
  }

  // --- Comments ---------------------------------------------------
  function renderComments(comments) {
    lbCommentCount.textContent = comments.length ? `(${comments.length})` : "";
    if (!comments.length) {
      lbCommentList.innerHTML = `<div class="lb-comment-empty">${escapeAttr(_t("lb.no_comments_yet", "아직 댓글이 없습니다."))}</div>`;
      return;
    }
    const editLabel = escapeAttr(_t("common.edit", "수정"));
    const deleteLabel = escapeAttr(_t("common.delete", "삭제"));
    const editedSuffix = " · " + _t("lb.comment_edited", "수정됨");
    const deletedUserLabel = _t("lb.comment_deleted_user", "(삭제된 사용자)");
    lbCommentList.innerHTML = comments.map(c => {
      const user = c.username || deletedUserLabel;
      const when = (c.updated_at || c.created_at).replace("T", " ").slice(0, 16);
      const edited = c.updated_at && c.updated_at !== c.created_at ? editedSuffix : "";
      const actions = c.can_edit
        ? `<div class="lb-comment-actions">
             <button data-role="edit" data-id="${c.id}">${editLabel}</button>
             <button data-role="delete" data-id="${c.id}" class="danger">${deleteLabel}</button>
           </div>`
        : "";
      return `
        <div class="lb-comment" data-id="${c.id}">
          <div class="lb-comment-head">
            <span class="lb-comment-user">${escapeAttr(user)}</span>
            <span class="lb-comment-time">${escapeAttr(when)}${edited}</span>
            ${actions}
          </div>
          <div class="lb-comment-body" data-role="body">${escapeAttr(c.body)}</div>
        </div>
      `;
    }).join("");
    lbCommentList.querySelectorAll('[data-role="edit"]').forEach(b => {
      b.addEventListener("click", () => startEditComment(parseInt(b.dataset.id, 10)));
    });
    lbCommentList.querySelectorAll('[data-role="delete"]').forEach(b => {
      b.addEventListener("click", () => deleteComment(parseInt(b.dataset.id, 10)));
    });
  }

  function startEditComment(commentId) {
    const row = lbCommentList.querySelector(`.lb-comment[data-id="${commentId}"]`);
    if (!row) return;
    const bodyEl = row.querySelector('[data-role="body"]');
    const current = bodyEl.textContent;
    bodyEl.innerHTML = `
      <textarea style="width:100%;min-height:50px;background:#222;color:#eee;
        border:1px solid #333;border-radius:4px;padding:8px;font-size:13px;
        font-family:inherit;outline:none;box-sizing:border-box"
        data-role="edit-input">${escapeAttr(current)}</textarea>
      <div style="display:flex;gap:6px;margin-top:6px;justify-content:flex-end">
        <button class="cancel" data-role="edit-cancel"
          style="background:#2a2a2a;color:#ddd;border:1px solid #3a3a3a;
          border-radius:4px;padding:5px 12px;cursor:pointer;font-size:12px">${escapeAttr(_t("common.cancel", "취소"))}</button>
        <button data-role="edit-save"
          style="background:#2a4d7a;color:white;border:1px solid #3a6db0;
          border-radius:4px;padding:5px 12px;cursor:pointer;font-size:12px">${escapeAttr(_t("common.save", "저장"))}</button>
      </div>
    `;
    const input = bodyEl.querySelector('[data-role="edit-input"]');
    input.focus(); input.setSelectionRange(input.value.length, input.value.length);
    bodyEl.querySelector('[data-role="edit-cancel"]').addEventListener("click", () => {
      if (lightboxPhoto) loadDetails(lightboxPhoto.id);
    });
    bodyEl.querySelector('[data-role="edit-save"]').addEventListener("click", async () => {
      const text = input.value.trim();
      if (!text) return;
      if (!lightboxPhoto) return;
      const res = await fetch(
        `/api/photos/${lightboxPhoto.id}/comments/${commentId}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body: text }),
        }
      );
      if (!res.ok) {
        let m = `수정 실패 (${res.status})`;
        try { const dd = await res.json(); if (dd?.detail) m = dd.detail; } catch (_) {}
        alert(m);
        return;
      }
      if (lightboxPhoto) loadDetails(lightboxPhoto.id);
    });
  }

  async function deleteComment(commentId) {
    if (!lightboxPhoto) return;
    if (!confirm(_t("lb.confirm_delete_comment", "이 댓글을 삭제하시겠습니까?"))) return;
    const res = await fetch(
      `/api/photos/${lightboxPhoto.id}/comments/${commentId}`,
      { method: "DELETE" }
    );
    if (!res.ok && res.status !== 204) {
      alert(await friendlyError(res, "댓글 삭제"));
      return;
    }
    loadDetails(lightboxPhoto.id);
  }

  // --- Description ------------------------------------------------
  function renderDescription(text) {
    if (text) {
      lbDesc.classList.remove("empty");
      lbDesc.textContent = text;
    } else {
      lbDesc.classList.add("empty");
      lbDesc.textContent = _t("lb.desc_add", "설명 추가...");
    }
  }

  function startEditDescription() {
    if (!lightboxPhoto) return;
    const current = lbDesc.classList.contains("empty") ? "" : lbDesc.textContent;
    lbDesc.classList.remove("empty");
    lbDesc.innerHTML = `
      <textarea data-role="desc-input">${escapeAttr(current)}</textarea>
      <div class="lb-desc-actions">
        <button class="cancel" data-role="desc-cancel">${escapeAttr(_t("common.cancel", "취소"))}</button>
        <button data-role="desc-save">${escapeAttr(_t("common.save", "저장"))}</button>
      </div>
    `;
    const ta = lbDesc.querySelector('[data-role="desc-input"]');
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    lbDesc.querySelector('[data-role="desc-cancel"]').addEventListener(
      "click", () => renderDescription(current)
    );
    lbDesc.querySelector('[data-role="desc-save"]').addEventListener(
      "click", async () => {
        const text = ta.value;
        const res = await fetch(
          `/api/photos/${lightboxPhoto.id}/description`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ description: text }),
          }
        );
        if (!res.ok) {
          alert(await friendlyError(res, _t("lb.desc_save", "설명 저장")));
          return;
        }
        renderDescription(text.trim());
      }
    );
  }

  // --- Tags + autocomplete ----------------------------------------
  // ML-generated labels are read-only — render once per photo, no
  // edit affordance. Same chip is reused but with a different palette
  // and a tiny source label so the user can tell yolo/clip/face apart.
  // Re-built on demand so language changes are picked up live.
  function _srcLabel() {
    return {
      "auto-yolo": _t("lb.auto_src_yolo", "객체"),
      "auto-clip": _t("lb.auto_src_clip", "장면"),
      "face":      _t("lb.auto_src_face", "얼굴"),
    };
  }
  const _SRC_LABEL = new Proxy({}, { get: (_x, k) => _srcLabel()[k] });

  function renderAutoTags(list) {
    const wrap = $("#lb-auto-tags-wrap");
    const row = $("#lb-auto-tags");
    if (!Array.isArray(list) || !list.length) {
      wrap.hidden = true;
      row.innerHTML = "";
      return;
    }
    wrap.hidden = false;
    row.innerHTML = list.map(t => {
      const srcLabel = _SRC_LABEL[t.source] || t.source || "";
      const confTip = (t.confidence != null)
        ? `${(t.confidence * 100).toFixed(0)}%`
        : "";
      const title = [t.source, confTip].filter(Boolean).join(" · ");
      const isAuto = t.source === "auto-yolo" || t.source === "auto-clip";
      const display = isAuto ? _t("auto_tag." + t.name, t.name) : t.name;
      return `<span class="lb-auto-chip" title="${escapeAttr(title)}">` +
             `${escapeAttr(display)}` +
             `<span class="src">${escapeAttr(srcLabel)}</span>` +
             `</span>`;
    }).join("");
  }

  function renderTags(tags) {
    currentTags = (tags || []).slice();
    // Rebuild the chips before the input — keep the input element alive
    // (it's a sibling) so the user's focus + draft text isn't lost.
    lbTags.querySelectorAll(".lb-tag-chip").forEach(el => el.remove());
    const frag = document.createDocumentFragment();
    for (const name of currentTags) {
      const chip = document.createElement("span");
      chip.className = "lb-tag-chip";
      chip.title = `'${name}' 태그로 필터`;
      chip.innerHTML =
        `<span data-role="chip-name">${escapeAttr(name)}</span>` +
        `<button type="button" class="x" data-role="chip-remove">×</button>`;
      chip.querySelector('[data-role="chip-name"]').addEventListener("click", () => {
        if (_deps.onTagChipClick) _deps.onTagChipClick(name);
        closeLightbox();
      });
      chip.querySelector('[data-role="chip-remove"]').addEventListener("click", (e) => {
        e.stopPropagation();
        const next = currentTags.filter(t => t !== name);
        saveTags(next);
      });
      frag.appendChild(chip);
    }
    lbTags.insertBefore(frag, lbTagInput);
  }

  async function saveTags(newTags) {
    if (!lightboxPhoto) return;
    const res = await fetch(
      `/api/photos/${lightboxPhoto.id}/tags`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: newTags }),
      }
    );
    if (!res.ok) {
      let msg = _tn("lb.tag_save_failed",
        "태그 저장 실패 ({status})", { status: res.status });
      try {
        const d = await res.json();
        if (d?.detail) msg += `\n${typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail)}`;
      } catch (_) { /* not JSON */ }
      if (res.status === 401) msg += "\n로그인이 만료되었을 수 있습니다 — 새로고침 후 다시 로그인하세요.";
      alert(msg);
      return;
    }
    const final = await res.json();
    renderTags(final);
    allTagsCache = null; // bust the cache so autocomplete shows new ones next time
  }

  async function ensureTagsCache() {
    if (allTagsCache) return allTagsCache;
    try {
      const r = await fetch("/api/photos/tags");
      if (r.ok) allTagsCache = await r.json();
      else allTagsCache = [];
    } catch (_) { allTagsCache = []; }
    return allTagsCache;
  }

  function renderTagSuggestions(query) {
    const q = (query || "").trim().toLowerCase();
    const tags = (allTagsCache || []).filter(t =>
      !currentTags.some(c => c.toLowerCase() === t.name.toLowerCase()) &&
      (q === "" || t.name.toLowerCase().includes(q))
    );
    const items = tags.slice(0, 8);
    const isNew = q && !(allTagsCache || []).some(t => t.name.toLowerCase() === q);
    const parts = items.map((t, i) =>
      `<div class="lb-tag-suggest-item${i === suggestActive ? " active" : ""}" data-name="${escapeAttr(t.name)}">${escapeAttr(t.name)}<span class="count">${t.count}</span></div>`
    );
    if (isNew) {
      parts.push(
        `<div class="lb-tag-suggest-item new${items.length === suggestActive ? " active" : ""}" data-name="${escapeAttr(query.trim())}">"${escapeAttr(query.trim())}" 새 태그로 추가</div>`
      );
    }
    if (!parts.length) {
      lbTagSuggest.classList.remove("show");
      lbTagSuggest.innerHTML = "";
      return;
    }
    lbTagSuggest.innerHTML = parts.join("");
    lbTagSuggest.classList.add("show");
    lbTagSuggest.querySelectorAll(".lb-tag-suggest-item").forEach(el => {
      el.addEventListener("mousedown", (e) => {
        // mousedown so we add before the blur fires on the input
        e.preventDefault();
        addTagFromInput(el.dataset.name);
      });
    });
  }

  function addTagFromInput(name) {
    const trimmed = (name || "").trim();
    if (!trimmed) return;
    if (currentTags.some(t => t.toLowerCase() === trimmed.toLowerCase())) {
      lbTagInput.value = "";
      lbTagSuggest.classList.remove("show");
      return;
    }
    const next = currentTags.concat([trimmed]);
    lbTagInput.value = "";
    suggestActive = -1;
    lbTagSuggest.classList.remove("show");
    saveTags(next);
  }

  // --- Date / time edit modal -------------------------------------
  function pad(n) { return String(n).padStart(2, "0"); }

  function openDateModal(d) {
    if (!lightboxPhoto) return;
    const cur = d.taken_at ? new Date(d.taken_at) : new Date();
    $("#date-input").value =
      `${cur.getFullYear()}-${pad(cur.getMonth() + 1)}-${pad(cur.getDate())}`;
    $("#date-h").value = cur.getHours();
    $("#date-m").value = cur.getMinutes();
    $("#date-s").value = cur.getSeconds();
    const orig = d.taken_at_original
      ? d.taken_at_original.replace("T", " ").slice(0, 19)
      : "(EXIF 그대로)";
    $("#date-original").textContent = orig;
    $("#date-msg").textContent = "";
    $("#date-revert").style.display = d.taken_at_original ? "" : "none";
    dateModal.classList.add("show");
    setTimeout(() => $("#date-input").focus(), 0);
  }
  function closeDateModal() { dateModal.classList.remove("show"); }

  // --- Filmstrip --------------------------------------------------
  function computeStripCount() {
    const item = STRIP_THUMB_SIZE + STRIP_GAP;
    const stripPadding = 32;
    const usable = Math.max(item, window.innerWidth - stripPadding);
    const count = Math.floor(usable / item);
    return Math.max(1, count);
  }

  function renderFilmstrip() {
    const count = computeStripCount();
    const len = lightboxList.length;
    let start = lightboxIndex - Math.floor(count / 2);
    let end = start + count;
    if (start < 0) { end -= start; start = 0; }
    if (end > len) {
      start = Math.max(0, start - (end - len));
      end = len;
    }

    const parts = [];
    for (let i = start; i < end; i++) {
      const p = lightboxList[i];
      const cls = i === lightboxIndex ? "lb-strip-item current" : "lb-strip-item";
      parts.push(
        `<div class="${cls}" data-idx="${i}" title="${escapeAttr(p.filename || "")}">` +
        `<img loading="lazy" src="/api/photos/${p.id}/thumb?size=256" alt="">` +
        `</div>`
      );
    }
    lbStrip.innerHTML = parts.join("");
    lbStrip.querySelectorAll(".lb-strip-item").forEach(el => {
      el.addEventListener("click", () => openLightboxByIndex(parseInt(el.dataset.idx, 10)));
    });
    const current = lbStrip.querySelector(".current");
    if (current) {
      const stripRect = lbStrip.getBoundingClientRect();
      const itemRect = current.getBoundingClientRect();
      const delta = (itemRect.left + itemRect.width / 2) - (stripRect.left + stripRect.width / 2);
      lbStrip.scrollLeft += delta;
    }
  }

  // --- Rotation (EXIF Orientation, lossless) ----------------------
  async function _lightboxRotate(direction) {
    const p = lightboxPhoto;
    if (!p) return;
    const btnIds = ["#menu-rotate-cw", "#menu-rotate-ccw", "#menu-rotate-180"];
    btnIds.forEach(s => { const b = $(s); if (b) b.disabled = true; });
    try {
      const res = await fetch("/api/photos/bulk-rotate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo_ids: [p.id], direction }),
      });
      if (!res.ok) {
        alert(await friendlyError(res, "회전"));
        return;
      }
      const data = await res.json();
      if (data.skipped_readonly && data.skipped_readonly.length) {
        alert(_t("bulk.alert_rotate_readonly", "읽기 전용 폴더의 사진은 회전할 수 없습니다."));
        return;
      }
      if (data.skipped_video && data.skipped_video.length) {
        alert(_t("bulk.alert_rotate_video",
          "동영상은 회전이 지원되지 않습니다."));
        return;
      }
      if (data.failed && data.failed.length) {
        alert(_tn("bulk.alert_rotate_failed",
          "회전 실패: {reason}",
          { reason: data.failed[0].reason
              || _t("bulk.alert_rotate_unknown", "알 수 없음") }));
        return;
      }
      // Always refetch from /api/photos/{id} for the authoritative
      // current sha (more reliable than reading from bulk-rotate's
      // response, which can lag the DB in long-running rotations).
      let freshSha = null;
      try {
        const r = await fetch(`/api/photos/${p.id}`);
        if (r.ok) {
          const fresh = await r.json();
          freshSha = fresh.sha256 || null;
        }
      } catch (_) { /* fall through */ }
      if (!freshSha) {
        alert(_t("bulk.alert_rotate_refetch_failed",
          "회전은 처리되었지만 갱신 정보를 가져오지 못했습니다. 페이지를 새로 고치세요."));
        return;
      }
      // Patch state in place: lightbox image + grid tile (if loaded).
      // No applyFilters reload — scroll position stays exactly where
      // the user left it.
      const ts = (typeof performance !== "undefined" && performance.now)
        ? Math.floor(performance.now())
        : new Date().getTime();
      lightboxPhoto.sha256 = freshSha;
      const photos = _photos();
      const i = photos.findIndex(x => x && x.id === p.id);
      if (i >= 0) {
        photos[i].sha256 = freshSha;
        const grid = document.getElementById("grid");
        const tile = grid && grid.querySelector(`.tile[data-idx="${i}"]`);
        const im = tile && tile.querySelector("img");
        if (im) im.src = `${_thumb(photos[i], 256)}&_t=${ts}`;
      }
      if (lbImg) lbImg.src = `${_thumb(lightboxPhoto, 1024)}&_t=${ts}`;
    } finally {
      btnIds.forEach(s => { const b = $(s); if (b) b.disabled = false; });
    }
  }

  // --- Init -------------------------------------------------------
  function init(deps) {
    _deps = deps || {};

    // Resolve DOM
    lb = $("#lightbox");
    lbImg = $("#lb-img");
    lbVideo = $("#lb-video");
    lbInfo = $("#lb-info");
    lbPrev = $("#lb-prev");
    lbNext = $("#lb-next");
    lbStrip = $("#lb-strip");
    lbInfoToggle = $("#lb-info-toggle");
    lbDetails = $("#lb-details");
    lbDetailsBody = $("#lb-details-body");
    lbDupesWrap = $("#lb-dupes-wrap");
    lbDupesBtn = $("#lb-dupes-btn");
    lbDupesPop = $("#lb-dupes-popover");
    lbStars = $("#lb-stars");
    lbRatingMeta = $("#lb-rating-meta");
    lbCommentList = $("#lb-comment-list");
    lbCommentCount = $("#lb-comment-count");
    lbCommentForm = $("#lb-comment-form");
    lbCommentInput = $("#lb-comment-input");
    lbDesc = $("#lb-desc");
    lbTags = $("#lb-tags");
    lbTagInput = $("#lb-tag-input");
    lbTagSuggest = $("#lb-tag-suggest");
    dateModal = $("#date-modal");

    // Video volume / muted persist across photos.
    _loadVideoPrefs();
    lbVideo.addEventListener("volumechange", _saveVideoPrefs);

    // Apply persisted details-panel visibility right away so the panel
    // is in the right position the first time the user opens the box.
    if (detailsVisible) {
      lbInfoToggle.classList.add("active");
      lbDetails.classList.add("show");
    }

    // --- Close + nav buttons -------------------------------------
    $("#lb-close").addEventListener("click", closeLightbox);
    $("#lb-live-toggle").addEventListener("click", () => {
      const p = lightboxPhoto;
      if (!p || p.companion_id == null || p.media_kind !== "image") return;
      _liveActive = !_liveActive;
      _renderLightbox();
    });
    lb.addEventListener("click", (e) => {
      if (e.target === lb || e.target.classList.contains("lb-main")) closeLightbox();
    });
    lbPrev.addEventListener("click", showPrev);
    lbNext.addEventListener("click", showNext);

    // Keyboard nav.
    // Esc cascades through the modal stack so the topmost layer
    // closes first: picker popover (handled inside the picker's own
    // keydown listener with stopImmediatePropagation), then date
    // modal, then lightbox. Each layer handles its own Esc and
    // returns; only the bottom-most actually closes the lightbox.
    document.addEventListener("keydown", (e) => {
      if (!lb.classList.contains("show")) return;
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
      if (e.key === "Escape") {
        if (dateModal && dateModal.classList.contains("show")) {
          // Yield to the modal layer above us. closeDateModal swallows
          // it; lightbox stays open under the modal.
          closeDateModal();
          return;
        }
        closeLightbox();
      }
      else if (e.key === "ArrowLeft") showPrev();
      else if (e.key === "ArrowRight") showNext();
      else if (e.key === "i" || e.key === "I") toggleDetails();
    });

    // Touch swipe (mobile) — horizontal drag flips prev/next.
    lb.addEventListener("touchstart", (e) => {
      if (!lb.classList.contains("show")) return;
      if (e.touches.length !== 1) { _lbTouch = null; return; }
      if (e.target.closest("video")) { _lbTouch = null; return; }
      const t = e.touches[0];
      _lbTouch = { x: t.clientX, y: t.clientY };
    }, { passive: true });
    lb.addEventListener("touchend", (e) => {
      if (!_lbTouch) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - _lbTouch.x;
      const dy = t.clientY - _lbTouch.y;
      _lbTouch = null;
      if (Math.abs(dx) < SWIPE_PX) return;
      if (Math.abs(dx) < Math.abs(dy) * 1.5) return;
      if (dx > 0) showPrev();
      else showNext();
    }, { passive: true });

    // --- Duplicates popover toggle -------------------------------
    lbDupesBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      lbDupesPop.hidden = !lbDupesPop.hidden;
    });
    document.addEventListener("click", (e) => {
      if (lbDupesPop.hidden) return;
      if (lbDupesPop.contains(e.target) || e.target === lbDupesBtn) return;
      lbDupesPop.hidden = true;
    });

    // --- Actions: share / visibility / delete / rotate ----------
    $("#menu-share").addEventListener("click", () => {
      if (lightboxPhoto && _deps.openShareModal) _deps.openShareModal([lightboxPhoto.id]);
    });

    $("#lb-visibility-toggle").addEventListener("click", async () => {
      const p = lightboxPhoto;
      if (!p) return;
      const cur = p.visibility || "inherit";
      const next = _VIS_NEXT[cur] || "inherit";
      const res = await fetch(`/api/photos/${p.id}/visibility`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ visibility: next }),
      });
      if (!res.ok) {
        alert(await friendlyError(res, _t("lb.visibility_change", "공개 범위 변경")));
        return;
      }
      p.visibility = next;
      _updateVisibilityToggle(p);
    });

    $("#menu-delete").addEventListener("click", async () => {
      lbMenu.classList.remove("show");
      const p = lightboxPhoto;
      if (!p) return;
      const ok = confirm(_tn("lb.confirm_delete_photo",
        "삭제하시겠습니까?\n\n{filename}\n\n원본 파일은 휴지통(data/trash/)으로 이동됩니다.",
        { filename: p.filename }));
      if (!ok) return;
      const res = await fetch(`/api/photos/${p.id}`, { method: "DELETE" });
      if (!res.ok) {
        alert(await friendlyError(res, "삭제"));
        return;
      }
      try {
        const data = await res.json();
        if (data && data.file_moved === false) {
          console.warn("file not moved:", data.reason);
        }
      } catch (_) { /* ignore parse errors */ }
      removeCurrentPhoto();
    });

    $("#menu-rotate-cw").addEventListener("click", () => _lightboxRotate("cw"));
    $("#menu-rotate-ccw").addEventListener("click", () => _lightboxRotate("ccw"));
    $("#menu-rotate-180").addEventListener("click", () => _lightboxRotate("180"));

    // --- Details toggle ------------------------------------------
    lbInfoToggle.addEventListener("click", toggleDetails);

    // --- Rating star interactions --------------------------------
    lbStars.addEventListener("mouseover", (e) => {
      const star = e.target.closest(".lb-star");
      if (!star) return;
      const v = parseInt(star.dataset.v, 10);
      lbStars.querySelectorAll(".lb-star").forEach(el => {
        const sv = parseInt(el.dataset.v, 10);
        el.classList.toggle("preview", sv <= v);
      });
    });
    lbStars.addEventListener("mouseleave", () => paintStars(myCurrentRating));
    lbStars.addEventListener("click", (e) => {
      const star = e.target.closest(".lb-star");
      if (!star) return;
      const v = parseInt(star.dataset.v, 10);
      setRating(v === myCurrentRating ? null : v);
    });

    // --- Comments form -------------------------------------------
    lbCommentInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        lbCommentForm.requestSubmit();
      }
    });
    lbCommentForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!lightboxPhoto) return;
      const body = lbCommentInput.value.trim();
      if (!body) return;
      const submitBtn = $("#lb-comment-submit");
      submitBtn.disabled = true;
      const res = await fetch(`/api/photos/${lightboxPhoto.id}/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body }),
      });
      submitBtn.disabled = false;
      if (!res.ok) {
        let m = `댓글 추가 실패 (${res.status})`;
        try { const d = await res.json(); if (d?.detail) m = d.detail; } catch (_) {}
        alert(m);
        return;
      }
      lbCommentInput.value = "";
      loadDetails(lightboxPhoto.id);
    });

    // --- Description: click-to-edit ------------------------------
    lbDesc.addEventListener("click", () => {
      if (lbDesc.querySelector("textarea")) return;
      startEditDescription();
    });

    // --- Tag input -----------------------------------------------
    lbTagInput.addEventListener("focus", async () => {
      await ensureTagsCache();
      suggestActive = -1;
      renderTagSuggestions(lbTagInput.value);
    });
    lbTagInput.addEventListener("input", () => {
      suggestActive = -1;
      renderTagSuggestions(lbTagInput.value);
    });
    lbTagInput.addEventListener("blur", () => {
      setTimeout(() => lbTagSuggest.classList.remove("show"), 120);
    });
    lbTagInput.addEventListener("keydown", (e) => {
      const items = lbTagSuggest.querySelectorAll(".lb-tag-suggest-item");
      if (e.key === "ArrowDown") {
        e.preventDefault();
        suggestActive = Math.min(items.length - 1, suggestActive + 1);
        renderTagSuggestions(lbTagInput.value);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        suggestActive = Math.max(-1, suggestActive - 1);
        renderTagSuggestions(lbTagInput.value);
      } else if (e.key === "Enter") {
        e.preventDefault();
        let pick = lbTagInput.value.trim();
        if (suggestActive >= 0 && items[suggestActive]) {
          pick = items[suggestActive].dataset.name;
        }
        addTagFromInput(pick);
      } else if (e.key === "Backspace" && !lbTagInput.value && currentTags.length) {
        e.preventDefault();
        saveTags(currentTags.slice(0, -1));
      } else if (e.key === "Escape") {
        lbTagSuggest.classList.remove("show");
      }
    });

    // --- Date modal ----------------------------------------------
    $("#date-cancel").addEventListener("click", closeDateModal);
    dateModal.addEventListener("click", (e) => {
      if (e.target === dateModal) closeDateModal();
    });

    $("#date-revert").addEventListener("click", async () => {
      if (!lightboxPhoto) return;
      const res = await fetch(`/api/photos/${lightboxPhoto.id}/taken-at`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taken_at: null }),
      });
      if (!res.ok) { $("#date-msg").textContent = "복원 실패"; return; }
      closeDateModal();
      loadDetails(lightboxPhoto.id);
    });

    $("#date-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!lightboxPhoto) return;
      const dateStr = $("#date-input").value;       // YYYY-MM-DD
      const h = parseInt($("#date-h").value, 10) || 0;
      const m = parseInt($("#date-m").value, 10) || 0;
      const s = parseInt($("#date-s").value, 10) || 0;
      if (!dateStr) { $("#date-msg").textContent = "날짜를 입력하세요"; return; }
      // Build a local ISO without timezone — the server stores naive
      // UTC, but for display purposes we just want the wall-clock to
      // be preserved.
      const iso = `${dateStr}T${pad(h)}:${pad(m)}:${pad(s)}`;
      const res = await fetch(`/api/photos/${lightboxPhoto.id}/taken-at`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taken_at: iso }),
      });
      if (!res.ok) {
        let msg = _tn("common.save_failed", "저장 실패 ({status})", { status: res.status });
        try { const d = await res.json(); if (d?.detail) msg = d.detail; } catch (_) {}
        $("#date-msg").textContent = msg;
        return;
      }
      closeDateModal();
      loadDetails(lightboxPhoto.id);
    });
  }

  window.lightbox = {
    init,
    openAt,
    openForPhotoId,
    openWithList,
    close: closeLightbox,
    isOpen,
    shiftIndex,
  };
})();
