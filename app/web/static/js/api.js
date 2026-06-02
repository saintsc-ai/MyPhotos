/*
 * MyPhotos API helpers.
 *
 * Per-page usage:
 *   <script src="/js/api.js"></script>   <!-- AFTER /js/common.js -->
 *
 * Side effects at load time:
 *   - Wraps window.fetch so any 401 response bounces the user to
 *     /login.html. Keyed off the URL so /api/auth/me itself doesn't
 *     redirect-loop. Both index.html and admin.html benefit — admin
 *     previously had no 401 handling, so a session-expired admin saw
 *     a perpetually-loading panel.
 *
 * Globals exposed:
 *   window.friendlyError(res, action) -> Promise<string>
 *     Same shape index.html had inline. Pulls the FastAPI `detail`
 *     field when present, falls back to a humanised per-status
 *     sentence ("권한이 없어 {action}할 수 없습니다." etc.).
 *
 *   window.api = {
 *     get(url, opts?),                 // -> parsed JSON, or null on 204
 *     post(url, body?, opts?),         // body auto JSON.stringified
 *     patch(url, body?, opts?),        // same
 *     put(url, body?, opts?),          // same
 *     del(url, opts?),                 // delete is a reserved word
 *   }
 *
 * Helpers throw ApiError on !res.ok with the friendlyError-derived
 * message + status + url, so a typical caller is:
 *
 *   try {
 *     const data = await api.get("/api/photos?page=1");
 *     ...
 *   } catch (e) {
 *     alert(e.message);   // already humanised
 *   }
 *
 * Endpoints that stream bytes (zip download, thumbnails, originals)
 * SHOULD continue to use window.fetch directly so the caller can
 * inspect the raw Response. The api.* helpers are for "fetch some
 * JSON, throw on error" — the 90% case.
 */
(function () {
  "use strict";

  // ---- 401-redirect wrap ----------------------------------------
  // Idempotent: if api.js gets loaded twice for some reason, don't
  // wrap the wrapper. (Guard via a sentinel property.)
  if (!window.fetch.__myphotosWrapped) {
    const _origFetch = window.fetch.bind(window);

    // Retry idempotent GETs on transient network failures. When the
    // connection drops mid-request, fetch() *rejects* with a TypeError
    // ("Failed to fetch") instead of returning a response — on flaky
    // Wi-Fi / mobile / Tailscale links a single dropped packet otherwise
    // surfaces as a hard error (e.g. the timeline's "오류: Failed to
    // fetch"). Only GETs are retried (safe to repeat); HTTP error
    // *responses* and aborts pass through untouched.
    const _GET_RETRY_DELAYS = [300, 900]; // ms waited before each retry
    const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const _isGet = (args) => {
      const init = args[1];
      return !init || !init.method || String(init.method).toUpperCase() === "GET";
    };
    const _isAborted = (args) => {
      const init = args[1];
      return !!(init && init.signal && init.signal.aborted);
    };

    const wrapped = async function (...args) {
      const attempts = _isGet(args) ? _GET_RETRY_DELAYS.length + 1 : 1;
      let lastErr;
      for (let i = 0; i < attempts; i++) {
        try {
          const res = await _origFetch(...args);
          if (res.status === 401) {
            // /auth/me is the bootstrap check on every page — letting it
            // redirect would loop the login page back to itself. The
            // login form itself naturally posts to /auth/login and that
            // 401-on-bad-password should also stay where it is.
            const url = String(args[0] || "");
            if (!url.endsWith("/api/auth/me")
                && !url.endsWith("/api/auth/login")) {
              location.replace("/login.html");
            }
          }
          return res;
        } catch (e) {
          // A user/programmatic abort must propagate immediately, never retry.
          if ((e && e.name === "AbortError") || _isAborted(args)) throw e;
          lastErr = e;
          if (i < attempts - 1) await _sleep(_GET_RETRY_DELAYS[i]);
        }
      }
      throw lastErr;
    };
    wrapped.__myphotosWrapped = true;
    window.fetch = wrapped;
  }

  // ---- friendlyError --------------------------------------------
  // Pull a user-friendly Korean message out of a fetch Response.
  // FastAPI's HTTPException.detail wins; falls back to a humanised
  // per-status sentence. `action` is woven into the fallback so the
  // sentence reads ("권한이 없어 **삭제**할 수 없습니다.").
  window.friendlyError = async function (res, action) {
    action = action || "작업";
    let detail = "";
    try {
      const d = await res.clone().json();
      if (d && typeof d.detail === "string" && d.detail.trim()) {
        detail = d.detail.trim();
      }
    } catch (_) { /* not JSON */ }
    if (detail) return detail;
    switch (res.status) {
      case 400: return `요청이 올바르지 않아 ${action}할 수 없습니다.`;
      case 401: return "로그인 세션이 만료되었습니다. 다시 로그인해주세요.";
      case 403: return `권한이 없어 ${action}할 수 없습니다.`;
      case 404: return `대상을 찾을 수 없습니다 (${action} 실패).`;
      case 409: return `다른 설정과 충돌하여 ${action}할 수 없습니다.`;
      case 413: return "파일이 너무 큽니다.";
      case 429: return "요청이 너무 잦습니다. 잠시 후 다시 시도하세요.";
      case 500:
      case 502:
      case 503:
      case 504:
        return `서버에 문제가 발생했습니다 (${action} 실패). 잠시 후 다시 시도하세요.`;
      default:
        return `${action} 실패 (HTTP ${res.status})`;
    }
  };

  // ---- ApiError --------------------------------------------------
  // Plain Error subclass so callers can `catch (e) { if (e.status ===
  // 404) ... }` without needing a separate type-import dance.
  class ApiError extends Error {
    constructor(message, status, url) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.url = url;
    }
  }
  window.ApiError = ApiError;

  // ---- api.* helpers --------------------------------------------
  async function _request(method, url, body, opts) {
    opts = opts || {};
    const init = { method, headers: {} };
    if (body !== undefined && body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = typeof body === "string" ? body : JSON.stringify(body);
    }
    if (opts.headers) {
      for (const k of Object.keys(opts.headers)) init.headers[k] = opts.headers[k];
    }
    if (opts.signal) init.signal = opts.signal;

    const res = await window.fetch(url, init);
    if (!res.ok) {
      const action = opts.action || _defaultActionFor(method);
      const msg = await window.friendlyError(res, action);
      throw new ApiError(msg, res.status, url);
    }
    if (res.status === 204) return null;
    // Some endpoints (CSV / plain text) return non-JSON on success.
    // Honour `opts.raw` so the caller can read them.
    if (opts.raw) return res;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res.text();
  }

  function _defaultActionFor(method) {
    switch (method) {
      case "GET":    return "조회";
      case "POST":   return "처리";
      case "PUT":    return "저장";
      case "PATCH":  return "수정";
      case "DELETE": return "삭제";
      default:       return "작업";
    }
  }

  window.api = {
    get:   (url, opts)       => _request("GET",    url, null, opts),
    post:  (url, body, opts) => _request("POST",   url, body, opts),
    put:   (url, body, opts) => _request("PUT",    url, body, opts),
    patch: (url, body, opts) => _request("PATCH",  url, body, opts),
    del:   (url, opts)       => _request("DELETE", url, null, opts),
  };
})();
