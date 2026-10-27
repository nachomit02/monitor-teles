// Recibe los avisos del monitor aunque la app esté cerrada.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let aviso = {titulo: "Monitor de teles", cuerpo: "Hay novedades.", url: "./"};
  try { aviso = {...aviso, ...e.data.json()}; } catch (_) {}
  e.waitUntil(self.registration.showNotification(aviso.titulo, {
    body: aviso.cuerpo,
    icon: "img/icon-192.png",
    badge: "img/favicon-32.png",
    tag: aviso.tag,          // el mismo producto reemplaza su aviso anterior en vez de apilarse
    renotify: !!aviso.tag,
    data: {url: aviso.url},
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "./", self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(ventanas => {
    const abierta = ventanas.find(v => v.url === url);
    return abierta ? abierta.focus() : self.clients.openWindow(url);
  }));
});
