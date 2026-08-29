// AI AURA PWA service worker — caches the app shell for offline load.
// API calls are always network (never cached) so signals are live.
const CACHE = "aiaura-shell-v1";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/icon.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache API responses — signals and health must be live.
  if (url.pathname.startsWith("/api/")) return;
  if (e.request.method !== "GET") return;
  // Cache-first for the shell, falling back to network.
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return resp;
    }).catch(() => hit))
  );
});
