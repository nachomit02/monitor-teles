"""Publica el monitor en GitHub Actions y mantiene sus secretos.

Uso (en tu PC):
  python nube.py publicar     crea el repo, sube los archivos, carga los secretos y lo corre una vez
  python nube.py actualizar   vuelve a subir los archivos (después de cambiar config.json o el código)
  python nube.py token        reemplaza el token de GitHub cuando está por vencer

Uso interno (lo corre GitHub Actions):
  python nube.py guardar-refresh   guarda el refresh token nuevo de Mercado Libre como secreto
  python nube.py aviso-corrida     avisa por Telegram si la nube empezó a fallar o si el token vence pronto
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from nacl import encoding, public

BASE = Path(__file__).resolve().parent
SECRETS = BASE / "secrets.json"
SALUD = BASE / "salud.json"
REPO_NOMBRE = "monitor-teles"
ARCHIVOS = ["monitor.py", "nube.py", "config.json", "requirements.txt", "LEEME.md", "index.html",
            "logo_monitor.png", ".github/workflows/monitor.yml",
            "img/lg-oled-b5.webp", "img/tcl-c6k.webp", "img/tcl-c755.webp", "manifest.webmanifest",
            "img/icon-192.png", "img/icon-512.png", "img/icon-maskable-512.png", "img/apple-touch-icon.png",
            "img/favicon-32.png"]
DIAS_AVISO_TOKEN = 14


def gh(metodo, ruta, token, datos=None):
    req = urllib.request.Request(
        f"https://api.github.com{ruta}", method=metodo,
        data=json.dumps(datos).encode() if datos is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            cuerpo = r.read().decode()
            return json.loads(cuerpo) if cuerpo else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and metodo == "GET":
            return None
        raise SystemExit(f"GitHub respondió {e.code} en {metodo} {ruta}: {e.read().decode()[:300]}")


def poner_secreto(token, repo, nombre, valor):
    clave = gh("GET", f"/repos/{repo}/actions/secrets/public-key", token)
    caja = public.SealedBox(public.PublicKey(clave["key"].encode(), encoding.Base64Encoder()))
    cifrado = base64.b64encode(caja.encrypt(str(valor).encode())).decode()
    gh("PUT", f"/repos/{repo}/actions/secrets/{nombre}", token,
       {"encrypted_value": cifrado, "key_id": clave["key_id"]})


def subir_archivos(token, repo):
    for ruta in ARCHIVOS:
        contenido = base64.b64encode((BASE / ruta).read_bytes()).decode()
        actual = gh("GET", f"/repos/{repo}/contents/{ruta}", token)
        datos = {"message": f"actualizar {ruta}", "content": contenido}
        if actual:
            datos["sha"] = actual["sha"]
        gh("PUT", f"/repos/{repo}/contents/{ruta}", token, datos)
        print("  subido:", ruta)


def leer_secretos():
    return json.loads(SECRETS.read_text(encoding="utf-8"))


def pedir_token():
    sec = leer_secretos()
    if sec.get("gh_pat"):
        return sec["gh_pat"]
    pat = input("Pegá el token de GitHub (empieza con ghp_) y Enter: ").strip()
    usuario = gh("GET", "/user", pat)
    if not usuario:
        raise SystemExit("Ese token no funciona.")
    sec["gh_pat"] = pat
    SECRETS.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")
    return pat


def publicar():
    token = pedir_token()
    usuario = gh("GET", "/user", token)["login"]
    repo = f"{usuario}/{REPO_NOMBRE}"
    if not gh("GET", f"/repos/{repo}", token):
        # público: GitHub Actions es gratis e ilimitado; los secretos quedan cifrados y no se ven
        gh("POST", "/user/repos", token, {"name": REPO_NOMBRE, "private": False, "auto_init": True,
                                          "description": "Monitor de stock de televisores"})
        print("Repo creado:", repo)
    subir_archivos(token, repo)

    sec = leer_secretos()
    for nombre, valor in {"TELEGRAM_TOKEN": sec["token"], "TELEGRAM_CHAT_ID": sec["chat_id"],
                          "ML_CLIENT_ID": sec["ml_client_id"], "ML_SECRET": sec["ml_secret"],
                          "ML_REFRESH": sec["ml_refresh"], "GH_PAT": token}.items():
        poner_secreto(token, repo, nombre, valor)
        print("  secreto cargado:", nombre)

    # Mercado Libre invalida el refresh token al usarlo: si la PC y la nube lo usan a la vez, uno se rompe.
    subprocess.run(["schtasks", "/Change", "/TN", "MonitorTeles", "/DISABLE"], capture_output=True)
    print("Tarea de la PC desactivada (ahora corre en la nube).")

    gh("POST", f"/repos/{repo}/actions/workflows/monitor.yml/dispatches", token, {"ref": "main"})
    print(f"\nListo. Primera corrida lanzada: https://github.com/{repo}/actions")


def actualizar():
    token = pedir_token()
    usuario = gh('GET', '/user', token)['login']
    repo = f"{usuario}/{REPO_NOMBRE}"
    subir_archivos(token, repo)
    print("Archivos actualizados.")
    if not gh("GET", f"/repos/{repo}/pages", token):
        # página pública con el gráfico de precios (index.html)
        gh("POST", f"/repos/{repo}/pages", token, {"source": {"branch": "main", "path": "/"}})
        print(f"Gráfico activado: https://{usuario}.github.io/{REPO_NOMBRE}/ (tarda unos minutos la primera vez)")


def token_nuevo():
    sec = leer_secretos()
    sec.pop("gh_pat", None)
    SECRETS.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")
    token = pedir_token()
    repo = f"{gh('GET', '/user', token)['login']}/{REPO_NOMBRE}"
    poner_secreto(token, repo, "GH_PAT", token)
    print("Token de GitHub reemplazado en tu PC y en la nube.")


def telegram(texto):
    sec = leer_secretos()
    datos = urllib.parse.urlencode({"chat_id": sec["chat_id"], "text": texto, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{sec['token']}/sendMessage", data=datos, timeout=20)


def aviso_corrida():
    repo, pat, resultado = os.environ["REPO"], os.environ["GH_PAT"], os.environ["RESULTADO"]
    # solo avisa en el cambio (anduvo -> falló, o falló -> anduvo), no en cada corrida
    runs = gh("GET", f"/repos/{repo}/actions/workflows/monitor.yml/runs?status=completed&per_page=1",
              pat)["workflow_runs"]
    anterior = runs[0]["conclusion"] if runs else "success"
    if resultado == "failure" and anterior != "failure":
        telegram(f"🔧 El monitor falló en la nube. Detalle: "
                 f"https://github.com/{repo}/actions/runs/{os.environ['RUN_ID']}")
    elif resultado == "success" and anterior == "failure":
        telegram("✅ El monitor volvió a andar en la nube.")

    # si el token de GitHub vence, deja de guardarse el permiso de Mercado Libre y todo se cae
    req = urllib.request.Request("https://api.github.com/user", headers={"Authorization": f"Bearer {pat}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        vence = r.headers.get("github-authentication-token-expiration")
    if not vence:
        return
    fecha = datetime.strptime(vence[:19], "%Y-%m-%d %H:%M:%S")
    dias = (fecha - datetime.now(timezone.utc).replace(tzinfo=None)).days
    salud = json.loads(SALUD.read_text(encoding="utf-8")) if SALUD.exists() else {}
    hoy = f"{datetime.now(timezone.utc):%Y-%m-%d}"
    if dias <= DIAS_AVISO_TOKEN and salud.get("_aviso_token") != hoy:
        telegram(f"🔑 El token de GitHub del monitor vence en {dias} días ({fecha:%d/%m}).\n"
                 "1. Creá uno nuevo en github.com/settings/tokens (classic, permisos repo + workflow).\n"
                 "2. En tu PC corré: python nube.py token")
        salud["_aviso_token"] = hoy
        SALUD.write_text(json.dumps(salud, ensure_ascii=False, indent=2), encoding="utf-8")


def guardar_refresh():
    sec = leer_secretos()
    nuevo = sec.get("ml_refresh")
    if nuevo and nuevo != os.environ.get("ML_REFRESH_ANTERIOR"):
        poner_secreto(os.environ["GH_PAT"], os.environ["REPO"], "ML_REFRESH", nuevo)
        print("Refresh token de Mercado Libre actualizado.")
    else:
        print("Sin cambios en el refresh token.")


if __name__ == "__main__":
    {"publicar": publicar, "actualizar": actualizar, "token": token_nuevo, "guardar-refresh": guardar_refresh,
     "aviso-corrida": aviso_corrida}[sys.argv[1]]()
