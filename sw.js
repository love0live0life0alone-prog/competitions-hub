// ============================================================
// Service Worker — University Competitions Hub
// - Cache-first for the app shell (HTML/CSS/JS/icons)
// - Network-first with cache fallback for API/data calls
// - Push notifications (works even if the app tab is closed)
// - Basic Background Sync queue for offline interactions
// ============================================================

const APP_SHELL_CACHE = "uch-shell-v7";
const DATA_CACHE = "uch-data-v7";

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
    caches.open(APP_SHELL_CACHE).then((cache) =>
      // cache:"reload" skips the browser HTTP cache, so a freshly installed worker
      // never precaches a stale (up to 10 min old on GitHub Pages) copy of the pages.
      Promise.all(APP_SHELL_FILES.map((file) => cache.add(new Request(file, { cache: "reload" }))))
    )
  );
  // Do NOT auto skipWaiting anymore — a new SW now waits until the page
  // explicitly asks it to activate (see the "message" listener below), so
  // an update prompt can be shown first instead of silently switching
  // versions under an open session.
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
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

  // Firestore / Auth / Installations / FCM-registration traffic (…googleapis.com) must go
  // straight to the network: Firestore has its own IndexedDB offline cache and its
  // streaming channel requests must never be cloned into the Cache API.
  // (fonts.googleapis.com is still handled below so fonts keep working offline.)
  if (url.hostname.endsWith(".googleapis.com") && url.hostname !== "fonts.googleapis.com") return;

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

  // Everything below is the app shell / static assets: plain GET requests only.
  if (request.method !== "GET") return;

  const isNavigation = request.mode === "navigate";

  // Keeps the worker alive until the background refresh below has finished, so a
  // cached page is served instantly AND the cache is still updated with the latest copy.
  let finishRefresh;
  event.waitUntil(new Promise((resolve) => { finishRefresh = resolve; }));

  // Stale-while-revalidate: cached copy first, network refresh in the background.
  // Navigations ignore the query string, so deep links / reloads such as
  // "home.html?competition=ID" still resolve to the cached home.html when offline.
  event.respondWith(
    caches.match(request, { ignoreSearch: isNavigation }).then((cached) => {
      const network = fetch(request).then((response) => {
        // Only store usable responses (or opaque cross-origin ones such as images/scripts):
        // a 404/500 must never overwrite a good cached copy. Pages are stored without
        // their query string so there is one cached copy per page.
        if (request.url.startsWith("http") && response && (response.ok || response.type === "opaque")) {
          const clone = response.clone();
          const cacheKey = isNavigation ? url.origin + url.pathname : request;
          caches.open(APP_SHELL_CACHE).then((cache) => cache.put(cacheKey, clone));
        }
        return response;
      });
      network.then(finishRefresh, finishRefresh);

      if (cached) return cached;

      // Nothing cached: use the network; if that fails (offline) fall back to the cached
      // app shell for page navigations instead of showing a browser error page.
      return network
        .catch(() => (isNavigation ? caches.match("./home.html") : undefined))
        .then((res) => res || Response.error());
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
  // Resolved against the SW scope (not the origin root) so it also works when the app
  // is hosted under a sub-path such as https://<user>.github.io/competitions-hub/
  const targetUrl = new URL(
    targetId ? `./home.html?competition=${encodeURIComponent(targetId)}` : "./home.html",
    self.registration.scope
  ).href;

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

 
