#!/usr/bin/env python3
"""Wrapper desktop do tradutor — splash + server (subprocess) + browser.

Comportamento:
  1. Sobe server.py como SUBPROCESS (nao thread — uvicorn+signal handlers
     so funcionam na main thread do processo dono)
  2. Mostra splash tkinter com polling em /health
  3. Quando /health responde, fecha splash + abre Chrome/Edge em modo --app
  4. Quando usuario fecha browser, mata server subprocess
  5. Quando user fecha splash sem terminar, tambem mata server

Pra rodar:
    pythonw tradutor_app.py     <- sem terminal preto (usado pelo atalho)
    python tradutor_app.py      <- com terminal pra ver logs
"""

import atexit
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from splash import Splash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tradutor.app")


# Porta default 8766 (8765 é da MillenniumConnect.exe do bancoHaoma).
# Sobrescrever via env var TRADUTOR_PORT=NNNN se quiser outra.
TRADUTOR_PORT = int(os.environ.get("TRADUTOR_PORT", "8766"))
SERVER_URL = f"http://127.0.0.1:{TRADUTOR_PORT}"
HEALTH_URL = SERVER_URL + "/health"
BOOT_TIMEOUT_S = 240


def _has_cuda():
    """Detecta se CUDA (GPU NVIDIA) está disponível.
    Se NÃO houver, o tradutor cai pra CPU automaticamente."""
    # Override explícito via env (operador forçar CPU mesmo com GPU disponível)
    forced = os.environ.get("TRADUTOR_DEVICE", "").strip().lower()
    if forced in ("cpu", "cuda"):
        return forced == "cuda"
    # Heurística 1: nvidia-smi existe e responde?
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, timeout=3, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        pass
    # Heurística 2: torch reconhece CUDA?
    try:
        import torch  # noqa: F401
        return torch.cuda.is_available()
    except Exception:
        return False


HAS_GPU = _has_cuda()

if HAS_GPU:
    # Modo GPU — qualidade máxima
    SERVER_ARGS = [
        "--host", "127.0.0.1",
        "--port", str(TRADUTOR_PORT),
        "--device", "cuda",
        "--model", "large-v3",
        "--compute-type", "float16",
        "--backend", "marian",
        "--partial-ms", "1000",
        "--silence-ms", "700",
    ]
    logger.info("Modo GPU detectado — Whisper large-v3 float16 + Marian.")
else:
    # Modo CPU-only — modelo menor + int8 + intervalos maiores pra reduzir carga
    SERVER_ARGS = [
        "--host", "127.0.0.1",
        "--port", str(TRADUTOR_PORT),
        "--device", "cpu",
        "--model", "small",          # 480MB, ~1-2s por frase em CPU moderno
        "--compute-type", "int8",     # quantização — mais rápido em CPU
        "--backend", "marian",        # Marian PT-ES é leve em CPU (~300MB)
        "--partial-ms", "1500",       # menos updates parciais (reduz CPU)
        "--silence-ms", "700",
    ]
    logger.info("Modo CPU detectado (sem GPU) — Whisper small int8 + Marian.")


# Estado global pra cleanup
_server_proc = None
_browser_proc = None


def _cleanup():
    """Mata server + browser ao sair (atexit)."""
    global _server_proc, _browser_proc
    for name, p in (("browser", _browser_proc), ("server", _server_proc)):
        if p is None:
            continue
        try:
            if p.poll() is None:
                logger.info("encerrando %s (pid %d)", name, p.pid)
                p.terminate()
                try:
                    p.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    p.kill()
        except Exception as exc:
            logger.warning("erro encerrando %s: %s", name, exc)


atexit.register(_cleanup)


def start_server():
    """Sobe server.py como subprocess."""
    global _server_proc
    server_script = SCRIPT_DIR / "server.py"
    if not server_script.exists():
        raise FileNotFoundError(server_script)

    # No Windows usar CREATE_NO_WINDOW pra nao abrir terminal preto separado
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW

    # Usa python.exe (nao pythonw.exe) — uvicorn precisa de stderr/stdout
    py_exe = sys.executable
    if sys.platform == "win32" and py_exe.lower().endswith("pythonw.exe"):
        py_exe = py_exe[:-len("pythonw.exe")] + "python.exe"

    cmd = [py_exe, str(server_script)] + SERVER_ARGS
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Loga stdout/stderr num arquivo pra debug
    log_path = SCRIPT_DIR.parent / "data" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "ab", buffering=0)

    logger.info("Iniciando server: %s", " ".join(cmd))
    logger.info("Log do server em: %s", log_path)

    _server_proc = subprocess.Popen(
        cmd,
        cwd=str(SCRIPT_DIR),
        env=env,
        stdout=log_fp,
        stderr=log_fp,
        creationflags=creationflags,
    )
    return _server_proc


def find_browser():
    """Acha Edge ou Chrome no Windows."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def launch_browser(browser_path, url):
    """Sobe browser em modo --app. Localhost ja eh secure context por default —
    nao precisa da flag --unsafely-treat-insecure-origin-as-secure (causa aviso
    amarelo visivel no app)."""
    global _browser_proc
    user_data = tempfile.mkdtemp(prefix="tradutor-profile-")
    cmd = [
        browser_path,
        f"--app={url}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-features=TranslateUI",
        "--window-size=1280,800",
    ]
    logger.info("Lancando browser: %s", browser_path)
    _browser_proc = subprocess.Popen(cmd)
    return _browser_proc


def is_server_alive():
    """Detecta se ja existe servidor em :8765 — se sim, reusa em vez de subir outro."""
    from urllib.request import urlopen
    from urllib.error import URLError
    try:
        with urlopen(HEALTH_URL, timeout=1) as r:
            return r.status == 200
    except (URLError, Exception):
        return False


def main():
    global _server_proc

    # Atalho rápido: se servidor já tá rodando (ex: ficou de outro start ou roda
    # como serviço), pula splash+subprocess e abre browser DIRETO em ~1s.
    if is_server_alive():
        logger.info("Server ja roda em %s — reusando (sem splash).", SERVER_URL)
        browser = find_browser()
        if browser is None:
            logger.error("Nenhum browser encontrado. Abra %s manualmente.", SERVER_URL)
            return 1
        launch_browser(browser, SERVER_URL)
        # Marca pra atexit não matar (servidor é externo, pode estar em uso)
        _server_proc = None
        if _browser_proc is not None:
            try:
                _browser_proc.wait()
            except KeyboardInterrupt:
                pass
        return 0

    # 1. Sobe server como subprocess
    try:
        start_server()
    except Exception as exc:
        logger.exception("falha iniciar server: %s", exc)
        return 1

    # 2. Estado compartilhado
    state = {"launched_browser": False}

    def on_ready():
        if state["launched_browser"]:
            return
        state["launched_browser"] = True
        browser = find_browser()
        if browser is None:
            logger.error("Nenhum browser. Abra %s manualmente.", SERVER_URL)
            return
        launch_browser(browser, SERVER_URL)

    def on_timeout():
        logger.error("Servidor nao subiu em %ds. Veja %s",
                     BOOT_TIMEOUT_S, SCRIPT_DIR.parent / "data" / "server.log")

    # 3. Splash (bloqueia ate fechar)
    icon_png = SCRIPT_DIR / "tradutor_icon_preview.png"
    splash = Splash(
        icon_path=str(icon_png) if icon_png.exists() else None,
        health_url=HEALTH_URL,
        timeout_s=BOOT_TIMEOUT_S,
        on_ready=on_ready,
        on_timeout=on_timeout,
    )
    splash.run()

    # 4. Espera browser fechar (mantem server vivo nesse meio tempo)
    if _browser_proc is not None:
        try:
            _browser_proc.wait()
        except KeyboardInterrupt:
            pass
    # atexit cuida do resto (mata server + browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
