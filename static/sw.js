/* Smart DIT Learning Hub service worker.
   - Versioned CSS/JS (?v=hash) are served cache-first, so repeat visits on a
     slow connection load instantly; older versions are pruned automatically.
   - Page navigations always go to the network; if it is unreachable the
     generic offline page is shown instead of the browser's error screen.
   - Signed-in pages, API responses and private resource files are never
     cached, so nothing personal is left behind on a shared phone. */
"use strict";

var STATIC_CACHE = "smart-dit-static-v1";
var OFFLINE_CACHE = "smart-dit-offline-v1";
var OFFLINE_URL = "/offline";

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(OFFLINE_CACHE)
      .then(function (cache) { return cache.add(new Request(OFFLINE_URL, { credentials: "omit" })); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  var keep = [STATIC_CACHE, OFFLINE_CACHE];
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (key) { return keep.indexOf(key) === -1; })
          .map(function (key) { return caches.delete(key); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

function cacheFirst(request, url) {
  return caches.open(STATIC_CACHE).then(function (cache) {
    return cache.match(request).then(function (hit) {
      if (hit) return hit;
      return fetch(request).then(function (response) {
        if (!response.ok) return response;
        var copy = response.clone();
        cache.keys().then(function (entries) {
          entries.forEach(function (entry) {
            if (new URL(entry.url).pathname === url.pathname) cache.delete(entry);
          });
          cache.put(request, copy);
        });
        return response;
      });
    });
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(function () { return caches.match(OFFLINE_URL); }));
    return;
  }
  if (url.pathname.indexOf("/static/") === 0 && url.searchParams.has("v")) {
    event.respondWith(cacheFirst(request, url));
  }
});
