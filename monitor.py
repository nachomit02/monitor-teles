"""Monitor de stock y precio de televisores en tiendas argentinas.

Uso:
  python monitor.py                 revisa todo y avisa por Telegram si algo cambió
  python monitor.py --dry-run       revisa y muestra resultados, sin mandar mensajes
  python monitor.py --resumen       revisa, avisa y además manda el estado de todas las tiendas
  python monitor.py --telegram [TOKEN]  configura el bot de Telegram (token + chat id)
  python monitor.py --test          manda un mensaje de prueba a Telegram
  python monitor.py --mercadolibre APP_ID   conecta la API oficial de Mercado Libre
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
SECRETS = BASE / "secrets.json"
STATE = BASE / "state.json"
LOG = BASE / "monitor.log"
PROFILE = BASE / "perfil_navegador"

SIN_STOCK = ["sin stock", "agotado", "no disponible", "no está disponible", "no se encuentra disponible",
             "no hay stock", "publicación pausada", "te avisaremos cuando", "avisame cuando"]
CON_STOCK = ["agregar al carrito", "añadir al carrito", "comprar ahora", "agregar a la bolsa"]
# Bloque del producto: sube desde el título (h1) hasta el contenedor más chico que tenga
# un aviso de "sin stock", o un precio junto a una señal de stock. Así se evitan banners
# y productos relacionados.
JS_BLOQUE = r"""([sin, con]) => {
  const h1 = [...document.querySelectorAll('h1')].find(e => e.offsetParent !== null && e.innerText.trim());
  if (!h1) return {titulo: '', bloque: '', meta: null};
  const meta = document.querySelector('meta[property="product:price:amount"], [itemprop="price"]');
  let el = h1, bloque = '';
  for (let i = 0; i < 15 && el.parentElement; i++) {
    el = el.parentElement;
    const t = el.innerText.replace(/\s+/g, ' ');
    if (t.length > 6000) break;
    const tl = t.toLowerCase();
    const hayPrecio = /\$\s?\d/.test(t);
    if (sin.some(k => tl.includes(k)) || (hayPrecio && con.some(k => tl.includes(k)))) { bloque = t; break; }
  }
  return {titulo: h1.innerText.replace(/\s+/g, ' ').trim(), bloque,
          meta: meta ? (meta.getAttribute('content') || meta.innerText) : null};
}"""
PRECIO_RE = re.compile(r"\$\s?(\d{1,3}(?:\.\d{3})+)(?:,\d{2})?")
# precios que no son el precio de venta: sin impuestos, cuotas, garantías extendidas
NO_PRECIO_RE = re.compile(
    r"(sin impuestos[^$]{0,20}|s/imp[^$]{0,20}|cuotas?[^$]{0,30}de\s|\bde\s|meses\s-\s|ahorr[áa]s?\s)\$\s?[\d.]+(?:,\d{2})?",
    re.I)


def log(msg):
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(linea)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(linea + "\n")


def leer_json(path, defecto):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return defecto


def guardar_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def tienda_de(url):
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    return host.split(".")[0].capitalize()


def normalizar(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------- Telegram

def telegram_api(token, metodo, params=None):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.loads(r.read().decode())


def enviar_telegram(texto):
    sec = leer_json(SECRETS, {})
    if not sec.get("token") or not sec.get("chat_id"):
        log("Telegram no configurado: corré 'python monitor.py --telegram'")
        return
    telegram_api(sec["token"], "sendMessage", {
        "chat_id": sec["chat_id"], "text": texto, "parse_mode": "HTML",
        "disable_web_page_preview": "true"})


def configurar_telegram(token=None):
    if not token:
        print("1. En Telegram, abrí @BotFather, mandá /newbot y seguí los pasos.")
        print("2. Copiá el token que te da (algo como 123456:ABC-...).")
        token = input("Pegá el token acá: ").strip()
        print("3. Ahora abrí tu bot nuevo en Telegram y mandale cualquier mensaje (ej: hola).")
        input("   Cuando lo hayas mandado, apretá Enter...")
    updates = telegram_api(token, "getUpdates").get("result", [])
    chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
    if not chats:
        print("No encontré mensajes. Mandale un mensaje al bot y volvé a correr --telegram.")
        return
    guardar_json(SECRETS, {"token": token, "chat_id": chats[-1]})
    enviar_telegram("✅ Monitor de teles conectado. Te aviso acá cuando haya stock.")
    print("Listo. Te llegó un mensaje de prueba a Telegram.")


# ---------------------------------------------------------------- Scraping

def extraer_precio(texto, precio_max):
    """Toma el menor precio 'de venta' cerca del título; descarta cuotas, sin impuestos, etc."""
    limpio = NO_PRECIO_RE.sub(" ", texto)
    precios = [int(m.group(1).replace(".", "")) for m in PRECIO_RE.finditer(limpio)]
    # descarta accesorios/garantías: un televisor no vale menos del 35% del tope
    precios = [p for p in precios if p >= precio_max * 0.35]
    return min(precios) if precios else None


def revisar_pagina(page, url, producto):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    if "account-verification" in page.url or "/login" in page.url:
        return {"estado": "bloqueado", "precio": None}

    r = page.evaluate(JS_BLOQUE, [SIN_STOCK, CON_STOCK])
    titulo, bloque = r["titulo"], r["bloque"].lower()
    # si la tienda redirige a la home o a otro producto, el título no tiene el código del modelo
    if normalizar(producto["codigo"]) not in normalizar(titulo + page.title()):
        log(f"  no_existe: terminó en {page.url[:120]} | título '{page.title()[:80]}' | h1 '{titulo[:80]}'")
        return {"estado": "no_existe", "precio": None, "titulo": titulo[:120]}

    # "no disponible" manda sobre el botón de compra: varias tiendas lo muestran igual
    if any(k in bloque for k in SIN_STOCK):
        estado = "sin_stock"
    elif any(k in bloque for k in CON_STOCK):
        estado = "disponible"
    else:
        estado = "desconocido"
    precio = None
    if estado == "disponible":
        meta = re.sub(r"[^\d.]", "", r["meta"] or "")
        precio = int(float(meta)) if meta else extraer_precio(r["bloque"], producto["precio_max"])
    return {"estado": estado, "precio": precio, "titulo": titulo[:120]}


def links_de_busqueda(page, url, codigo):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    links = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [e.href, e.innerText || ''])")
    cod = normalizar(codigo)
    encontrados = []
    for href, texto in links:
        es_producto = "/p/" in href or href.endswith("/p") or "/producto/" in href or href.endswith(".html")
        if (cod in normalizar(href) or cod in normalizar(texto)) and es_producto and "catalogsearch" not in href:
            href = href.split("?")[0]
            if href not in encontrados:
                encontrados.append(href)
    if not encontrados:
        log(f"  buscador sin coincidencias: {len(links)} links, título '{page.title()[:80]}'")
    return encontrados[:5]


def buscar_vtex(tienda, codigo):
    """Tiendas hechas con VTEX (ej: Cetrogar) tienen un catálogo público en JSON: más confiable que leer la página."""
    url = f"{tienda.rstrip('/')}/api/catalog_system/pub/products/search?ft={urllib.parse.quote(codigo)}"
    req = urllib.request.Request(url, headers={"User-Agent": "monitor-teles"})
    with urllib.request.urlopen(req, timeout=30) as r:
        productos = json.loads(r.read().decode())
    resultados = []
    for prod in productos:
        if normalizar(codigo) not in normalizar(prod.get("productName", "")):
            continue
        ofertas = [s["commertialOffer"] for it in prod.get("items", []) for s in it.get("sellers", [])]
        con_stock = [o for o in ofertas if o.get("AvailableQuantity", 0) > 0 and o.get("Price")]
        resultados.append({
            "url": prod["link"], "titulo": prod["productName"][:120],
            "estado": "disponible" if con_stock else "sin_stock",
            "precio": int(min(o["Price"] for o in con_stock)) if con_stock else None})
    return resultados


# ---------------------------------------------------------------- Mercado Libre (API oficial)

ML_API = "https://api.mercadolibre.com"
ML_REDIRECT = "https://www.google.com"


def ml_token_pedir(datos):
    req = urllib.request.Request(f"{ML_API}/oauth/token", data=urllib.parse.urlencode(datos).encode(),
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Mercado Libre rechazó el pedido (HTTP {e.code}): {e.read().decode()[:300]}")
    sec = leer_json(SECRETS, {})
    sec.update({"ml_access": tok["access_token"], "ml_refresh": tok.get("refresh_token", sec.get("ml_refresh")),
                "ml_expira": datetime.now().timestamp() + tok.get("expires_in", 21600) - 300})
    guardar_json(SECRETS, sec)
    return sec["ml_access"]


def ml_token():
    sec = leer_json(SECRETS, {})
    if not sec.get("ml_refresh"):
        return None
    if sec.get("ml_access") and datetime.now().timestamp() < sec.get("ml_expira", 0):
        return sec["ml_access"]
    return ml_token_pedir({"grant_type": "refresh_token", "client_id": sec["ml_client_id"],
                           "client_secret": sec["ml_secret"], "refresh_token": sec["ml_refresh"]})


def ml_get(ruta):
    req = urllib.request.Request(f"{ML_API}{ruta}", headers={"Authorization": f"Bearer {ml_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def clave_valida(client_id, secreto):
    datos = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id,
                                    "client_secret": secreto}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"{ML_API}/oauth/token", data=datos), timeout=30)
        return True
    except urllib.error.HTTPError:
        return False


def configurar_mercadolibre(client_id):
    while True:
        secreto = input("Pegá la Clave secreta de la app (clic derecho para pegar, UNA sola vez) y Enter: ").strip()
        if clave_valida(client_id, secreto):
            print("Clave correcta.")
            break
        print(f"Mercado Libre dice que la clave no es válida (tiene {len(secreto)} caracteres). Probá de nuevo.\n")
    sec = leer_json(SECRETS, {})
    sec.update({"ml_client_id": client_id, "ml_secret": secreto})
    guardar_json(SECRETS, sec)
    url = (f"https://auth.mercadolibre.com.ar/authorization?response_type=code&client_id={client_id}"
           f"&redirect_uri={urllib.parse.quote(ML_REDIRECT)}")
    print("\n1. Abrí este link, iniciá sesión y tocá 'Permitir':\n   " + url)
    print("2. Te va a llevar a Google. Copiá la dirección completa de la barra (empieza con https://www.google.com/?code=...)")
    dato = input("Pegala acá y Enter: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(dato).query).get("code", [dato])[0]
    ml_token_pedir({"grant_type": "authorization_code", "client_id": client_id, "client_secret": secreto,
                    "code": code, "redirect_uri": ML_REDIRECT})
    print("Listo: Mercado Libre conectado.")


def buscar_mercadolibre(producto):
    """Productos de catálogo que coinciden con el código; estado y mejor precio de sus publicaciones."""
    codigo = producto["codigo"]
    ids = list(producto.get("ml_productos", []))
    try:
        res = ml_get(f"/products/search?status=active&site_id=MLA&q={urllib.parse.quote(codigo)}")
        ids += [r["id"] for r in res.get("results", [])
                if normalizar(codigo) in normalizar(r.get("name", "")) and r["id"] not in ids]
    except urllib.error.HTTPError as e:
        log(f"Mercado Libre búsqueda {codigo}: HTTP {e.code}")
    resultados = []
    for pid in ids[:5]:
        url = f"https://www.mercadolibre.com.ar/p/{pid}"
        try:
            ofertas = ml_get(f"/products/{pid}/items").get("results", [])
        except urllib.error.HTTPError as e:
            # 404 = el producto de catálogo no tiene publicaciones activas
            ofertas = [] if e.code == 404 else None
            if ofertas is None:
                log(f"Mercado Libre {pid}: HTTP {e.code}")
                continue
        precios = [int(o["price"]) for o in ofertas if o.get("price")]
        resultados.append({"url": url, "titulo": pid, "estado": "disponible" if precios else "sin_stock",
                           "precio": min(precios) if precios else None})
    return resultados


def abrir_navegador(p, headless=True):
    return p.chromium.launch_persistent_context(
        str(PROFILE), headless=headless, locale="es-AR",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"),
        viewport={"width": 1366, "height": 900})


def login_mercadolibre():
    with sync_playwright() as p:
        ctx = abrir_navegador(p, headless=False)
        page = ctx.new_page()
        page.goto("https://www.mercadolibre.com.ar/navigation/login")
        print("Iniciá sesión en Mercado Libre en la ventana que se abrió y después cerrala.")
        ctx.wait_for_event("close", timeout=15 * 60 * 1000)
    print("Sesión guardada en el perfil del monitor.")


# ---------------------------------------------------------------- Principal

def fmt(precio):
    return f"${precio:,.0f}".replace(",", ".") if precio else "precio no detectado"


ICONOS = {"disponible": "🟢", "sin_stock": "🔴", "no_publicado": "⚪", "no_existe": "⚫", "bloqueado": "🚫",
          "desconocido": "❔"}


def resumen(estado):
    """Mensaje con el estado de todas las páginas revisadas, agrupado por producto."""
    lineas = [f"📋 <b>Resumen {datetime.now():%d/%m %H:%M}</b>"]
    por_producto = {}
    for url, v in estado.items():
        por_producto.setdefault(v.get("producto", "?"), []).append((url, v))
    for prod, filas in por_producto.items():
        lineas.append(f"\n<b>{prod}</b>")
        for url, v in filas:
            precio = f" {fmt(v['precio'])}" if v.get("precio") else ""
            lineas.append(f"{ICONOS.get(v['estado'], '❔')} {tienda_de(url)}: {v['estado'].replace('_', ' ')}{precio}")
    return "\n".join(lineas)


def main(dry_run=False, con_resumen=False):
    config = leer_json(CONFIG, {})
    estado_previo = leer_json(STATE, {})
    estado_nuevo = dict(estado_previo)
    avisos = []
    revisadas = []

    with sync_playwright() as p:
        ctx = abrir_navegador(p)
        page = ctx.new_page()
        for prod in config["productos"]:
            urls = list(prod.get("paginas", []))
            for busq in prod.get("busquedas", []):
                try:
                    encontrados = links_de_busqueda(page, busq, prod["codigo"])
                except Exception as e:
                    log(f"ERROR búsqueda {busq}: {e}")
                    continue
                urls += [u for u in encontrados if u not in urls]
                if not encontrados:
                    # el buscador no tiene el modelo: lo mostramos en el resumen como "no publicado"
                    log(f"{prod['nombre']} | {tienda_de(busq)} (buscador) | no_publicado")
                    estado_nuevo[busq] = {"estado": "no_publicado", "precio": None, "producto": prod["nombre"],
                                          "visto": f"{datetime.now():%Y-%m-%d %H:%M}"}
                    revisadas.append(busq)

            # tiendas VTEX: ya traen estado y precio, no hace falta abrir la página
            ya_revisadas = {}
            for tienda in prod.get("vtex", []):
                try:
                    encontrados = buscar_vtex(tienda, prod["codigo"])
                except Exception as e:
                    log(f"ERROR catálogo {tienda}: {e}")
                    continue
                if not encontrados:
                    log(f"{prod['nombre']} | {tienda_de(tienda)} (catálogo) | no_publicado")
                    estado_nuevo[tienda + "#" + prod["codigo"]] = {
                        "estado": "no_publicado", "precio": None, "producto": prod["nombre"],
                        "visto": f"{datetime.now():%Y-%m-%d %H:%M}"}
                    revisadas.append(tienda + "#" + prod["codigo"])
                for res in encontrados:
                    ya_revisadas[res["url"]] = res
                    if res["url"] not in urls:
                        urls.append(res["url"])

            if ml_token():
                try:
                    for res in buscar_mercadolibre(prod):
                        ya_revisadas[res["url"]] = res
                        if res["url"] not in urls:
                            urls.append(res["url"])
                except Exception as e:
                    log(f"ERROR Mercado Libre: {str(e)[:150]}")

            for url in urls:
                try:
                    r = ya_revisadas.get(url) or revisar_pagina(page, url, prod)
                except Exception as e:
                    log(f"ERROR {url}: {str(e)[:150]}")
                    continue
                tienda = tienda_de(url)
                log(f"{prod['nombre']} | {tienda} | {r['estado']} | {fmt(r['precio'])}")
                revisadas.append(url)
                previo = estado_previo.get(url, {})
                estado_nuevo[url] = {**r, "producto": prod["nombre"], "visto": f"{datetime.now():%Y-%m-%d %H:%M}"}

                if r["estado"] != "disponible":
                    continue
                barato = r["precio"] is not None and r["precio"] <= prod["precio_max"]
                if not barato and not config.get("avisar_aunque_supere_precio_max", True):
                    continue
                volvio_stock = previo.get("estado") != "disponible"
                bajo_precio = (r["precio"] and previo.get("precio") and r["precio"] < previo["precio"])
                if volvio_stock or bajo_precio:
                    motivo = "🟢 HAY STOCK" if volvio_stock else "📉 BAJÓ DE PRECIO"
                    if r["precio"] is None:
                        marca = "💲 revisá el precio en la página"
                    elif barato:
                        marca = "✅ debajo de tu tope"
                    else:
                        marca = f"⚠️ arriba de tu tope ({fmt(prod['precio_max'])})"
                    avisos.append(f"{motivo}: <b>{prod['nombre']}</b>\n{tienda}: {fmt(r['precio'])} {marca}\n{url}")
        ctx.close()

    urls_config = {u for prod in config["productos"] for u in prod.get("paginas", [])}
    # limpia páginas que ya no están en config.json (ej: Mercado Libre)
    estado_nuevo = {u: v for u, v in estado_nuevo.items()
                    if u in urls_config or v.get("estado") != "bloqueado"}
    bloqueados = [u for u, v in estado_nuevo.items() if v.get("estado") == "bloqueado" and u in urls_config]
    if bloqueados:
        log(f"{len(bloqueados)} páginas de Mercado Libre bloqueadas: corré 'python monitor.py --login'")

    if dry_run:
        print("\n--- Avisos que se mandarían ---\n" + ("\n\n".join(avisos) or "(ninguno)"))
        return
    if avisos and not leer_json(SECRETS, {}).get("chat_id"):
        # sin Telegram no guardamos el estado, para no perder el aviso
        log("Hay avisos pero Telegram no está configurado: corré 'python monitor.py --telegram'")
        return
    for a in avisos:
        enviar_telegram(a)
    if con_resumen:
        enviar_telegram(resumen({u: v for u, v in estado_nuevo.items() if u in revisadas}))
    guardar_json(STATE, estado_nuevo)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--telegram" in args:
        resto = args[args.index("--telegram") + 1:]
        configurar_telegram(resto[0] if resto else None)
    elif "--test" in args:
        enviar_telegram("🔔 Prueba del monitor de teles: funciona.")
    elif "--login" in args:
        login_mercadolibre()
    elif "--mercadolibre" in args:
        configurar_mercadolibre(args[args.index("--mercadolibre") + 1])
    else:
        main(dry_run="--dry-run" in args, con_resumen="--resumen" in args)
