// ============================================================
// Service Worker — University Competitions Hub
// - Cache-first for the app shell (HTML/CSS/JS/icons)
// - Network-first with cache fallback for API/data calls
// - Push notifications (works even if the app tab is closed)
// - Basic Background Sync queue for offline interactions
// ============================================================

const APP_SHELL_CACHE = "uch-shell-v2";
const DATA_CACHE = "uch-data-v2";

const APP_SHELL_FILES = [
  "/",
  "/index.html",
  "/home.html",
  "/admin.html",
  "/manifest.json"
];

// ---------- Install: pre-cache the app shell ----------
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(APP_SHELL_CACHE).then((cache) => cache.addAll(APP_SHELL_FILES))
  );
  self.skipWaiting();
});

// ---------- Activate: clean up old cache versions ----------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => ![APP_SHELL_CACHE, DATA_CACHE].includes(key))
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

// ---------- Fetch strategy ----------
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Treat calls to your Flask API (Railway) as "data" requests.
  const isApiCall = url.pathname.startsWith("/api/");

  if (isApiCall) {
    // Network-first, fall back to last cached data when offline.
    event.respondWith(
      fetch(request)
        .then((response) => {
          const clone = response.clone();
          caches.open(DATA_CACHE).then((cache) => cache.put(request, clone));
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // App shell: cache-first, refresh cache in the background.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          const clone = response.clone();
          caches.open(APP_SHELL_CACHE).then((cache) => cache.put(request, clone));
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});

// ---------- Push notifications ----------
// Triggered by Firebase Cloud Messaging even when no tab is open.
self.addEventListener("push", (event) => {
  let payload = { title: "إعلان جديد", body: "في مسابقة جديدة على التطبيق" };
  try {
    payload = event.data.json();
  } catch (e) {
    /* fall back to default payload above */
  }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/assets/icon-192.png",
      badge: "/assets/icon-192.png",
      data: { competitionId: payload.competitionId || null },
      dir: "rtl",
      lang: "ar"
    })
  );
});

// Clicking the notification opens (or focuses) the competition detail.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetId = event.notification.data && event.notification.data.competitionId;
  const targetUrl = targetId ? `/index.html?competition=${targetId}` : "/index.html";

  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((clientsArr) => {
      const existing = clientsArr.find((c) => c.url.includes("/index.html"));
      if (existing) return existing.focus();
      return self.clients.openWindow(targetUrl);
    })
  );
});

// ---------- Background Sync ----------
// Queues interactions (e.g. link clicks) recorded while offline
// and flushes them to the backend once connectivity returns.
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-interactions") {
    event.waitUntil(flushQueuedInteractions());
  }
});

async function flushQueuedInteractions() {
  const cache = await caches.open(DATA_CACHE);
  const queued = await cache.match("queued-interactions");
  if (!queued) return;

  const items = await queued.json();
  for (const item of items) {
    try {
      await fetch("/api/interactions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(item)
      });
    } catch (e) {
      // still offline — leave the queue intact and try again next sync
      return;
    }
  }
  await cache.delete("queued-interactions");
}
