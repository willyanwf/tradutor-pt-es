#!/usr/bin/env python3
"""Guardião do túnel: mantém o Cloudflare tunnel no ar e o LINK CURTO sempre
apontando pro túnel atual.

- Sobe `cloudflared tunnel --url http://localhost:PORT`
- Lê a saída e acha a URL https://xxxx.trycloudflare.com
- Quando a URL aparece/muda, atualiza o link curto (short.io) -> URL + /v2
- Se o cloudflared cair, reinicia e re-publica a nova URL automaticamente

Resultado: se o túnel trocar de endereço, a igreja reconecta sozinha — o app da
igreja segue o link curto, que estará sempre atualizado. Ninguém precisa mexer.

Uso:
    python tunnel_keeper.py                 # porta 8766 (padrão)
    python tunnel_keeper.py --port 8766
"""
import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import shortio_update

CF_CANDIDATES = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
]
URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


def find_cloudflared():
    for c in CF_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("cloudflared") or "cloudflared"


def publish(url):
    dest = url.rstrip("/") + "/v2"
    try:
        ok, msg = shortio_update.update_destination(dest)
    except Exception as exc:
        ok, msg = False, str(exc)
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] link curto -> {dest}  ({'OK' if ok else 'FALHOU: ' + msg})",
          flush=True)


def run_once(cf, port):
    """Sobe o cloudflared, publica a URL quando aparece, lê até cair."""
    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://localhost:{port}", "--edge-ip-version", "4"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    current = None
    try:
        for line in proc.stdout:
            m = URL_RE.search(line)
            if m and m.group(0) != current:
                current = m.group(0)
                print(f"[tunnel] no ar: {current}", flush=True)
                publish(current)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    return proc.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    cf = find_cloudflared()
    print(f"[keeper] cloudflared: {cf}", flush=True)
    print(f"[keeper] mantendo tunel -> localhost:{args.port} + link curto", flush=True)
    delay = 2
    while True:
        try:
            run_once(cf, args.port)
        except Exception as exc:
            print(f"[keeper] erro: {exc}", flush=True)
        print(f"[keeper] tunel caiu. Reiniciando em {delay}s...", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 20)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
