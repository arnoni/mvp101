self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open('dilldrill-v1').then((cache) => {
            return cache.addAll([
                '/',
                '/static/graphics/DillDrill_banner_selected.svg',
                '/offline.html',
            ]);
        })
    );
});

self.addEventListener('fetch', (event) => {
    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request).catch(() => {
                return caches.match('/offline.html');
            });
        })
    );
});
