"""App da IGREJA — captura o áudio do culto e ENVIA pro PC de casa traduzir.

Arquitetura: o PC da igreja não precisa de GPU. Ele só capta o áudio (da
interface/mesa) e manda PCM 16kHz mono pelo WebSocket pro server.py de casa,
que faz Whisper + tradução + serve a tela pros celulares.

Conecta no /ws como se fosse um "operador" headless: manda config PT→ES e
streama os bytes de áudio (mesmo formato que o navegador manda hoje).

Reusa LocalAudioCapture (system_capture.py) pra captar + resamplear pra 16k mono.

Uso:
    # 1) Escolher a fonte de áudio (uma vez) — mostra medidor de nível
    python church_sender.py --setup

    # 2) Rodar (manda pro PC de casa)
    python church_sender.py --url wss://tradutor.siaccon.com.br/ws

    # Atalhos
    python church_sender.py --list                 (lista devices)
    python church_sender.py --url ws://127.0.0.1:8766/ws   (teste local)
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

try:
    import websockets
except ImportError:
    sys.exit("Falta 'websockets'. Rode: pip install websockets")

from system_capture import LocalAudioCapture, list_devices

# Endereço do PC de casa embutido (vira o túnel FIXO quando estiver pronto).
# Pode ser sobrescrito no --setup ou via --url.
DEFAULT_URL = "wss://tradutor.siaccon.com.br/ws"

# Config fica ao lado do .exe/script (pasta gravável). Em .exe onefile,
# __file__ aponta pra pasta temporária — usa a pasta do executável.
def _config_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

CONFIG_PATH = _config_dir() / "church_sender_config.json"


def load_config():
    if CONFIG_PATH.exists():
        try:
            # utf-8-sig tolera BOM (PowerShell/Bloco de Notas salvam com BOM)
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _all_sources():
    """Lista unificada de fontes: mics/inputs + loopbacks. Cada item:
    {kind: 'mic'|'loopback', id, name}."""
    dev = list_devices()
    out = []
    for m in dev.get("microphones", []):
        out.append({"kind": "mic", "id": m["id"], "name": m["name"]})
    for l in dev.get("loopbacks", []):
        out.append({"kind": "loopback", "id": l["id"], "name": l["name"]})
    return out


def cmd_list():
    sources = _all_sources()
    print("\n=== Fontes de áudio disponíveis ===")
    for i, s in enumerate(sources):
        tag = "🎤 entrada" if s["kind"] == "mic" else "🔊 loopback"
        print(f"  [{i}] {tag}  {s['name']}")
    print()
    return sources


def _level_meter(source, seconds=8):
    """Captura a fonte por N segundos mostrando barra de nível ao vivo."""
    spec = f"{source['kind']}:{source['id']}"
    state = {"peak": 0.0, "last": 0.0}

    def on_pcm(b):
        if not b:
            return
        a = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0
        if len(a):
            rms = float(np.sqrt(np.mean(a * a)))
            state["last"] = rms
            state["peak"] = max(state["peak"], rms)

    cap = LocalAudioCapture(spec, on_pcm)
    try:
        cap.start()
    except Exception as exc:
        print(f"  ⚠ não consegui abrir essa fonte: {exc}")
        return False
    print(f"\n  Fale no microfone / bata na mesa. Medindo {seconds}s...\n")
    end = time.time() + seconds
    try:
        while time.time() < end:
            lvl = min(1.0, state["last"] * 4)
            bar = "█" * int(lvl * 40)
            print(f"\r  nível: |{bar:<40}| {state['last']*100:5.1f}%", end="", flush=True)
            time.sleep(0.08)
    finally:
        cap.stop()
    print(f"\n\n  pico detectado: {state['peak']*100:.1f}%")
    return state["peak"] > 0.01


def cmd_setup():
    sources = cmd_list()
    if not sources:
        print("Nenhuma fonte encontrada. Conecte a interface de áudio e tente de novo.")
        return 1
    try:
        idx = int(input("Digite o número da fonte que tem a VOZ DO PASTOR: ").strip())
        source = sources[idx]
    except (ValueError, IndexError):
        print("Escolha inválida.")
        return 1
    ok = _level_meter(source)
    if not ok:
        print("\n  ⚠ Quase não detectei áudio. Confirme que é a fonte certa e que tem som.")
    resp = input("\n  Salvar essa fonte? [S/n] ").strip().lower()
    if resp not in ("", "s", "sim", "y"):
        print("  (não salvo)")
        return 0
    cfg = load_config()
    cfg["source_name"] = source["name"]
    cfg["source_kind"] = source["kind"]

    # Endereço do PC de casa (pra onde mandar). Pré-preenchido com DEFAULT_URL.
    cur = cfg.get("url") or DEFAULT_URL
    print(f"\n  Endereço do PC de casa (pra onde mandar o áudio).")
    print(f"  [Enter] mantém: {cur}")
    typed = input("  Novo endereço (ou Enter): ").strip()
    cfg["url"] = typed if typed else cur

    save_config(cfg)
    print(f"\n  ✓ Configurado!")
    print(f"    Fonte:    {source['name']}")
    print(f"    Destino:  {cfg['url']}")
    print(f"\n  Pronto. Da próxima vez é só abrir — já lembra de tudo.")
    return 0


def _resolve_saved_source(cfg):
    """Acha a fonte salva pelo NOME (id muda entre reinícios)."""
    name = cfg.get("source_name")
    kind = cfg.get("source_kind", "mic")
    if not name:
        return None
    for s in _all_sources():
        if s["kind"] == kind and s["name"] == name:
            return s
    # fallback: casa por substring (driver às vezes muda sufixo)
    for s in _all_sources():
        if name.split("(")[0].strip() in s["name"]:
            return s
    return None


async def run(url, cfg, token=None):
    source = _resolve_saved_source(cfg)
    if not source:
        print("⚠ Fonte de áudio não configurada. Rode primeiro: python church_sender.py --setup")
        return 1
    spec = f"{source['kind']}:{source['id']}"
    print(f"[igreja] fonte: {source['name']}")
    print(f"[igreja] destino: {url}")

    loop = asyncio.get_event_loop()
    audio_q: asyncio.Queue = asyncio.Queue(maxsize=200)

    def on_pcm(b):
        try:
            loop.call_soon_threadsafe(_enqueue, b)
        except Exception:
            pass

    def _enqueue(b):
        if audio_q.full():
            try:
                audio_q.get_nowait()  # descarta o mais antigo (não acumula atraso)
            except asyncio.QueueEmpty:
                pass
        audio_q.put_nowait(b)

    cap = LocalAudioCapture(spec, on_pcm)
    cap.start()

    reconnect_delay = 1.0
    ws_url = url
    if token:
        sep = "&" if "?" in ws_url else "?"
        ws_url = f"{ws_url}{sep}token={token}"

    try:
        while True:
            try:
                async with websockets.connect(ws_url, max_size=None,
                                               ping_interval=20, ping_timeout=20) as ws:
                    print("[igreja] 🟢 conectado — enviando áudio")
                    reconnect_delay = 1.0
                    # drena 'ready' + manda config PT->ES
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    except Exception:
                        pass
                    await ws.send(json.dumps({"type": "config", "src": "pt", "tgt": "es"}))

                    async def drain():
                        # descarta mensagens do servidor (resultados/telemetria)
                        try:
                            async for _ in ws:
                                pass
                        except Exception:
                            pass

                    drain_task = asyncio.ensure_future(drain())
                    try:
                        while True:
                            b = await audio_q.get()
                            await ws.send(b)  # binário = PCM
                    finally:
                        drain_task.cancel()
            except Exception as exc:
                print(f"[igreja] 🔴 caiu ({exc}). Reconectando em {reconnect_delay:.0f}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 15)
    finally:
        cap.stop()


def main():
    ap = argparse.ArgumentParser(description="App da igreja — envia áudio pro PC de casa")
    ap.add_argument("--setup", action="store_true", help="escolher a fonte de áudio (com medidor)")
    ap.add_argument("--list", action="store_true", help="listar fontes de áudio")
    ap.add_argument("--url", default=None, help="WebSocket do PC de casa (sobrescreve config)")
    ap.add_argument("--token", default=None, help="token de autenticação (Fase 2)")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return 0
    if args.setup:
        return cmd_setup()

    cfg = load_config()
    # Primeira vez (duplo-clique sem nada configurado) → assistente
    if not cfg.get("source_name"):
        print("=" * 56)
        print("  PRIMEIRA VEZ — vamos configurar (uma vez só)")
        print("=" * 56)
        rc = cmd_setup()
        if rc != 0:
            return rc
        cfg = load_config()
        if not cfg.get("source_name"):
            return 1

    url = args.url or cfg.get("url") or DEFAULT_URL
    token = args.token or cfg.get("token")
    try:
        return asyncio.run(run(url, cfg, token=token))
    except KeyboardInterrupt:
        print("\n[igreja] encerrado.")
        return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rc = 0
    try:
        rc = main()
    except Exception as exc:
        print(f"\n⚠ Erro: {exc}")
        rc = 1
    # Se for .exe (duplo-clique), segura a janela pra ver a mensagem
    if getattr(sys, "frozen", False):
        try:
            input("\nPressione Enter para fechar...")
        except Exception:
            pass
    sys.exit(rc)
