// Service worker — caches the app SHELL only (so the UI loads offline), never the API.
// Live device state always goes to the network; if the box is unreachable the app shows last-known.
const CACHE = "ha-shell-v31";   // v31: LED night mode
const SHELL = [
  "/app/", "/app/index.html", "/app/app.js", "/app/push.js", "/app/styles.css",
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

// Web Push — payload-less "tickle": wake, fetch the current alerts, surface the most severe new one.
// We deliberately carry NO payload (server signs VAPID only, no aes128gcm), so the SW pulls the truth
// from /api/v1/alerts itself — the single source of alert rules.
self.addEventListener("push", (e) => {
  e.waitUntil((async () => {
    let alerts = [];
    try { alerts = await (await fetch("/api/v1/alerts", { cache: "no-store" })).json().then((d) => d.alerts || []); }
    catch (_) { /* offline: still show a generic nudge below */ }
    const rank = { critical: 0, warning: 1, info: 2 };
    alerts.sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3));
    const top = alerts[0];
    const title = top ? `${top.name}: ${top.detail}` : "Home Automation alert";
    const body = alerts.length > 1 ? `+${alerts.length - 1} more alert(s)` : (top ? top.severity : "Tap to open");
    await self.registration.showNotification(title, {
      body, icon: "/app/icon.svg", badge: "/app/icon.svg",
      tag: "ha-alerts", renotify: true, data: { url: "/app/" },
    });
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/app/";
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) { if (c.url.includes("/app") && "focus" in c) return c.focus(); }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  })());
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // API + control are always live — let them hit the network (no respondWith = default fetch).
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/control/") ||
      url.pathname.startsWith("/devices") || url.pathname.startsWith("/health")) {
    return;
  }
  // App shell: NETWORK-FIRST (always serve the freshest code when the box is reachable; fall back to the
  // cached shell only when offline). Cache-first was wrong for an actively-developed app — it stranded
  // users on stale JS until a cache-version bump + double reload. On a LAN the extra round-trip is nil.
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok && e.request.method === "GET") {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      }
      return res;
    }).catch(() => caches.match(e.request)),
  );
});
