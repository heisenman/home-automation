// Service worker — caches the app SHELL only (so the UI loads offline), never the API.
// Live device state always goes to the network; if the box is unreachable the app shows last-known.
const CACHE = "ha-shell-v6";   // bump on any shell (html/js/css) change to evict the old cache
const SHELL = [
  "/app/", "/app/index.html", "/app/app.js", "/app/styles.css",
  "/app/vendor/preact-htm.standalone.module.js", "/app/manifest.webmanifest", "/app/icon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // API + control are always live — let them hit the network (no respondWith = default fetch).
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/control/") ||
      url.pathname.startsWith("/devices") || url.pathname.startsWith("/health")) {
    return;
  }
  // App shell: cache-first, fall back to network (and refresh the cache opportunistically).
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      if (res.ok && e.request.method === "GET") {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      }
      return res;
    })),
  );
});
