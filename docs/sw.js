// Offline shell + notifications. iOS requires every push to show a notification, so we always do.
const CACHE = "commute-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (new URL(e.request.url).origin !== location.origin) return;   // ntfy / GitHub calls go straight out
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request)));
});
self.addEventListener("push", (e) => {
  let d = { title: "Commute", body: "" };
  try { d = e.data.json(); } catch (_) { d.body = e.data ? e.data.text() : ""; }
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, tag: d.tag || "commute", renotify: true, icon: "icon-192.png", badge: "icon-192.png",
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((ws) =>
    ws.length ? ws[0].focus() : clients.openWindow("./")));
});
