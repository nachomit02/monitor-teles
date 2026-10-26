# Monitor de teles

Revisa cada 30 minutos si hay stock de las teles de `config.json` y avisa por Telegram
cuando un producto vuelve a tener stock o baja de precio.

## Puesta en marcha (una sola vez)

Abrí PowerShell en esta carpeta (`C:\Users\MititieriIgnacio\monitor_teles`) y corré:

1. `.\.venv\Scripts\python.exe monitor.py --telegram`  → crea el bot y te manda un mensaje de prueba
2. `.\.venv\Scripts\python.exe monitor.py --mercadolibre APP_ID` → conecta la API oficial de Mercado Libre
   (app creada en developers.mercadolibre.com.ar; el acceso se renueva solo)
3. `.\.venv\Scripts\python.exe monitor.py --dry-run`   → revisa todo y muestra el resultado, sin mandar mensajes
4. `powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1` → lo deja corriendo solo cada 30 min

## Uso diario

- No hace falta hacer nada: si la PC está prendida, revisa solo.
- `monitor.log` tiene el historial de cada revisión.
- Para agregar una tienda, pegá el link del producto en `paginas` del modelo en `config.json`.
- `precio_max` es tu tope: el aviso dice si el precio está por debajo o por arriba.
- Para desinstalar: `Unregister-ScheduledTask -TaskName "MonitorTeles" -Confirm:$false`

## Estados

- `disponible`: se puede comprar (te avisa)
- `sin_stock`: agotado
- `no_existe`: el link ya no lleva al producto (conviene reemplazarlo)
- `bloqueado`: Mercado Libre pide iniciar sesión → volvé a correr `--login`
- `desconocido`: la página cambió y no se pudo leer
