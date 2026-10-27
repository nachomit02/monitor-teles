# Monitor de teles

Corre en GitHub Actions (no necesita la PC prendida): revisa cada hora si hay stock de las teles de
`config.json` y avisa por Telegram cuando un producto vuelve a tener stock o baja de precio.
Del 2 al 4 de noviembre (Cyber Monday) revisa cada 15 minutos.

- Corridas y botón "Run workflow": https://github.com/nachomit02/monitor-teles/actions
- Gráfico de precios: https://nachomit02.github.io/monitor-teles/

## Qué te llega por Telegram

- 🟢 HAY STOCK / 📉 BAJÓ DE PRECIO, con:
  - 🏆 si es el precio más bajo registrado en 30 días, o 📊 cuál fue el mínimo y dónde
  - ⚠️ si esa tienda lo tuvo más barato hace más de una semana (oferta inflada)
- 🔧 si una tienda falla 3 veces seguidas (y ✅ cuando vuelve a andar)
- 🔧 si la corrida en la nube falla (una vez, no cada hora)
- 🔑 si el token de GitHub vence en menos de 14 días

## Comandos en tu PC

En PowerShell, en esta carpeta (`C:\Users\MititieriIgnacio\monitor_teles`):

- `.\.venv\Scripts\python.exe nube.py actualizar` → sube los cambios de `config.json` o del código
- `.\.venv\Scripts\python.exe nube.py token` → reemplaza el token de GitHub cuando está por vencer
- `.\.venv\Scripts\python.exe nube.py activar-push` → (una sola vez) crea las claves de los avisos en el celular.
  Después, en la app: "Activar avisos en este celular" → mandale el código al bot de Telegram.
  En iPhone, los avisos solo funcionan con la app agregada a inicio.
- `.\.venv\Scripts\python.exe monitor.py --mercadolibre APP_ID` y después `nube.py publicar`
  → reconecta Mercado Libre si se pierde el permiso

No corras `monitor.py` en la PC sin `--dry-run`: Mercado Libre invalida el permiso al usarlo y se
rompería el de la nube.

## Archivos que guarda la nube

- `state.json`: último estado de cada página (para no repetir avisos)
- `historial.csv`: precios con stock de cada corrida (alimenta el gráfico y el detector de ofertas)
- `salud.json`: fallas seguidas por tienda

## Estados

- `disponible`: se puede comprar (te avisa)
- `sin_stock`: agotado
- `no_publicado`: el buscador de la tienda no tiene el modelo
- `no_existe`: el link ya no lleva al producto (conviene reemplazarlo)
- `bloqueado`: la tienda rechaza el acceso (Frávega desde servidores fuera de Argentina)
- `desconocido`: la página cambió y no se pudo leer
