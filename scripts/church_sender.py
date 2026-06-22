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
import re
import sys
import threading
import time
import urllib.request
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


def _normalize_ws_url(typed):
    """Aceita qualquer coisa colada e devolve wss://HOST/ws.
    Converte https→wss, http→ws, ignora caminho (ex: /v2). None se inválido."""
    typed = (typed or "").strip()
    if not typed:
        return None
    m = re.match(r"^(wss|ws|https|http)://([^/]+)", typed, re.IGNORECASE)
    if not m:
        return None
    scheme = m.group(1).lower()
    host = m.group(2)
    ws_scheme = "wss" if scheme in ("wss", "https") else "ws"
    return f"{ws_scheme}://{host}/ws"


def _ensure_scheme(u):
    u = (u or "").strip()
    if re.match(r"^(wss?|https?)://", u, re.IGNORECASE):
        return u
    return "https://" + u  # link curto / domínio sem esquema


def _resolve_redirect(url):
    """Segue redirects HTTP e devolve a URL final. Pra links curtos (short.io)
    que apontam pro túnel atual — assim o link curto vira endereço permanente."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tradutor-igreja"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.geturl()
    except Exception:
        return url


def resolve_destination(url):
    """Devolve wss://HOST/ws final. Se for http(s) (ex.: link curto), segue o
    redirect pra descobrir o túnel atual; se já for ws/wss, só normaliza."""
    url = _ensure_scheme(url)
    if url.lower().startswith(("http://", "https://")):
        final = _resolve_redirect(url)
        return _normalize_ws_url(final) or _normalize_ws_url(url)
    return _normalize_ws_url(url)


def _input_with_timeout(prompt, timeout):
    """Lê uma linha com timeout (Windows). Retorna a string digitada, ou None se
    estourou o tempo SEM nada digitado. Se já começou a digitar, espera terminar."""
    print(prompt, end="", flush=True)
    try:
        import msvcrt
    except ImportError:
        try:
            return input()
        except EOFError:
            return None
    buf = ""
    start = time.time()
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                return buf
            elif ch == "\x08":  # backspace
                if buf:
                    buf = buf[:-1]
                    print("\b \b", end="", flush=True)
            elif ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            else:
                buf += ch
                print(ch, end="", flush=True)
        elif not buf and (time.time() - start) >= timeout:
            print()
            return None
        else:
            time.sleep(0.04)


def confirm_url(url, cfg, timeout=15):
    """Mostra o destino atual e deixa COLAR uma URL nova na hora. Se não digitar
    nada em `timeout` segundos, começa sozinho com o destino atual."""
    print("=" * 56)
    print("  TRADUTOR  —  IGREJA → CASA")
    print("=" * 56)
    print(f"  Destino (PC de casa):  {url}")
    if cfg.get("source_name"):
        print(f"  Fonte de áudio:        {cfg['source_name']}")
    print()
    print("  [Enter] começa com esse destino")
    print("  ou COLE uma nova URL (do PC de casa) e Enter pra trocar")
    print(f"  (começa sozinho em {timeout}s se você não digitar nada)")
    print()
    typed = _input_with_timeout("  URL: ", timeout)
    if typed is None or not typed.strip():
        print("  → usando o destino atual.\n")
        return url
    new_url = _normalize_ws_url(typed)
    if not new_url:
        print(f"  ⚠ Não entendi '{typed.strip()}'. Usando o destino atual.\n")
        return url
    cfg["url"] = new_url
    try:
        save_config(cfg)
        print(f"  ✓ Novo destino salvo: {new_url}\n")
    except Exception as exc:
        print(f"  (vou usar mesmo sem salvar: {exc})\n")
    return new_url


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


async def run(url, cfg, token=None, status=None, control=None):
    """Capta o áudio e envia pro PC de casa. Reconecta sozinho.

    status(state, msg): callback opcional ('connecting'|'connected'|'error'|'stopped').
    control: dict opcional {'stop':bool, 'url':str, 'reconnect':bool} — a bandeja
             mexe nisso pra parar ou trocar a URL em tempo real.
    """
    source = _resolve_saved_source(cfg)
    if not source:
        msg = "Fonte de áudio não configurada. Rode com --setup."
        print("⚠ " + msg)
        if status:
            status("error", msg)
        return 1
    spec = f"{source['kind']}:{source['id']}"
    print(f"[igreja] fonte: {source['name']}")

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
    try:
        while True:
            if control and control.get("stop"):
                break
            raw_url = (control.get("url") if control else None) or url
            if status:
                status("connecting", "Procurando o PC de casa…")
            ws_url = resolve_destination(raw_url)
            if not ws_url:
                print(f"[igreja] endereço inválido: {raw_url}")
                if status:
                    status("error", "endereço inválido")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 15)
                continue
            full = ws_url
            if token:
                sep = "&" if "?" in full else "?"
                full = f"{full}{sep}token={token}"
            print(f"[igreja] destino: {ws_url}")
            try:
                async with websockets.connect(full, max_size=None,
                                               ping_interval=20, ping_timeout=20) as ws:
                    print("[igreja] 🟢 conectado — enviando áudio")
                    if status:
                        status("connected", ws_url)
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
                            # checa comandos da bandeja (parar / trocar URL)
                            if control and (control.get("stop") or control.get("reconnect")):
                                if control.get("reconnect"):
                                    control["reconnect"] = False
                                break
                            try:
                                b = await asyncio.wait_for(audio_q.get(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            await ws.send(b)  # binário = PCM
                    finally:
                        drain_task.cancel()
            except Exception as exc:
                print(f"[igreja] 🔴 caiu ({exc}). Reconectando em {reconnect_delay:.0f}s...")
                if status:
                    status("error", str(exc)[:60])
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 15)
    finally:
        cap.stop()
        if status:
            status("stopped", "parado")
    return 0


# ============================================================
# Modo BANDEJA (system tray) — pro PC da igreja rodar discreto
# ============================================================
def _tray_icon_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(20, 28, 44, 255))
    d.ellipse((16, 16, 48, 48), fill=color)
    return img


def _ask_url_dialog(current):
    """Caixinha pra COLAR uma nova URL (tkinter). Retorna str ou None."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        val = simpledialog.askstring(
            "Trocar destino — Tradutor Igreja",
            "Cole o endereço do PC de casa\n"
            "(link curto te0u7om.s.gy/…, https:// ou wss://):",
            initialvalue=current, parent=root)
        root.destroy()
        return val.strip() if (val and val.strip()) else None
    except Exception as exc:
        print("dialog erro:", exc)
        return None


def _ask_source_dialog(cfg):
    """Janela pra ESCOLHER a fonte de áudio (entrada da mesa, etc). Retorna o
    dict da fonte escolhida {kind,id,name} ou None."""
    try:
        import tkinter as tk
        sources = _all_sources()
        if not sources:
            return None
        root = tk.Tk()
        root.title("Fonte de áudio — Tradutor Igreja")
        root.attributes("-topmost", True)
        root.geometry("560x380")
        tk.Label(root,
                 text="Escolha a ENTRADA com a voz do pastor\n"
                      "(no PC da igreja: a entrada da mesa de som):",
                 font=("Segoe UI", 10), justify="center").pack(pady=10)
        lb = tk.Listbox(root, font=("Segoe UI", 10), activestyle="dotbox")
        for s in sources:
            tag = "🎤 entrada" if s["kind"] == "mic" else "🔊 áudio do PC"
            lb.insert(tk.END, f"  {tag}    {s['name']}")
        cur_name = cfg.get("source_name")
        for i, s in enumerate(sources):
            if s["name"] == cur_name:
                lb.selection_set(i)
                lb.see(i)
        lb.pack(fill="both", expand=True, padx=14, pady=6)
        result = {"src": None}

        def ok():
            sel = lb.curselection()
            if sel:
                result["src"] = sources[sel[0]]
            root.destroy()

        frm = tk.Frame(root)
        frm.pack(pady=10)
        tk.Button(frm, text="Usar esta fonte", command=ok, width=16,
                  default="active").pack(side="left", padx=6)
        tk.Button(frm, text="Cancelar", command=root.destroy, width=12).pack(side="left", padx=6)
        lb.bind("<Double-Button-1>", lambda e: ok())
        root.mainloop()
        return result["src"]
    except Exception as exc:
        print("source dialog erro:", exc)
        return None


def run_tray(cfg, initial_url, token=None):
    """Roda na bandeja do Windows: ícone colorido por status + menu."""
    import pystray

    COLORS = {
        "connecting": (245, 176, 38, 255),   # âmbar
        "connected":  (95, 207, 128, 255),   # verde
        "error":      (255, 107, 107, 255),  # vermelho
        "stopped":    (130, 130, 130, 255),  # cinza
    }
    LABELS = {
        "connecting": "Procurando o PC de casa…",
        "connected":  "🟢 Conectado — enviando áudio",
        "error":      "🔴 Sem conexão — tentando de novo",
        "stopped":    "Parado",
    }

    st = {"state": "connecting"}
    control = {"stop": False, "url": initial_url, "reconnect": False}

    icon = pystray.Icon("tradutor_igreja", _tray_icon_image(COLORS["connecting"]),
                        "Tradutor Igreja → Casa")

    def on_status(state, msg):
        st["state"] = state
        try:
            icon.icon = _tray_icon_image(COLORS.get(state, COLORS["stopped"]))
            icon.title = "Tradutor Igreja → Casa\n" + LABELS.get(state, state)
            icon.update_menu()
        except Exception:
            pass

    worker_ref = {"t": None}

    def _worker():
        try:
            asyncio.run(run(initial_url, cfg, token=token,
                            status=on_status, control=control))
        except Exception as exc:
            on_status("error", str(exc))

    def start_worker():
        control["stop"] = False
        t = threading.Thread(target=_worker, daemon=True)
        worker_ref["t"] = t
        t.start()

    def restart_worker():
        # para o worker atual (libera a fonte de áudio) e sobe um novo
        control["stop"] = True
        t = worker_ref["t"]
        if t and t.is_alive():
            t.join(timeout=4)
        start_worker()

    # 1ª vez (ou fonte inválida pra esta máquina): pede a fonte numa janelinha,
    # em vez de capturar a fonte errada calado. No PC da igreja = entrada da mesa.
    if not _resolve_saved_source(cfg):
        picked = _ask_source_dialog(cfg)
        if picked:
            cfg["source_name"] = picked["name"]
            cfg["source_kind"] = picked["kind"]
            try:
                save_config(cfg)
            except Exception:
                pass

    start_worker()

    def m_status(item):
        return LABELS.get(st["state"], st["state"])

    def m_dest(item):
        u = control["url"]
        return ("Destino: " + u) if len(u) < 50 else ("Destino: …" + u[-46:])

    def m_source(item):
        return "Fonte: " + (cfg.get("source_name") or "?")

    def change_url(icon_, item):
        new = _ask_url_dialog(control["url"])
        if new:
            control["url"] = new
            cfg["url"] = new
            try:
                save_config(cfg)
            except Exception:
                pass
            control["reconnect"] = True   # reconecta já com a URL nova
            on_status("connecting", "trocando destino…")

    def change_source(icon_, item):
        picked = _ask_source_dialog(cfg)
        if picked:
            cfg["source_name"] = picked["name"]
            cfg["source_kind"] = picked["kind"]
            try:
                save_config(cfg)
            except Exception:
                pass
            on_status("connecting", "trocando fonte…")
            restart_worker()   # reinicia a captura com a fonte nova

    def do_quit(icon_, item):
        control["stop"] = True
        icon_.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(m_status, None, enabled=False),
        pystray.MenuItem(m_dest, None, enabled=False),
        pystray.MenuItem(m_source, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Trocar fonte de áudio…", change_source),
        pystray.MenuItem("Trocar URL de destino…", change_url),
        pystray.MenuItem("Sair", do_quit),
    )
    icon.run()
    return 0


def main():
    ap = argparse.ArgumentParser(description="App da igreja — envia áudio pro PC de casa")
    ap.add_argument("--setup", action="store_true", help="escolher a fonte de áudio (com medidor)")
    ap.add_argument("--list", action="store_true", help="listar fontes de áudio")
    ap.add_argument("--url", default=None, help="WebSocket do PC de casa (sobrescreve config)")
    ap.add_argument("--token", default=None, help="token de autenticação (Fase 2)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="não pergunta a URL na abertura (usa a salva direto)")
    ap.add_argument("--tray", action="store_true", help="força rodar na bandeja")
    ap.add_argument("--console", action="store_true",
                    help="força modo console (sem bandeja) — útil pra ver logs")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return 0
    if args.setup:
        return cmd_setup()

    cfg = load_config()
    # Primeira vez (duplo-clique sem nada configurado) → assistente
    did_setup = False
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
        did_setup = True

    initial_url = args.url or cfg.get("url") or DEFAULT_URL
    token = args.token or cfg.get("token")

    # Modo: BANDEJA (padrão no .exe) ou CONSOLE (--console, ou rodando como .py).
    frozen = getattr(sys, "frozen", False)
    use_tray = args.tray or (frozen and not args.console)

    if use_tray:
        return run_tray(cfg, initial_url, token=token)

    # Console: deixa confirmar/colar a URL (a menos que --url ou --no-prompt).
    if not args.url and not args.no_prompt and not did_setup:
        initial_url = confirm_url(initial_url, cfg)
    try:
        return asyncio.run(run(initial_url, cfg, token=token))
    except KeyboardInterrupt:
        print("\n[igreja] encerrado.")
        return 0


if __name__ == "__main__":
    _frozen = getattr(sys, "frozen", False)
    if _frozen:
        # .exe sem console: manda os logs pra um arquivo do lado do programa
        try:
            _logf = open(Path(sys.executable).parent / "church_sender.log",
                         "a", encoding="utf-8", buffering=1)
            sys.stdout = _logf
            sys.stderr = _logf
        except Exception:
            pass
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    rc = 0
    try:
        rc = main()
    except Exception as exc:
        import traceback
        print(f"\n⚠ Erro: {exc}")
        traceback.print_exc()
        rc = 1
        if _frozen:
            # Sem console: avisa numa caixinha
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk(); root.withdraw()
                messagebox.showerror(
                    "Tradutor Igreja — erro",
                    f"Algo deu errado:\n\n{exc}\n\n"
                    f"Detalhes em church_sender.log (ao lado do programa).")
                root.destroy()
            except Exception:
                pass
    else:
        if not _frozen:
            # rodando como .py no console: segura pra ver a mensagem
            try:
                input("\nPressione Enter para fechar...")
            except Exception:
                pass
    sys.exit(rc)
