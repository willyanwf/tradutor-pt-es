#!/usr/bin/env python3
"""Sobe um tunel Cloudflare expondo http://localhost:8765 com URL HTTPS publica.

Permite que pessoas acessem a tela /display do celular, de qualquer rede,
sem precisar mexer no router.

Uso:
    python tunnel.py                # quick tunnel (URL aleatoria)
    python tunnel.py --port 8765    # custom port

O script:
  1. Roda `cloudflared tunnel --url http://localhost:<port>` como subprocess
  2. Captura a URL .trycloudflare.com gerada
  3. Grava em <data>/tunnel_url.txt (pro server.py expor em /qr)
  4. Imprime QR code ASCII grande no terminal
  5. Reconecta automaticamente se o tunel cair
"""

import argparse
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TUNNEL_URL_FILE = DATA_DIR / "tunnel_url.txt"

URL_RE = re.compile(r"https?://[a-z0-9\-]+\.trycloudflare\.com")


def print_qr_ascii(url):
    try:
        import qrcode
    except ImportError:
        print("[tunnel] qrcode lib nao instalada — pulei o QR ascii", file=sys.stderr)
        return
    qr = qrcode.QRCode(box_size=1, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    print()
    qr.print_ascii(invert=True)
    print()


def banner(url):
    line = "=" * 78
    print()
    print(line)
    print(" TUNEL CLOUDFLARE PRONTO ".center(78, "="))
    print(line)
    print()
    print(f"  Operador:    {url}/")
    print(f"  Display:     {url}/display     <- compartilhe com a audiencia")
    print(f"  QR (mostrar): http://localhost:8765/qr")
    print()
    print_qr_ascii(f"{url}/display")
    print(line)
    print()


def stream_and_capture(proc, on_url):
    """Le stderr do cloudflared, captura a URL trycloudflare e propaga."""
    found = False
    for raw in iter(proc.stderr.readline, b""):
        try:
            line = raw.decode("utf-8", errors="replace").rstrip()
        except Exception:
            continue
        if not line:
            continue
        # Echo silencioso
        if not found:
            sys.stderr.write(f"[cloudflared] {line}\n")
        m = URL_RE.search(line)
        if m and not found:
            found = True
            on_url(m.group(0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765,
                        help="Porta local que o servidor de traducao roda")
    parser.add_argument("--cloudflared", default="cloudflared",
                        help="Caminho do binario cloudflared (default: PATH)")
    args = parser.parse_args()

    stop_event = threading.Event()

    def handle_sigint(signum, frame):
        stop_event.set()
        print("\n[tunnel] Encerrando...")
    signal.signal(signal.SIGINT, handle_sigint)

    backoff = 2.0
    while not stop_event.is_set():
        cmd = [args.cloudflared, "tunnel", "--url", f"http://localhost:{args.port}"]
        print(f"[tunnel] Iniciando: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
            )
        except FileNotFoundError:
            print("[tunnel] ERRO: cloudflared nao encontrado no PATH.")
            print("         Instale: winget install Cloudflare.cloudflared")
            return 1

        def on_url(url):
            TUNNEL_URL_FILE.write_text(url, encoding="utf-8")
            banner(url)

        t = threading.Thread(target=stream_and_capture, args=(proc, on_url), daemon=True)
        t.start()

        # Aguarda o subprocess ou stop_event
        while not stop_event.is_set():
            ret = proc.poll()
            if ret is not None:
                print(f"[tunnel] cloudflared saiu com codigo {ret} — reiniciando em {backoff}s")
                # Apaga URL stale
                if TUNNEL_URL_FILE.exists():
                    TUNNEL_URL_FILE.unlink()
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)
                break
            time.sleep(0.5)
        else:
            # stop_event acionou
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    if TUNNEL_URL_FILE.exists():
        TUNNEL_URL_FILE.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
