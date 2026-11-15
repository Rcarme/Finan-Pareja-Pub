/**
 * Service Worker para Finanzas en Común (F09).
 * 
 * Reglas arquitectónicas del proyecto:
 * 1. Solo App Shell y assets estáticos: NUNCA cachear datos contables, saldos ni información sensible.
 * 2. RN-39: NO existe modo offline para escritura. Las peticiones mutantes (POST, PUT, DELETE)
 *    van directo a la red (network-only) y nunca se encolan offline.
 * 3. En caso de navegación sin conexión, sirve la página de contingencia /offline.
 */

const CACHE_NAME = 'finanzas-shell-v2';
const PRECACHE_ASSETS = [
    '/offline',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/icons/apple-touch-icon.png',
    '/static/icons/favicon.png',
    '/static/js/chart.umd.min.js'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(PRECACHE_ASSETS);
        }).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((name) => {
                    if (name !== CACHE_NAME) {
                        return caches.delete(name);
                    }
                })
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);

    // RN-39: Toda petición que no sea GET (POST, PUT, DELETE, etc.) es NETWORK-ONLY.
    // Jamás se intercepta ni se cachea para garantizar que no haya mutaciones offline ni duplicados.
    if (request.method !== 'GET') {
        return;
    }

    // 1. Navegaciones HTML: Network-First con fallback a /offline
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request).catch(async () => {
                const cache = await caches.open(CACHE_NAME);
                const offlineResponse = await cache.match('/offline');
                return offlineResponse || new Response(
                    '<h1>Sin conexión (RN-39)</h1><p>No se pueden guardar datos sin conexión.</p>',
                    { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
                );
            })
        );
        return;
    }

    // Assets propios: red primero, caché únicamente como respaldo sin conexión.
    if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
        event.respondWith((async () => {
            const cache = await caches.open(CACHE_NAME);
            try {
                const response = await fetch(request, { cache: 'no-cache' });
                if (response.status === 200) await cache.put(request, response.clone());
                return response;
            } catch (error) {
                const cached = await cache.match(request);
                if (cached) return cached;
                throw error;
            }
        })());
        return;
    }

    // 3. El resto de las peticiones GET van a la red directamente
    event.respondWith(fetch(request));
});
