/* Minimal service worker: caches the app shell so the controller works
 * even with a flaky connection. Network-first for navigation so updates
 * roll out without a hard refresh. */

const CACHE = "blockyt-shell-v1";
const SHELL = [
  "./",
  "./index.html",
  "./app.js",
  "./styles.css",
  "./manifest.webmanifest",
  "./icon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache ntfy traffic.
  if (url.hostname.endsWith("ntfy.sh") || url.pathname.includes("/json") || url.pathname.includes("/sse")) {
    return;
  }
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp.ok && resp.type === "basic") {
          const clone = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(e.request).then((r) => r || Response.error())),
  );
});
