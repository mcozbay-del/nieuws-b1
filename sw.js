// Network first (so you always get the newest news), cache as offline fallback.
const CACHE = 'nieuws-b1-v2';
const SHELL = ['./', 'index.html', 'manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  e.respondWith(
    fetch(req)
      .then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req).then(r => {
        if (!r) return caches.match('./');
        // tell the app this is an old saved copy, not fresh from the internet
        const h = new Headers(r.headers);
        h.set('x-from-cache', '1');
        return r.blob().then(b => new Response(b, { status: r.status, statusText: r.statusText, headers: h }));
      }))
  );
});
