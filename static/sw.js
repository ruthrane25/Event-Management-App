const CACHE_NAME = 'eventflow-v1';
const ASSETS_TO_CACHE = [
    '/',
    '/static/css/style.css',
    '/static/js/main.js',
    '/static/icon-192.png',
    '/static/icon-512.png'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE);
        })
    );
});

self.addEventListener('fetch', (event) => {
    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request);
        })
    );
});
