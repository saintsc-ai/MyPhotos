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
  // Video containers browsers can usually play directly (H.264/AAC). Anything
  // else (avi/mkv/hevc/3gp/wmv/flv/ts…) is offered an "MP4로 변환해 다운로드".
  const NATIVE_VIDEO = new Set([".mp4", ".m4v", ".mov", ".webm"]);
  const SWIPE_PX = 50;
  // Vertical drag (from the photo area) past this dismisses the lightbox.
  // A bit larger than SWIPE_PX so it's a deliberate gesture, not a nudge.
  const CLOSE_PX = 90;
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
  // Video proxy (web-playable H.264) state. When the original fails to
  // decode (HEVC / .mkv / .avi), we lazily request a proxy and poll for it.
  let _proxyPoll = null;     // setTimeout handle while a proxy transcodes
  let _proxyDlPoll = null;   // setTimeout handle for a convert-to-mp4 download
  let _proxyTriedId = null;  // photo id we've already auto-requested this view
  let _videoNoticeEl = null; // overlay shown during conversion

  // --- Pinch-zoom / pan state -------------------------------------
  // _lbZoom mirrors the CSS transform applied to #lb-img. The existing
  // swipe/close touch handlers consult `_lbZoom.scale > 1` to bail out
  // so prev/next/close only fire at 1x. `tx`/`ty` are translate px
  // applied BEFORE scale (CSS order: translate then scale).
  const ZOOM_MIN = 1;
  const ZOOM_MAX = 4;
  const DOUBLE_TAP_SCALE = 2.5;
  const DOUBLE_TAP_MS = 300;     // max gap between taps to count as double
  const DOUBLE_TAP_SLOP = 30;    // max px between taps to count as double
  let _lbZoom = { scale: 1, tx: 0, ty: 0 };
  // Transient gesture trackers (null when no gesture in flight).
  let _pinch = null;   // { startDist, startScale, startTx, startTy, cx, cy }
  let _pan = null;     // { startX, startY, startTx, startTy }
  let _lastTap = 0;    // timestamp of the previous single-finger tap
  let _lastTapXY = { x: 0, y: 0 };

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
    _clearProxyPoll();
    _hideVideoNotice();
    _proxyTriedId = null;
    try {
      lbVideo.pause();
      lbVideo.removeAttribute("src");
      lbVideo.load();           // forces release of the prior source
    } catch (_) { /* element may not have had a source */ }
  }

  // --- Web-playable video proxy (lazy, on decode failure) ---------
  function _clearProxyPoll() {
    if (_proxyPoll) { clearTimeout(_proxyPoll); _proxyPoll = null; }
    if (_proxyDlPoll) { clearTimeout(_proxyDlPoll); _proxyDlPoll = null; }
  }
  function _videoNotice() {
    if (!_videoNoticeEl) {
      _videoNoticeEl = document.createElement("div");
      _videoNoticeEl.className = "lb-video-notice";
      _videoNoticeEl.style.cssText =
        "position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);"
        + "background:rgba(0,0,0,0.75);color:#fff;padding:12px 18px;"
        + "border-radius:8px;font-size:14px;max-width:80%;text-align:center;"
        + "z-index:6;pointer-events:none;line-height:1.5;";
      lb.appendChild(_videoNoticeEl);
    }
    return _videoNoticeEl;
  }
  function _showVideoNotice(text) {
    const el = _videoNotice();
    el.textContent = text;
    el.style.display = "";
  }
  function _hideVideoNotice() {
    if (_videoNoticeEl) _videoNoticeEl.style.display = "none";
  }

  // Ask the server to build an H.264 proxy, poll until it's ready, then
  // reload + play. Triggered by an undecodable container (error) AND by an
  // undecodable video track in an otherwise-playable file (audio plays but
  // there's no picture). Never loops within one view (_proxyTriedId).
  function _kickVideoProxy() {
    const p = lightboxPhoto;
    if (!p || p.media_kind !== "video") return; // Live-photo case uses original
    if (_proxyTriedId === p.id) return;
    _proxyTriedId = p.id;
    const id = p.id;
    _showVideoNotice(_t("lb.video_converting",
      "이 형식은 브라우저에서 바로 재생할 수 없어 변환 중입니다…"));
    const poll = function () {
      api.post(`/api/photos/${id}/proxy`).then(function (r) {
        if (!lightboxPhoto || lightboxPhoto.id !== id) { _clearProxyPoll(); return; }
        if (r && r.status === "done") {
          _clearProxyPoll();
          _hideVideoNotice();
          lbVideo.src = `/api/photos/${id}/video?p=1`; // cache-bust the failed media
          lbVideo.load();
          lbVideo.play().catch(function () { /* autoplay may be blocked */ });
        } else if (r && r.status === "failed") {
          _clearProxyPoll();
          _showVideoNotice(_t("lb.video_convert_failed",
            "이 동영상은 변환에 실패했습니다. 원본을 내려받아 재생해 주세요."));
        } else {
          _proxyPoll = setTimeout(poll, 3000); // pending / running
        }
      }).catch(function () {
        _clearProxyPoll();
        _showVideoNotice(_t("lb.video_convert_failed",
          "이 동영상은 변환에 실패했습니다. 원본을 내려받아 재생해 주세요."));
      });
    };
    poll();
  }

  // "MP4로 변환해 다운로드": for an undecodable video, hand back the H.264
  // proxy instead of the original. If the proxy isn't built yet, kick it
  // (POST /proxy is idempotent — also covers the playback build) and poll
  // until done, then trigger the download. Independent of the playback
  // poll so a download can run while watching.
  function _downloadVideoMp4(p) {
    if (!p || p.media_kind !== "video") return;
    const id = p.id;
    const base = (p.filename || "video").replace(/\.[^.]+$/, "");
    const trigger = function () {
      _hideVideoNotice();
      const a = document.createElement("a");
      a.href = `/api/photos/${id}/download?format=mp4`;
      a.download = base + ".mp4";
      document.body.appendChild(a);
      a.click();
      a.remove();
    };
    _showVideoNotice(_t("lb.video_dl_converting",
      "다운로드용으로 변환 중입니다… 완료되면 자동으로 받아집니다."));
    const poll = function () {
      api.post(`/api/photos/${id}/proxy`).then(function (r) {
        if (!lightboxPhoto || lightboxPhoto.id !== id) {
          if (_proxyDlPoll) { clearTimeout(_proxyDlPoll); _proxyDlPoll = null; }
          return;
        }
        if (r && r.status === "done") {
          if (_proxyDlPoll) { clearTimeout(_proxyDlPoll); _proxyDlPoll = null; }
          trigger();
        } else if (r && r.status === "failed") {
          if (_proxyDlPoll) { clearTimeout(_proxyDlPoll); _proxyDlPoll = null; }
          _showVideoNotice(_t("lb.video_convert_failed",
            "이 동영상은 변환에 실패했습니다. 원본을 내려받아 재생해 주세요."));
        } else {
          _proxyDlPoll = setTimeout(poll, 3000); // pending / running
        }
      }).catch(function () {
        if (_proxyDlPoll) { clearTimeout(_proxyDlPoll); _proxyDlPoll = null; }
        _showVideoNotice(_t("lb.video_convert_failed",
          "이 동영상은 변환에 실패했습니다. 원본을 내려받아 재생해 주세요."));
      });
    };
    poll();
  }

  // Undecodable container/codec → the element fires "error" (codes 3/4).
  function _onVideoError() {
    const code = lbVideo.error && lbVideo.error.code;
    if (code === 3 || code === 4) _kickVideoProxy();
  }
  // Old MP4s that decode the AUDIO but not the VIDEO track play sound with
  // no picture and fire no "error" — detect the missing video track once
  // metadata is in. (The H.264 proxy reload reports real dimensions, and
  // _proxyTriedId stops a re-trigger.)
  function _onVideoLoadedMeta() {
    const p = lightboxPhoto;
    if (p && p.media_kind === "video"
        && lbVideo.videoWidth === 0 && lbVideo.videoHeight === 0) {
      _kickVideoProxy();
    }
  }

  // --- Pinch-zoom / double-tap / drag-pan -------------------------
  // All gesture math operates in viewport pixels relative to the image's
  // *layout* box (the un-transformed getBoundingClientRect would include
  // the transform, so we back it out). The applied transform is
  //   translate(tx, ty) scale(scale)   — translate first, then scale,
  // scaled about the element's center (transform-origin: 50% 50%).
  function _isZoomImage() {
    // Zoom only applies to still images; videos and Live playback no-op.
    const p = lightboxPhoto;
    return !!(p && lbImg && lbImg.style.display !== "none"
              && p.media_kind === "image" && !_liveActive);
  }

  function _applyZoom() {
    const z = _lbZoom;
    if (z.scale <= 1) {
      lbImg.style.transform = "";
    } else {
      lbImg.style.transform =
        `translate(${z.tx}px, ${z.ty}px) scale(${z.scale})`;
    }
  }

  function _resetZoom() {
    _lbZoom = { scale: 1, tx: 0, ty: 0 };
    _pinch = null;
    _pan = null;
    if (lbImg) lbImg.style.transform = "";
  }

  // Clamp tx/ty so the (scaled) image can't be dragged off-screen — at
  // least the image edge stays within its layout box. With center
  // origin, the max translate on each axis is half the extra size.
  function _clampPan() {
    const z = _lbZoom;
    if (z.scale <= 1) { z.tx = 0; z.ty = 0; return; }
    const w = lbImg.clientWidth || lbImg.offsetWidth || 0;
    const h = lbImg.clientHeight || lbImg.offsetHeight || 0;
    const maxX = (w * (z.scale - 1)) / 2;
    const maxY = (h * (z.scale - 1)) / 2;
    z.tx = Math.max(-maxX, Math.min(maxX, z.tx));
    z.ty = Math.max(-maxY, Math.min(maxY, z.ty));
  }

  // Zoom toward a focal point (viewport px). Keeps the pixel under the
  // focal point fixed as scale changes. Used by pinch + double-tap.
  function _zoomToPoint(newScale, focalX, focalY) {
    const z = _lbZoom;
    newScale = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, newScale));
    const rect = lbImg.getBoundingClientRect();
    // Center of the (transformed) image on screen.
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    // Focal point offset from center, in *unscaled* image space.
    const ox = (focalX - cx) / z.scale;
    const oy = (focalY - cy) / z.scale;
    // Solve so the focal point stays put after the scale change.
    z.tx += ox * (z.scale - newScale);
    z.ty += oy * (z.scale - newScale);
    z.scale = newScale;
    _clampPan();
    _applyZoom();
  }

  function _dist(a, b) {
    const dx = a.clientX - b.clientX, dy = a.clientY - b.clientY;
    return Math.hypot(dx, dy);
  }
  function _mid(a, b) {
    return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
  }

  // touchstart on the image: begin pinch (2 fingers) or detect a
  // double-tap / start a pan (1 finger while zoomed).
  function _zoomTouchStart(e) {
    if (!_isZoomImage()) return;
    if (e.touches.length === 2) {
      // Two fingers → pinch. Cancel any pending pan/double-tap.
      _pan = null;
      _lastTap = 0;
      const a = e.touches[0], b = e.touches[1];
      const m = _mid(a, b);
      _pinch = {
        startDist: _dist(a, b) || 1,
        startScale: _lbZoom.scale,
        cx: m.x, cy: m.y,
      };
      e.preventDefault();
      return;
    }
    if (e.touches.length === 1) {
      const t = e.touches[0];
      const now = Date.now();
      // Double-tap detection (toggle 1x <-> DOUBLE_TAP_SCALE).
      if (now - _lastTap < DOUBLE_TAP_MS
          && Math.hypot(t.clientX - _lastTapXY.x,
                        t.clientY - _lastTapXY.y) < DOUBLE_TAP_SLOP) {
        _lastTap = 0;
        if (_lbZoom.scale > 1) {
          _resetZoom();
        } else {
          _zoomToPoint(DOUBLE_TAP_SCALE, t.clientX, t.clientY);
        }
        e.preventDefault();
        return;
      }
      _lastTap = now;
      _lastTapXY = { x: t.clientX, y: t.clientY };
      // Start a pan only when already zoomed; at 1x leave the gesture to
      // the existing swipe/close handlers on `lb`.
      if (_lbZoom.scale > 1) {
        _pan = {
          startX: t.clientX, startY: t.clientY,
          startTx: _lbZoom.tx, startTy: _lbZoom.ty,
        };
      }
    }
  }

  function _zoomTouchMove(e) {
    if (_pinch && e.touches.length === 2) {
      const a = e.touches[0], b = e.touches[1];
      const ratio = _dist(a, b) / _pinch.startDist;
      const target = _pinch.startScale * ratio;
      _zoomToPoint(target, _pinch.cx, _pinch.cy);
      e.preventDefault();   // listener registered with { passive: false }
      return;
    }
    if (_pan && e.touches.length === 1 && _lbZoom.scale > 1) {
      const t = e.touches[0];
      _lbZoom.tx = _pan.startTx + (t.clientX - _pan.startX);
      _lbZoom.ty = _pan.startTy + (t.clientY - _pan.startY);
      _clampPan();
      _applyZoom();
      // Stop the gesture reaching `lb`'s swipe/close handlers, and stop
      // the page from scrolling underneath.
      e.preventDefault();
      e.stopPropagation();
      return;
    }
  }

  function _zoomTouchEnd(e) {
    // Pinch ends once we drop below two active touches.
    if (_pinch && e.touches.length < 2) {
      _pinch = null;
      // Snap fully back to 1x if we ended just above it.
      if (_lbZoom.scale <= 1.02) _resetZoom();
    }
    if (_pan && e.touches.length === 0) {
      _pan = null;
      // A finished pan must not also register as swipe/close on `lb`.
      e.stopPropagation();
    }
  }

  // --- Open / close -----------------------------------------------
  function closeLightbox() {
    lb.classList.remove("show", "lb-isolated");
    _resetZoom();
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
    // The ⇄ convert-download button serves two roles depending on media:
    //   image (non-browser-safe, e.g. RAW/HEIC) → PNG conversion via href,
    //   video (non-native container, e.g. avi/mkv/hevc) → MP4 H.264 proxy,
    //     which may need building first, so it's JS-driven (no href) and
    //     handled by the #menu-convert click listener (see init).
    const cv = $("#menu-convert");
    const wantImgPng = (p.media_kind === "image") && !BROWSER_SAFE.has(ext);
    const wantVidMp4 = (p.media_kind === "video") && !NATIVE_VIDEO.has(ext);
    if (wantImgPng) {
      cv.href = `/api/photos/${p.id}/download?format=png`;
      cv.setAttribute("download", base + ".png");
      cv.setAttribute("data-i18n-title", "lb.convert_png");
      cv.title = _t("lb.convert_png", "PNG로 변환 후 다운로드");
      cv.style.display = "";
    } else if (wantVidMp4) {
      cv.removeAttribute("href");      // JS flow: build proxy if needed, then DL
      cv.removeAttribute("download");
      cv.setAttribute("data-i18n-title", "lb.convert_mp4");
      cv.title = _t("lb.convert_mp4", "MP4로 변환해 다운로드");
      cv.style.display = "";
    } else {
      cv.style.display = "none";
    }
    // 얼굴 가림 다운로드는 정지 이미지만 의미가 있음 — 동영상은 숨김.
    // 얼굴 검출 여부는 클릭 시점에 확인 (매번 fetch 하면 라이트박스
    // 열 때마다 1회 추가 — 지금은 의미 없는 비용이라 click 시점에 한 번).
    const mk = $("#menu-mask-download");
    if (mk) mk.style.display = (p.media_kind === "image") ? "" : "none";
  }

  function _renderLightbox() {
    const p = lightboxPhoto;
    if (!p) return;
    // Clear any pinch-zoom from the previous photo (prev/next/open/Live).
    _resetZoom();
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
      // /video serves the H.264 proxy when ready, else the original. If the
      // original is an undecodable codec, the <video> "error" handler kicks
      // off a lazy proxy build (see _onVideoError).
      lbVideo.src = `/api/photos/${p.id}/video`;
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
    lbDetailsBody.innerHTML = `<div style="color:#666">${escapeAttr(_t("common.loading", "불러오는 중..."))}</div>`;
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
      if (!res.ok) { lbDetailsBody.innerHTML = `<div style="color:#a66">${escapeAttr(_tn("common.load_failed", "로드 실패 ({status})", { status: res.status }))}</div>`; return; }
      d = await res.json();
    } catch (e) {
      if (my !== detailsReqSeq) return;
      lbDetailsBody.innerHTML = `<div style="color:#a66">${escapeAttr(_t("common.network_error", "네트워크 오류"))}</div>`; return;
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

    // Two semantic groups: 파일 정보 (file-level metadata about the file
    // on disk) and 촬영정보 (camera / EXIF metadata about the capture).
    // Each group is a separate <dl> so the grid layout stays intact and
    // empty rows in one group don't shrink the other.
    const fileRows = [];
    const shotRows = [];
    const _row = (label, value, html) =>
      `<dt>${escapeAttr(label)}</dt><dd>${html ? value : escapeAttr(String(value))}</dd>`;
    const push = (rows, label, value, html = false) => {
      if (value === null || value === undefined || value === "") return;
      rows.push(_row(label, value, html));
    };
    // ----- 파일 정보 group (everything about the file on disk) -----
    push(fileRows, _t("lb.field_filename", "파일명"), d.filename);
    push(fileRows, _t("lb.field_kind", "종류"),
      `${d.media_kind || ""}${d.ext ? " (" + d.ext + ")" : ""}`);
    push(fileRows, _t("lb.field_file_size", "파일 크기"), fmtBytes(d.file_size));
    // mtime — useful when taken_at is empty (날짜 없음 photos) and as
    // a sanity check otherwise. Photo.mtime is set by the scanner
    // from st_mtime; for files unchanged after import, this is
    // effectively the file's creation date on disk.
    if (d.mtime) {
      push(fileRows, _t("lb.field_file_mtime", "파일 생성일자"),
        d.mtime.replace("T", " ").slice(0, 19));
    }
    push(fileRows, _t("lb.field_path", "경로"), d.rel_path);
    if (d.owner_user_id != null) {
      push(fileRows, _t("lb.field_uploader", "올린 사람"),
        d.owner_username || `#${d.owner_user_id}`);
    } else {
      push(fileRows, _t("lb.field_uploader", "올린 사람"),
        _t("lb.field_uploader_unset", "(미지정)"));
    }
    if (d.sha256) push(fileRows, _t("lb.field_sha256", "SHA-256"),
      `<code>${escapeAttr(d.sha256)}</code>`, true);
    push(fileRows, _t("lb.field_indexed_at", "인덱스됨"),
      d.indexed_at ? d.indexed_at.replace("T", " ").slice(0, 19) : null);
    push(fileRows, _t("lb.field_exif_extractor", "EXIF 추출기"), d.exif_extractor);

    // ----- 촬영정보 group (capture / EXIF metadata) -----
    // taken_at editing writes to the file's EXIF DateTimeOriginal +
    // CreateDate (admin-only on the server), so the ✎ button is only
    // rendered for admins. Other users see the read-only timestamp.
    // Computed here so it's visible to the GPS block below too.
    const _u = _user();
    const _isAdmin = !!(_u && _u.is_admin);
    const dateText = d.taken_at ? d.taken_at.replace("T", " ").slice(0, 19) : _t("lb.field_none", "(없음)");
    const dateEditBtn = _isAdmin
      ? ` <button type="button" class="edit-icon" data-role="edit-date" title="${escapeAttr(_t("lb.edit_date", "날짜 편집"))}">✎</button>`
      : "";
    const reverted = d.taken_at_original
      ? `<span class="reverted-hint">${_t("lb.field_original", "원래:")} ${escapeAttr(d.taken_at_original.replace("T", " ").slice(0, 19))}</span>`
      : "";
    push(shotRows,
      _t("lb.field_taken_at", "촬영시각"),
      `${escapeAttr(dateText)}${dateEditBtn}${reverted}`,
      true
    );
    if (d.width && d.height) push(shotRows,
      _t("lb.field_dimensions", "크기"), `${d.width} × ${d.height}`);
    const cam = [d.camera_make, d.camera_model].filter(Boolean).join(" ");
    push(shotRows, _t("lb.field_camera", "카메라"), cam);
    push(shotRows, _t("lb.field_lens", "렌즈"), d.lens);
    const shot = [];
    if (d.fnumber) shot.push(`f/${d.fnumber}`);
    if (d.exposure) shot.push(d.exposure + (/\d+\/\d+/.test(d.exposure) ? "s" : ""));
    if (d.iso) shot.push(`ISO ${d.iso}`);
    if (d.focal_length) shot.push(`${d.focal_length}mm`);
    if (shot.length) push(shotRows, _t("lb.field_exposure", "노출"), shot.join(" · "));
    if (d.duration_seconds) push(shotRows, _t("lb.field_duration", "영상 길이"),
      `${d.duration_seconds.toFixed(1)}s`);
    // GPS row: shown when the photo has GPS, OR when the current user
    // is admin (so they can add it on photos that lack GPS). The ✎
    // button opens the GPS picker modal where the user clicks on a
    // Leaflet map to drop / drag a pin. Server-side the endpoint is
    // admin-only (require_admin) because it writes the GPS tags into
    // the file's EXIF, not just the DB — mirrors the taken_at + rotate
    // paths for consistency.
    const canEditGps = _isAdmin;
    const hasGps = d.latitude != null && d.longitude != null;
    if (hasGps || canEditGps) {
      const editBtn = canEditGps
        ? ` <button type="button" class="edit-icon" data-role="edit-gps" title="${escapeAttr(_t("lb.field_gps_edit", "GPS 편집"))}">✎</button>`
        : "";
      if (hasGps) {
        const lat = d.latitude.toFixed(6), lng = d.longitude.toFixed(6);
        push(shotRows,
          _t("lb.field_gps", "GPS"),
          `${lat}, ${lng}` +
          ` · <a href="https://www.openstreetmap.org/?mlat=${lat}&mlon=${lng}#map=15/${lat}/${lng}" target="_blank">${escapeAttr(_t("lb.map_link", "지도"))}</a>` +
          editBtn,
          true
        );
      } else {
        push(shotRows,
          _t("lb.field_gps", "GPS"),
          `<span style="color:#777">${escapeAttr(_t("lb.field_none", "(없음)"))}</span>${editBtn}`,
          true
        );
      }
    }

    // Compose the body. Empty groups (no rows) collapse so a photo
    // missing every camera field doesn't show a stranded 촬영정보 header.
    const parts = [];
    if (fileRows.length) {
      parts.push(
        `<h4 class="lb-section-title lb-group-title">${escapeAttr(_t("lb.group_file", "파일 정보"))}</h4>`,
        `<dl>${fileRows.join("")}</dl>`);
    }
    if (shotRows.length) {
      parts.push(
        `<h4 class="lb-section-title lb-group-title">${escapeAttr(_t("lb.group_shoot", "촬영 정보"))}</h4>`,
        `<dl>${shotRows.join("")}</dl>`);
    }
    // OCR'd text (only present when extracted) — read-only, scrollable.
    if (d.ocr_text) {
      parts.push(
        `<h4 class="lb-section-title lb-group-title">${escapeAttr(_t("lb.group_ocr", "텍스트 (OCR)"))}</h4>`,
        `<div class="lb-ocr-text" style="font-size:12px;color:#bbb;line-height:1.5;`
        + `white-space:pre-wrap;word-break:break-word;max-height:200px;overflow:auto;`
        + `background:rgba(255,255,255,0.03);border-radius:6px;padding:8px">`
        + `${escapeAttr(d.ocr_text)}</div>`);
    }
    lbDetailsBody.innerHTML = parts.length
      ? parts.join("")
      : `<div style="color:#666">${escapeAttr(_t("lb.no_info", "정보 없음"))}</div>`;

    const editBtn = lbDetailsBody.querySelector('[data-role="edit-date"]');
    if (editBtn) editBtn.addEventListener("click", () => openDateModal(d));
    const editGpsBtn = lbDetailsBody.querySelector('[data-role="edit-gps"]');
    if (editGpsBtn) editGpsBtn.addEventListener("click", () => openGpsModal(d));

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

  // --- GPS edit modal --------------------------------------------
  // Built lazily on first open — Leaflet needs the #gps-map div to be
  // visible at construction time to measure correctly. invalidateSize
  // gets kicked on every open so the modal can be re-shown after a
  // window resize / orientation flip without stale dimensions.
  let _gpsMap = null;
  let _gpsTiles = null;
  let _gpsMarker = null;
  let _gpsSelected = null;     // {lat, lng, alt}
  let _gpsMode = "single";     // "single" → PUT /{id}/gps for lightboxPhoto;
                               // "bulk"   → POST /bulk-gps for _gpsBulkIds
  let _gpsBulkIds = [];        // photo ids to apply in bulk mode
  let gpsModal = null;         // resolved in init

  // --- Face-mask download state (resolved in init) ----------------
  // _maskFaces shape: [{
  //    bbox: [x,y,w,h]∈[0..1],   // mutated by drag-to-resize
  //    mask: bool,                // true → pixelate on download
  //    source: 'detected'|'user', // detected = from YuNet; user = drawn
  //    confidence: number|null,   // detected only
  //    high_confidence: bool,
  // }]
  let maskModal = null;
  let _maskFaces = [];
  let _maskPhotoId = null;
  let _maskBusy = false;
  // Padding (fraction of bbox edge) applied to detected face boxes
  // on first render — YuNet's crop is tight at hairline/chin so a
  // little outward bleed gives a more natural blur. User-drawn
  // boxes are taken as-is.
  const _MASK_DETECTED_PAD = 0.10;
  // Transient state during a resize / draw drag — never persisted.
  let _maskDrag = null;     // { kind:'resize'|'draw', faceIdx?, corner?, startX, startY, startBbox?, stageRect, drawingEl? }
  let _maskDrawing = false; // toggle for "+ 새 영역" mode

  function _gpsUpdateDisplay() {
    const el = $("#gps-coords-display");
    if (!el) return;
    if (_gpsSelected) {
      el.textContent = `${_gpsSelected.lat.toFixed(6)}, ${_gpsSelected.lng.toFixed(6)}`;
    } else {
      el.textContent = _t("lb.field_none", "(없음)");
    }
  }

  function _gpsPlaceMarker(lat, lng) {
    if (_gpsMarker) {
      try { _gpsMap.removeLayer(_gpsMarker); } catch (_) {}
    }
    _gpsMarker = L.marker([lat, lng], { draggable: true }).addTo(_gpsMap);
    _gpsMarker.on("dragend", (e) => {
      const p = e.target.getLatLng();
      _gpsSelected = {
        lat: p.lat, lng: p.lng,
        alt: _gpsSelected ? _gpsSelected.alt : null,
      };
      _gpsUpdateDisplay();
    });
    _gpsSelected = {
      lat, lng,
      alt: _gpsSelected ? _gpsSelected.alt : null,
    };
    _gpsUpdateDisplay();
  }

  function _gpsClearMarker() {
    if (_gpsMarker) {
      try { _gpsMap.removeLayer(_gpsMarker); } catch (_) {}
      _gpsMarker = null;
    }
    _gpsSelected = null;
    _gpsUpdateDisplay();
  }

  function _gpsTileLayerForTheme() {
    // Mirror /js/panels/mapview.js's basemap choice so the picker map
    // doesn't look noticeably worse than the main map view: stock OSM
    // for light, CartoDB Dark Matter (with @2x retina via {r}) for
    // dark. Two separate layer instances per map — Leaflet doesn't
    // share a tile layer across maps, so we can't reuse mapView's.
    const isDark = !document.body.classList.contains("light");
    if (isDark) {
      return L.tileLayer(
        "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        {
          attribution:
            '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
            ' contributors © <a href="https://carto.com/attributions">CARTO</a>',
          maxZoom: 19,
          subdomains: "abcd",
        }
      );
    }
    return L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
      }
    );
  }

  function _gpsApplyTheme() {
    // Called from openGpsModal so a theme switch between opens picks
    // up the right basemap. Drop the old layer, add the new one.
    if (!_gpsMap) return;
    if (_gpsTiles) {
      try { _gpsMap.removeLayer(_gpsTiles); } catch (_) {}
    }
    _gpsTiles = _gpsTileLayerForTheme();
    _gpsTiles.addTo(_gpsMap);
  }

  function _gpsBuildMap() {
    if (_gpsMap) return;
    _gpsMap = L.map("gps-map").setView([37.5, 127.0], 7);
    _gpsTiles = _gpsTileLayerForTheme();
    _gpsTiles.addTo(_gpsMap);
    _gpsMap.on("click", (e) => {
      _gpsPlaceMarker(e.latlng.lat, e.latlng.lng);
    });
  }

  function _gpsResetModalChrome() {
    // Reset the chrome that differs between single + bulk runs so a
    // reopen doesn't leak the previous mode's labels.
    const titleEl = gpsModal.querySelector("h2");
    const hintEl = gpsModal.querySelector(".hint");
    if (_gpsMode === "bulk") {
      if (titleEl) titleEl.textContent =
        _t("gps_modal.title_bulk", "GPS 위치 일괄 편집");
      if (hintEl) hintEl.textContent =
        _tn("gps_modal.hint_bulk",
            "선택한 {count}장에 적용됩니다. 지도를 클릭하여 위치를 선택하세요. (취소: 변경 없음 / 삭제: 모두에서 GPS 제거)",
            { count: _gpsBulkIds.length });
    } else {
      if (titleEl) titleEl.textContent =
        _t("gps_modal.title", "GPS 위치 편집");
      if (hintEl) hintEl.textContent =
        _t("gps_modal.hint",
           "지도를 클릭하여 위치를 선택하세요. 마커는 드래그할 수 있습니다.");
    }
  }

  function openGpsModal(d) {
    if (!lightboxPhoto) return;
    _gpsMode = "single";
    _gpsBulkIds = [];
    _gpsResetModalChrome();
    gpsModal.classList.add("show");
    $("#gps-msg").textContent = "";

    _gpsBuildMap();
    _gpsApplyTheme();    // sync basemap with current light/dark choice

    // Reset marker for this photo: if it already has GPS, drop a pin
    // there; otherwise leave the map blank so a click drops the first
    // marker. Either way the user can drag or click again to refine.
    if (_gpsMarker) {
      try { _gpsMap.removeLayer(_gpsMarker); } catch (_) {}
      _gpsMarker = null;
    }
    _gpsSelected = (d.latitude != null && d.longitude != null)
      ? { lat: d.latitude, lng: d.longitude, alt: d.altitude }
      : null;

    if (_gpsSelected) {
      _gpsMap.setView([_gpsSelected.lat, _gpsSelected.lng], 14);
      _gpsPlaceMarker(_gpsSelected.lat, _gpsSelected.lng);
    } else {
      // No starting point — keep the previous view (or default
      // Korea-centred) so the user can pan + click.
      _gpsUpdateDisplay();
    }

    // Leaflet measures container size at construction. The map div was
    // display:none until the modal flipped to .show a microtask ago, so
    // it cached a width/height of 0. Re-measure after the layout settles.
    setTimeout(() => { if (_gpsMap) _gpsMap.invalidateSize(); }, 80);
  }

  function openGpsModalBulk(ids, opts) {
    // Bulk-bar entry point. `ids` is the list of selected photo ids;
    // opts.afterSave is called with the server response so the caller
    // (gallery) can clear selection / show toast.
    if (!Array.isArray(ids) || !ids.length) return;
    _gpsMode = "bulk";
    _gpsBulkIds = ids.slice();
    _gpsAfterBulkSave = (opts && opts.afterSave) || null;
    _gpsResetModalChrome();
    gpsModal.classList.add("show");
    $("#gps-msg").textContent = "";

    _gpsBuildMap();
    _gpsApplyTheme();    // sync basemap with current light/dark choice

    // No pre-fill — we don't pretend to know whether the selected
    // photos share a location. User picks a point fresh.
    if (_gpsMarker) {
      try { _gpsMap.removeLayer(_gpsMarker); } catch (_) {}
      _gpsMarker = null;
    }
    _gpsSelected = null;
    _gpsUpdateDisplay();

    setTimeout(() => { if (_gpsMap) _gpsMap.invalidateSize(); }, 80);
  }

  let _gpsAfterBulkSave = null;   // set by openGpsModalBulk

  function closeGpsModal() {
    if (gpsModal) gpsModal.classList.remove("show");
  }

  async function _gpsSaveSingle() {
    if (!lightboxPhoto) return;
    const msg = $("#gps-msg");
    const saveBtn = $("#gps-save");
    saveBtn.disabled = true;
    msg.textContent = "";
    try {
      const body = _gpsSelected
        ? { latitude: _gpsSelected.lat, longitude: _gpsSelected.lng,
            altitude: _gpsSelected.alt }
        : { latitude: null, longitude: null, altitude: null };
      const res = await fetch(`/api/photos/${lightboxPhoto.id}/gps`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        msg.textContent = await friendlyError(res,
          _t("gps_modal.save_failed", "GPS 저장 실패"));
        return;
      }
      closeGpsModal();
      // Refresh details so the GPS row reflects the new value (or
      // disappears when cleared).
      loadDetails(lightboxPhoto.id);
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function _gpsSaveBulk() {
    if (!_gpsBulkIds.length) return;
    const msg = $("#gps-msg");
    const saveBtn = $("#gps-save");
    saveBtn.disabled = true;
    msg.textContent = "";
    try {
      const body = {
        photo_ids: _gpsBulkIds,
        latitude: _gpsSelected ? _gpsSelected.lat : null,
        longitude: _gpsSelected ? _gpsSelected.lng : null,
        altitude: _gpsSelected ? _gpsSelected.alt : null,
      };
      const res = await fetch(`/api/photos/bulk-gps`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        msg.textContent = await friendlyError(res,
          _t("gps_modal.save_failed", "GPS 저장 실패"));
        return;
      }
      const data = await res.json();
      closeGpsModal();
      // Surface per-photo skip counts so the user knows how many
      // landed vs were skipped (readonly / video / failed).
      const parts = [
        _tn("gps_modal.bulk_done_updated",
            "GPS {count}개 적용됨", { count: data.updated }),
      ];
      if (data.skipped_readonly && data.skipped_readonly.length) {
        parts.push(_tn("gps_modal.bulk_done_readonly",
          "{count}개 건너뜀 (읽기 전용)", { count: data.skipped_readonly.length }));
      }
      if (data.skipped_video && data.skipped_video.length) {
        parts.push(_tn("gps_modal.bulk_done_video",
          "{count}개 건너뜀 (동영상)", { count: data.skipped_video.length }));
      }
      if (data.failed && data.failed.length) {
        parts.push(_tn("gps_modal.bulk_done_failed",
          "{count}개 실패", { count: data.failed.length }));
      }
      alert(parts.join("\n"));
      if (_gpsAfterBulkSave) {
        try { _gpsAfterBulkSave(data); } catch (_) { /* ignore */ }
      }
    } finally {
      saveBtn.disabled = false;
    }
  }

  function _gpsSave() {
    return _gpsMode === "bulk" ? _gpsSaveBulk() : _gpsSaveSingle();
  }

  // --- Face-mask download modal -----------------------------------
  function _maskUpdateCounter() {
    const masked = _maskFaces.filter(f => f.mask).length;
    const total = _maskFaces.length;
    const el = $("#mask-counter");
    if (!el) return;
    if (total === 0) {
      // Different copy when there's nothing to toggle — guide the
      // user toward "+ 새 영역" instead of showing "0/0".
      el.textContent = _t(
        "mask_modal.counter_empty",
        "감지된 얼굴 없음 — '+ 새 영역'으로 영역을 그리세요"
      );
      return;
    }
    el.textContent = _tn(
      "mask_modal.counter",
      "{total}개 중 {masked}개 가림 (클릭하여 해제)",
      { total: total, masked: masked }
    );
  }

  function _maskRenderOverlays() {
    const overlay = $("#mask-overlay");
    const img = $("#mask-img");
    if (!overlay || !img) return;
    overlay.innerHTML = "";
    // Use the rendered img size for box placement so the boxes scale
    // with whatever the browser actually painted (max-width / max-height
    // both clip in CSS). bbox is normalized [0..1] so we just multiply.
    const w = img.clientWidth;
    const h = img.clientHeight;
    if (!w || !h) return;
    _maskFaces.forEach((f, idx) => {
      const box = document.createElement("div");
      box.className = "face-box";
      box.dataset.idx = String(idx);
      box.dataset.mask = f.mask ? "1" : "0";
      box.style.left = (f.bbox[0] * w) + "px";
      box.style.top = (f.bbox[1] * h) + "px";
      box.style.width = (f.bbox[2] * w) + "px";
      box.style.height = (f.bbox[3] * h) + "px";
      // Confidence hint — only on detected boxes that weren't sure.
      if (f.source === "detected" && !f.high_confidence) {
        const lab = document.createElement("span");
        lab.className = "face-label";
        lab.textContent = "? " + Math.round((f.confidence || 0) * 100) + "%";
        box.appendChild(lab);
      }
      // 4 corner resize handles. pointerdown on a handle starts a
      // resize drag; stopPropagation keeps the box's toggle-on-click
      // from firing afterwards.
      ["tl", "tr", "bl", "br"].forEach(c => {
        const h = document.createElement("div");
        h.className = "face-handle";
        h.dataset.corner = c;
        h.addEventListener("pointerdown", (ev) => _maskBeginResize(ev, idx, c));
        box.appendChild(h);
      });
      // Delete button — user-drawn boxes only. Detected faces stay
      // toggleable but undeletable so users can always see what the
      // detector found (and toggle to opt out instead).
      if (f.source === "user") {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "face-delete";
        del.textContent = "×";
        del.title = _t("mask_modal.delete_rect", "이 영역 삭제");
        del.addEventListener("click", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          _maskFaces.splice(idx, 1);
          _maskRenderOverlays();
        });
        box.appendChild(del);
      }
      // Click on the box body toggles mask state. _maskDrag check
      // suppresses the toggle when the click was actually the end of
      // a resize drag (pointerup fires click).
      box.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (_maskDrag) return;
        f.mask = !f.mask;
        box.dataset.mask = f.mask ? "1" : "0";
        _maskUpdateCounter();
      });
      overlay.appendChild(box);
    });
    _maskUpdateCounter();
  }

  // ---- Resize drag --------------------------------------------------
  function _maskStageRect() {
    const img = $("#mask-img");
    return img ? img.getBoundingClientRect() : null;
  }

  function _maskBeginResize(ev, idx, corner) {
    ev.preventDefault();
    ev.stopPropagation();
    const rect = _maskStageRect();
    if (!rect) return;
    const f = _maskFaces[idx];
    if (!f) return;
    _maskDrag = {
      kind: "resize",
      idx,
      corner,
      startX: ev.clientX,
      startY: ev.clientY,
      startBbox: f.bbox.slice(),
      stageRect: rect,
    };
    try { ev.target.setPointerCapture(ev.pointerId); } catch (_) {}
    window.addEventListener("pointermove", _maskDragMove);
    window.addEventListener("pointerup", _maskDragEnd, { once: true });
  }

  function _maskDragMove(ev) {
    if (!_maskDrag) return;
    const d = _maskDrag;
    if (d.kind === "resize") {
      const f = _maskFaces[d.idx];
      if (!f) return;
      const dxN = (ev.clientX - d.startX) / d.stageRect.width;
      const dyN = (ev.clientY - d.startY) / d.stageRect.height;
      let [x, y, w, h] = d.startBbox;
      // Corner-specific transforms: anchor the opposite corner, move
      // the dragged one. Clamp to [0..1] and refuse to invert
      // (minimum size = 1px-ish → 0.005 normalized to keep handles
      // grabbable on small thumbs).
      const MIN = 0.005;
      if (d.corner === "tl") {
        const nx = Math.max(0, Math.min(x + w - MIN, x + dxN));
        const ny = Math.max(0, Math.min(y + h - MIN, y + dyN));
        w = w + (x - nx); x = nx;
        h = h + (y - ny); y = ny;
      } else if (d.corner === "tr") {
        const ny = Math.max(0, Math.min(y + h - MIN, y + dyN));
        w = Math.max(MIN, Math.min(1 - x, w + dxN));
        h = h + (y - ny); y = ny;
      } else if (d.corner === "bl") {
        const nx = Math.max(0, Math.min(x + w - MIN, x + dxN));
        w = w + (x - nx); x = nx;
        h = Math.max(MIN, Math.min(1 - y, h + dyN));
      } else { // br
        w = Math.max(MIN, Math.min(1 - x, w + dxN));
        h = Math.max(MIN, Math.min(1 - y, h + dyN));
      }
      f.bbox = [x, y, w, h];
      // Update DOM in place rather than re-rendering — re-render would
      // detach the in-flight handle and break pointer capture.
      const box = $(`#mask-overlay .face-box[data-idx="${d.idx}"]`);
      if (box) {
        const sw = d.stageRect.width;
        const sh = d.stageRect.height;
        box.style.left = (x * sw) + "px";
        box.style.top = (y * sh) + "px";
        box.style.width = (w * sw) + "px";
        box.style.height = (h * sh) + "px";
      }
    } else if (d.kind === "draw") {
      const dxN = (ev.clientX - d.startX) / d.stageRect.width;
      const dyN = (ev.clientY - d.startY) / d.stageRect.height;
      // Allow drawing in any direction — normalize to (left, top, w, h).
      const x0 = (d.startX - d.stageRect.left) / d.stageRect.width;
      const y0 = (d.startY - d.stageRect.top) / d.stageRect.height;
      let x = Math.max(0, Math.min(1, x0));
      let y = Math.max(0, Math.min(1, y0));
      let w = dxN, h = dyN;
      if (w < 0) { x = Math.max(0, x + w); w = -w; }
      if (h < 0) { y = Math.max(0, y + h); h = -h; }
      w = Math.min(1 - x, w);
      h = Math.min(1 - y, h);
      d.drawBbox = [x, y, w, h];
      if (d.drawingEl) {
        d.drawingEl.style.left = (x * d.stageRect.width) + "px";
        d.drawingEl.style.top = (y * d.stageRect.height) + "px";
        d.drawingEl.style.width = (w * d.stageRect.width) + "px";
        d.drawingEl.style.height = (h * d.stageRect.height) + "px";
      }
    }
  }

  function _maskDragEnd(ev) {
    window.removeEventListener("pointermove", _maskDragMove);
    if (!_maskDrag) return;
    const d = _maskDrag;
    if (d.kind === "draw" && d.drawBbox) {
      const [x, y, w, h] = d.drawBbox;
      // Refuse tiny accidental drags (a click that barely moved).
      if (w >= 0.01 && h >= 0.01) {
        _maskFaces.push({
          bbox: [x, y, w, h],
          mask: true,
          source: "user",
          confidence: null,
          high_confidence: true,
        });
      }
      if (d.drawingEl) d.drawingEl.remove();
      // Exit draw mode after one commit so the user can immediately
      // inspect/resize the new box. Re-toggle the button to draw
      // another.
      _maskSetDrawMode(false);
      _maskRenderOverlays();
    }
    _maskDrag = null;
    // Clear the suppress-toggle flag on next tick so the trailing
    // click event from this drag doesn't toggle the underlying box.
    setTimeout(() => { /* _maskDrag already null; sentinel kept short */ }, 0);
  }

  // ---- Draw mode ----------------------------------------------------
  function _maskSetDrawMode(on) {
    _maskDrawing = !!on;
    const stage = $("#mask-stage");
    const overlay = $("#mask-overlay");
    const btn = $("#mask-add-rect");
    if (stage) stage.classList.toggle("draw-mode", _maskDrawing);
    if (overlay) {
      if (_maskDrawing) overlay.dataset.draw = "1";
      else delete overlay.dataset.draw;
    }
    if (btn) btn.classList.toggle("active", _maskDrawing);
  }

  function _maskBeginDraw(ev) {
    if (!_maskDrawing) return;
    if (ev.button !== undefined && ev.button !== 0) return;
    const rect = _maskStageRect();
    if (!rect) return;
    ev.preventDefault();
    // Visible "currently being drawn" rectangle. Live-updates in
    // pointermove; committed (or discarded if tiny) on pointerup.
    const drawingEl = document.createElement("div");
    drawingEl.className = "face-drawing";
    $("#mask-overlay").appendChild(drawingEl);
    _maskDrag = {
      kind: "draw",
      startX: ev.clientX,
      startY: ev.clientY,
      stageRect: rect,
      drawingEl,
      drawBbox: null,
    };
    window.addEventListener("pointermove", _maskDragMove);
    window.addEventListener("pointerup", _maskDragEnd, { once: true });
  }

  async function openMaskModal() {
    const p = lightboxPhoto;
    if (!p) return;
    if (p.media_kind !== "image") {
      alert(_t("mask_modal.image_only",
        "사진(이미지)만 얼굴 가림 다운로드를 지원합니다."));
      return;
    }
    if (_maskBusy) return;
    _maskBusy = true;
    try {
      const res = await fetch(`/api/photos/${p.id}/faces`);
      if (!res.ok) {
        alert(await friendlyError(res,
          _t("mask_modal.load_failed", "얼굴 정보를 불러오지 못했습니다")));
        return;
      }
      const arr = (await res.json()) || [];
      // Default mask state: high-confidence faces ON, low-confidence
      // ones OFF (user can opt them IN if they really are faces).
      // Outward-pad the bbox a bit so YuNet's tight crop covers the
      // full face once blurred — user can still resize from there.
      // Empty arr is fine — open the modal anyway so the user can
      // draw custom rectangles with the "+ 새 영역" tool.
      _maskFaces = arr.map(f => {
        const [x, y, w, h] = f.bbox;
        const px = Math.max(0, x - w * _MASK_DETECTED_PAD);
        const py = Math.max(0, y - h * _MASK_DETECTED_PAD);
        const pw = Math.min(1 - px, w * (1 + 2 * _MASK_DETECTED_PAD));
        const ph = Math.min(1 - py, h * (1 + 2 * _MASK_DETECTED_PAD));
        return {
          bbox: [px, py, pw, ph],
          mask: !!f.high_confidence,
          source: "detected",
          confidence: f.confidence,
          high_confidence: f.high_confidence,
        };
      });
      _maskPhotoId = p.id;
      // Auto-enter draw mode when there are no detected faces — the
      // user's only useful action is to draw a custom area, so jump
      // straight to it (cursor already crosshair, ready to drag).
      // When faces ARE present, start in normal mode so the user
      // sees them first and can review/toggle.
      _maskSetDrawMode(_maskFaces.length === 0);
      const img = $("#mask-img");
      const msg = $("#mask-msg");
      if (msg) msg.textContent = "";
      // Use the 1024 thumb for the preview — much faster to load than
      // the original, and bbox coords are normalized so they scale.
      img.src = `/api/photos/${p.id}/thumb?size=1024`;
      img.onload = () => _maskRenderOverlays();
      maskModal.classList.add("show");
    } finally {
      _maskBusy = false;
    }
  }

  function closeMaskModal() {
    if (maskModal) maskModal.classList.remove("show");
    const overlay = $("#mask-overlay");
    if (overlay) overlay.innerHTML = "";
    const img = $("#mask-img");
    if (img) img.removeAttribute("src");
    _maskFaces = [];
    _maskPhotoId = null;
    _maskSetDrawMode(false);
    // Clear any in-flight drag so a stray pointermove can't mutate
    // stale state after close.
    if (_maskDrag) {
      window.removeEventListener("pointermove", _maskDragMove);
      _maskDrag = null;
    }
  }

  async function _maskDownload() {
    if (!_maskPhotoId || !lightboxPhoto) return;
    // Send the final rect list — only boxes the user has marked
    // mask=true. Source (detected vs user-drawn) doesn't matter
    // to the server; it just receives "mask these rectangles".
    const rects = _maskFaces.filter(f => f.mask).map(f => f.bbox);
    if (rects.length === 0) {
      // Empty download = same as plain ⬇ original. Warn rather than
      // silently re-encoding the original as JPEG.
      alert(_t("mask_modal.download_empty",
        "가릴 영역이 없습니다. 박스를 활성화하거나 '+ 새 영역'으로 그리세요. " +
        "원본만 받으려면 모달을 닫고 ⬇ 버튼을 사용하세요."));
      return;
    }
    const btn = $("#mask-download");
    const msg = $("#mask-msg");
    btn.disabled = true;
    if (msg) msg.textContent = _t("mask_modal.processing", "처리 중…");
    try {
      const res = await fetch(`/api/photos/${_maskPhotoId}/download-masked`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rects }),
      });
      if (!res.ok) {
        if (msg) {
          msg.textContent = await friendlyError(res,
            _t("mask_modal.download_failed", "다운로드 실패"));
        }
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const base = (lightboxPhoto.filename || "image").replace(/\.[^.]+$/, "");
      a.download = base + "-masked.jpg";
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke after a tick so the browser starts the save first.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      closeMaskModal();
    } finally {
      btn.disabled = false;
      if (msg) msg.textContent = "";
    }
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
      if (i >= 0) photos[i].sha256 = freshSha;   // keep timeline state in sync
      // Refresh EVERY on-page thumbnail for this photo — timeline grid,
      // the map sidebar, the filmstrip, anywhere — not just the timeline
      // tile. The cache-buster forces a re-fetch of the rotated art (map
      // sidebar thumbs carry no ?v= of their own and are cached immutable,
      // so without this they'd keep showing the pre-rotation image).
      const pid = p.id;
      document.querySelectorAll("img").forEach((im) => {
        const src = im.getAttribute("src") || "";
        if (src.indexOf(`/api/photos/${pid}/thumb`) === -1) return;
        const m = src.match(/[?&]size=(\d+)/);
        const size = m ? m[1] : 256;
        im.src = `/api/photos/${pid}/thumb?size=${size}&v=${freshSha}&_t=${ts}`;
      });
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
    gpsModal = $("#gps-modal");
    maskModal = $("#mask-modal");

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
    // Undecodable video → lazily build + load an H.264 proxy. "error"
    // catches bad containers/codecs; "loadedmetadata" catches files that
    // play audio but render no video track (videoWidth stays 0).
    lbVideo.addEventListener("error", _onVideoError);
    lbVideo.addEventListener("loadedmetadata", _onVideoLoadedMeta);

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
        // Yield to whichever modal is on top. closeXxx swallows the
        // keystroke; lightbox stays open under the modal stack.
        if (maskModal && maskModal.classList.contains("show")) {
          closeMaskModal();
          return;
        }
        if (gpsModal && gpsModal.classList.contains("show")) {
          closeGpsModal();
          return;
        }
        if (dateModal && dateModal.classList.contains("show")) {
          closeDateModal();
          return;
        }
        closeLightbox();
      }
      else if (e.key === "ArrowLeft") showPrev();
      else if (e.key === "ArrowRight") showNext();
      else if (e.key === "i" || e.key === "I") toggleDetails();
      // Delete → trash the current photo. Route through the menu button so
      // the permission/readonly lock (it's .disabled when write-blocked) and
      // the confirm() prompt both apply — a disabled button's click is a
      // no-op. The INPUT/TEXTAREA/SELECT guard above means Del while editing
      // a comment/tag won't fire this.
      else if (e.key === "Delete") {
        e.preventDefault();
        const del = document.querySelector("#menu-delete");
        if (del && !del.disabled) del.click();
      }
    });

    // Touch swipe (mobile) — horizontal drag flips prev/next.
    lb.addEventListener("touchstart", (e) => {
      if (!lb.classList.contains("show")) return;
      // While zoomed in, one-finger drags pan the image (handled by the
      // image's own listeners) — never prev/next/close. Don't arm swipe.
      if (_lbZoom.scale > 1 || e.touches.length === 2) { _lbTouch = null; return; }
      if (e.touches.length !== 1) { _lbTouch = null; return; }
      if (e.target.closest("video")) { _lbTouch = null; return; }
      const t = e.touches[0];
      // Remember whether the drag began on the photo area (.lb-main) so a
      // swipe-down-to-close can't fire while scrolling the details panel.
      const onMain = !!(e.target.closest && e.target.closest(".lb-main"));
      _lbTouch = { x: t.clientX, y: t.clientY, onMain };
    }, { passive: true });
    lb.addEventListener("touchend", (e) => {
      if (!_lbTouch) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - _lbTouch.x;
      const dy = t.clientY - _lbTouch.y;
      const onMain = _lbTouch.onMain;
      _lbTouch = null;
      // Swipe DOWN to dismiss — only when the drag started on the photo
      // area and is clearly vertical, and not while a sub-modal owns the
      // gesture (GPS / date / mask editor open on top).
      const subModalOpen =
        (maskModal && maskModal.classList.contains("show")) ||
        (gpsModal && gpsModal.classList.contains("show")) ||
        (dateModal && dateModal.classList.contains("show"));
      if (onMain && !subModalOpen
          && dy > CLOSE_PX && Math.abs(dy) > Math.abs(dx) * 1.5) {
        closeLightbox();
        return;
      }
      // Horizontal swipe → prev / next.
      if (Math.abs(dx) < SWIPE_PX) return;
      if (Math.abs(dx) < Math.abs(dy) * 1.5) return;
      if (dx > 0) showPrev();
      else showNext();
    }, { passive: true });

    // --- Pinch-zoom / double-tap / drag-pan (still images only) ---
    // Listeners live on #lb-img so they sit "below" the bubbling `lb`
    // swipe handlers and can preventDefault/stopPropagation to win the
    // gesture while zoomed. touchmove must be { passive: false } so its
    // preventDefault is honoured; touchAction:none keeps the browser
    // from claiming the gesture for native scroll/zoom.
    lbImg.style.touchAction = "none";
    lbImg.addEventListener("touchstart", _zoomTouchStart, { passive: false });
    lbImg.addEventListener("touchmove", _zoomTouchMove, { passive: false });
    lbImg.addEventListener("touchend", _zoomTouchEnd, { passive: false });
    lbImg.addEventListener("touchcancel", _zoomTouchEnd, { passive: false });

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

    // ⇄ convert-download. Images (PNG) just follow the anchor's href; videos
    // need the H.264 proxy (built lazily), so intercept and run the MP4 flow.
    $("#menu-convert").addEventListener("click", (e) => {
      const p = lightboxPhoto;
      if (p && p.media_kind === "video") {
        e.preventDefault();
        _downloadVideoMp4(p);
      }
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

    // --- Face-mask download modal wiring ------------------------
    const mkBtn = $("#menu-mask-download");
    if (mkBtn) mkBtn.addEventListener("click", openMaskModal);
    if (maskModal) {
      $("#mask-cancel")?.addEventListener("click", closeMaskModal);
      $("#mask-download")?.addEventListener("click", _maskDownload);
      $("#mask-add-rect")?.addEventListener("click", () => {
        _maskSetDrawMode(!_maskDrawing);
      });
      // Pointerdown on the overlay starts a new-box draw when in
      // draw mode. The overlay's pointer-events: auto (via
      // data-draw="1") + .face-box pointer-events: none (CSS rule)
      // mean this handler reliably catches the gesture even when it
      // starts on top of an existing detected box.
      $("#mask-overlay")?.addEventListener("pointerdown", _maskBeginDraw);
      // Belt+braces: kill native image-drag (the browser's HTML5
      // drag-and-drop for <img> elements). Even with the overlay on
      // top in draw mode, some browsers fire dragstart on the image
      // beneath if the gesture starts there before the overlay
      // claims it. preventDefault on dragstart is the canonical
      // shut-it-off.
      $("#mask-img")?.addEventListener("dragstart", (e) => e.preventDefault());
      // Backdrop click closes; clicks INSIDE the .box are stopped by
      // the modal's stacking context.
      maskModal.addEventListener("click", (e) => {
        if (e.target === maskModal) closeMaskModal();
      });
      // Re-layout overlays on window resize so boxes stay aligned
      // when the user resizes the browser mid-selection.
      window.addEventListener("resize", () => {
        if (maskModal.classList.contains("show")) _maskRenderOverlays();
      });
    }

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

    // --- GPS modal handlers ----------------------------------
    $("#gps-cancel").addEventListener("click", closeGpsModal);
    gpsModal.addEventListener("click", (e) => {
      if (e.target === gpsModal) closeGpsModal();
    });
    $("#gps-save").addEventListener("click", _gpsSave);
    $("#gps-clear").addEventListener("click", () => {
      // "Clear" is a UI affordance only — until the user clicks 저장
      // the on-disk row is untouched. So drop the marker locally and
      // let _gpsSave send {null, null} to actually delete the row.
      _gpsClearMarker();
    });
    $("#gps-locate-me").addEventListener("click", () => {
      if (!navigator.geolocation) {
        $("#gps-msg").textContent = _t("map.locate_unavailable",
          "이 브라우저는 위치 정보를 지원하지 않습니다.");
        return;
      }
      const btn = $("#gps-locate-me");
      btn.disabled = true;
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          btn.disabled = false;
          const lat = pos.coords.latitude, lng = pos.coords.longitude;
          _gpsMap.flyTo([lat, lng], Math.max(15, _gpsMap.getZoom()),
            { duration: 0.5 });
          _gpsPlaceMarker(lat, lng);
        },
        (err) => {
          btn.disabled = false;
          $("#gps-msg").textContent =
            (err && err.code === err.PERMISSION_DENIED)
              ? _t("map.locate_denied",
                  "위치 권한이 거부되었습니다. 브라우저 설정에서 허용해 주세요.")
              : _t("map.locate_failed", "현재 위치를 가져올 수 없습니다.")
                  + (err && err.message ? " (" + err.message + ")" : "");
        },
        { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
      );
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
    // Bulk-bar GPS edit entry point. Doesn't open the photo lightbox
    // itself — just reuses the GPS picker modal (which lives in this
    // module) in bulk mode against the supplied ids.
    openGpsBulk: openGpsModalBulk,
  };
})();
