/* MyPhotos service worker — offline app shell + thumbnail cache.
 *
 * Registered ONLY in a secure context (HTTPS / localhost) — see
 * /js/pwa-register.js, which is guarded by `window.isSecureContext`. Over
 * plain http:// this file is never loaded and has ZERO effect; the site
 * behaves exactly as if no service worker existed.
 *
 * Strategy (GET + same-origin only):
 *   - app shell (HTML pages, /js, /i18n, /icons, manifest): cache-first
 *     with a background refresh (stale-while-revalidate) — instant repeat
 *     loads, self-heals on the next visit after a deploy.
 *   - thumbnails (/api/photos/<id>/thumb...): cache-first, capped — the
 *     URL carries a ?v= cache-buster so each version is its own key, so a
 *     hit is always valid and recently-seen photos survive a flaky network.
 *   - every OTHER /api/* (gallery lists, photo details, auth, writes): NOT
 *     intercepted → always the live network, so nothing dynamic or
 *     auth/user-scoped is ever served stale.
 *
 * Bump VERSION to force every client to drop old cached shell assets
 * (e.g. after a frontend change that must not be served stale).
 */
const VERSION = "v10";
const SHELL_CACHE = `myphotos-shell-${VERSION}`;
const THUMB_CACHE = `myphotos-thumb-${VERSION}`;
const THUMB_MAX = 600; // cap cached thumbnails (~several screens of 256px)

const SHELL = [
  "/", "/index.html", "/login.html", "/share.html",
  "/js/common.js", "/js/api.js",
  "/manifest.json",
  "/icons/icon-192.png", "/icons/icon-512.png",
  "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      // Per-file add via allSettled so one 404 can't wedge the install.
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL_CACHE && k !== THUMB_CACHE)
            .map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

function isThumb(url) {
  return url.pathname.startsWith("/api/photos/") && url.pathname.endsWith("/thumb");
}

function isShellAsset(url) {
  const p = url.pathname;
  return p === "/" || p.endsWith(".html")
    || p.startsWith("/js/") || p.startsWith("/i18n/") || p.startsWith("/icons/")
    || p === "/manifest.json"
    || p.endsWith(".css") || p.endsWith(".svg")
    || p.endsWith(".woff") || p.endsWith(".woff2");
}

function cacheable(res) {
  // Same-origin, fully-readable, OK responses only. Skips opaque/redirect
  // responses and any non-200 (e.g. a 401/403 thumb when unauthenticated).
  return res && res.status === 200 && res.type === "basic";
}

async function trim(cache, max) {
  const keys = await cache.keys();
  if (keys.length <= max) return;
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

async function cacheFirst(cacheName, request, opts) {
  opts = opts || {};
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) {
    if (opts.revalidate) {
      // Refresh in the background; don't block the cached response.
      fetch(request).then((res) => {
        if (cacheable(res)) cache.put(request, res.clone());
      }).catch(() => {});
    }
    return hit;
  }
  try {
    const res = await fetch(request);
    if (cacheable(res)) {
      cache.put(request, res.clone());
      if (opts.cap) trim(cache, opts.cap);
    }
    return res;
  } catch (e) {
    return Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return; // leave map tiles etc. alone

  if (isThumb(url)) {
    event.respondWith(cacheFirst(THUMB_CACHE, req, { cap: THUMB_MAX }));
    return;
  }
  // Other /api/* — dynamic / auth-scoped: never cache, always live network.
  if (url.pathname.startsWith("/api/")) return;

  if (req.mode === "navigate") {
    // Network-first so login/redirect state is always fresh; fall back to
    // a cached page (then the app shell) only when the network is down.
    event.respondWith(
      fetch(req).catch(() =>
        caches.match(req).then((r) => r || caches.match("/")))
    );
    return;
  }

  if (isShellAsset(url)) {
    event.respondWith(cacheFirst(SHELL_CACHE, req, { revalidate: true }));
    return;
  }
  // Anything else: default network (no respondWith).
});
