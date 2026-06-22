/* Service worker do PWA da audiência (tela /v2).
   Objetivo: tornar a tela instalável (critério do Android/Chrome) e abrir
   rápido. Estratégia: NETWORK-FIRST (sempre busca o mais novo quando online;
   cai pro cache só se a rede falhar). O WebSocket NÃO passa por aqui — SW só
   intercepta GET HTTP(S), então a tradução ao vivo não é afetada. */
const CACHE = "tradutor-v1";
const SHELL = ["/v2", "/icon-192.png", "/icon-512.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Não cacheia chamadas de API (audiência, etc) — sempre rede.
  if (url.pathname.startsWith("/api/")) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.status === 200 && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then((r) => r || caches.match("/v2")))
  );
});
