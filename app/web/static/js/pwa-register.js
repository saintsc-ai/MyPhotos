/* Register the MyPhotos service worker — but ONLY in a secure context.
 *
 * Service workers are a secure-context feature: they exist on HTTPS and on
 * http://localhost, but NOT on a plain http://NAS:8888 LAN origin. The
 * `window.isSecureContext` guard means that over HTTP this whole block is
 * a no-op — no registration, no caching, no errors — so the app behaves
 * exactly as it did before. Offline caching simply switches on once the
 * site is served over HTTPS (see the README "HTTPS 설정" section).
 */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {
      /* registration failure is non-fatal — app works without it */
    });
  });
})();
