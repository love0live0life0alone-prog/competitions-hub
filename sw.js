// ============================================================
// Service Worker — University Competitions Hub
// - Cache-first for the app shell (HTML/CSS/JS/icons)
// - Network-first with cache fallback for API/data calls
// - Push notifications (works even if the app tab is closed)
// - Basic Background Sync queue for offline interactions
// ============================================================

const APP_SHELL_CACHE = "uch-shell-v6";
const DATA_CACHE = "uch-data-v6";

const APP_SHELL_FILES = [
  "./",
  "./index.html",
  "./home.html",
  "./admin.html",
  "./manifest.json"
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
    // Only GET responses are cacheable — the Cache API rejects POST/PUT/etc.
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (request.method === "GET" && response && response.status === 200) {
            const clone = response.clone();
            caches.open(DATA_CACHE).then((cache) => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => (request.method === "GET" ? caches.match(request) : Response.error()))
    );
    return;
  }

  // App shell: cache-first, refresh cache in the background.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          // الكاش الوسيط بيدعم بس طلبات GET على http/https — أي حاجة تانية
          // (POST، أو طلبات إضافات كروم زي chrome-extension://) لازم تتجاهل
          // هنا عشان متعملش reject/uncaught error، مع إنها مش مشكلة حقيقية.
          if (request.method === "GET" && request.url.startsWith("http")) {
            const clone = response.clone();
            caches.open(APP_SHELL_CACHE).then((cache) => cache.put(request, clone));
          }
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
  let payload = {
    title: "🎯 إعلان جديد",
    body: "في تحديث جديد على تطبيق المسابقات — دوس لعرض التفاصيل",
    competitionId: null,
  };
  try {
    const raw = event.data.json();
    payload = {
      title: (raw.notification && raw.notification.title) || raw.title || payload.title,
      body: (raw.notification && raw.notification.body) || raw.body || payload.body,
      competitionId: (raw.data && raw.data.competitionId) || raw.competitionId || null,
    };
  } catch (e) {
    /* fall back to default payload above */
  }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "./icon-192.png",
      badge: "./icon-192.png",
      image: "./icon-512.png",
      vibrate: [200, 100, 200],
      renotify: true,
      tag: payload.competitionId ? `comp-${payload.competitionId}` : "general",
      data: { competitionId: payload.competitionId || null },
      dir: "rtl",
      lang: "ar",
      actions: [{ action: "open", title: "عرض التفاصيل 👀" }],
    })
  );
});

// Clicking the notification opens (or focuses) the competition detail.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetId = event.notification.data && event.notification.data.competitionId;
  const targetUrl = targetId ? `/home.html?competition=${targetId}` : "/home.html";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientsArr) => {
      const existing = clientsArr.find((c) => c.url.includes("home.html") || c.url.includes("index.html"));
      if (existing) {
        existing.focus();
        existing.postMessage({ type: "open-competition", competitionId: targetId });
        return;
      }
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
