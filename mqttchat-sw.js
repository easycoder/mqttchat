/*
 * MqttChat's service worker: what makes the page installable, and lets it open without a
 * connection.
 *
 * Plain JavaScript rather than AllSpeak on purpose — a worker has no DOM and no window, so the
 * browser runtime has nothing to run against. This is the same reasoning as any other plugin:
 * native code where a native API is the point (see learn/reference/13-plugins.md).
 *
 * The policy is deliberately thin. The three shell files are fetched from the network first and
 * cached only as a fallback, so a reload always picks up a new build while there is a
 * connection; the credentials endpoint is never cached, and neither is anything from another
 * origin (the AllSpeak bundle, MQTT.js) — nothing here has any business holding a secret.
 */

const SHELL = 'mqttchat-shell-v2';
const SHELL_FILES = ['mqttchat.html', 'mqttchat-main.allspeak', 'mqttchat.json'];

self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(SHELL)
            .then(function (cache) { return cache.addAll(SHELL_FILES); })
            .then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys()
            .then(function (names) {
                return Promise.all(names.map(function (name) {
                    return name === SHELL ? null : caches.delete(name);
                }));
            })
            .then(function () { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function (event) {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;          // the CDN bundle and MQTT.js
    if (url.pathname.indexOf('credentials.php') !== -1) return; // never hold the login

    const wanted = SHELL_FILES.some(function (file) {
        return url.pathname.endsWith(file);
    });
    if (!wanted) return;

    event.respondWith(
        fetch(request)
            .then(function (response) {
                const copy = response.clone();
                caches.open(SHELL).then(function (cache) { cache.put(request, copy); });
                return response;
            })
            .catch(function () {
                // Offline: the shell opens from the cache. Asking a question still needs the
                // broker, so the page will say the service is not answering, which is true.
                return caches.match(request, { ignoreSearch: true }).then(function (hit) {
                    return hit || Response.error();
                });
            })
    );
});
