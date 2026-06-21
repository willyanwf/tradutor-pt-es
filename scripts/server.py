#!/usr/bin/env python3
"""Servidor WebSocket pra traducao simultanea PT <-> ES.

Duas rotas WebSocket:
  /ws         - operador: envia audio (bytes PCM), recebe traducoes
  /ws/display - publico/projetor: so recebe broadcasts das traducoes

Duas paginas HTML:
  /         -> static/index.html   (painel do operador, captura mic)
  /display  -> static/display.html (tela publica, fonte grande, fullscreen)

Como rodar:
    python server.py                          # 0.0.0.0:8765 (LAN visivel)
    python server.py --host 127.0.0.1         # so localhost
    python server.py --port 9000 --model base # porta + modelo
    python server.py --silence-ms 500         # frases fecham mais rapido
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path


def _setup_cuda_dll_dirs():
    """Pre-carrega DLLs CUDA do pacote pip nvidia-*-cu12 no Windows.

    Sem CUDA Toolkit instalado, os pacotes nvidia-cublas-cu12 e
    nvidia-cudnn-cu12 trazem as DLLs em <site-packages>/nvidia/.../bin/.
    `os.add_dll_directory` nem sempre basta pro ctranslate2 (que faz
    LoadLibrary lazy na 1a chamada de encode). Solucao: pre-carregar
    cada DLL via ctypes.WinDLL — forca o Windows a manter o handle aberto
    e o LoadLibrary subsequente retorna esse handle.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import site
        bin_dirs = []
        for site_dir in site.getsitepackages() + [site.getusersitepackages()]:
            nv = Path(site_dir) / "nvidia"
            if not nv.exists():
                continue
            for sub in nv.iterdir():
                bin_dir = sub / "bin"
                if bin_dir.exists():
                    bin_dirs.append(bin_dir)

        # Adiciona aos DLL search paths
        for d in bin_dirs:
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass

        # Pre-carrega DLLs criticas (ordem importa: cublas depende de cudart-like libs)
        preload_order = [
            "cudart64_*.dll",     # CUDA runtime se houver
            "nvrtc*.dll",
            "cudnn_*64_*.dll",
            "cudnn64_*.dll",
            "cublasLt64_*.dll",
            "cublas64_*.dll",
        ]
        loaded = 0
        for pattern in preload_order:
            for d in bin_dirs:
                for dll_path in d.glob(pattern):
                    try:
                        ctypes.WinDLL(str(dll_path))
                        loaded += 1
                    except OSError:
                        pass
        print(f"[cuda-dll] {len(bin_dirs)} dirs, {loaded} DLLs pre-carregadas",
              file=sys.stderr)
    except Exception as exc:
        print(f"[cuda-dll] aviso: {exc}", file=sys.stderr)


_setup_cuda_dll_dirs()

try:
    import webrtcvad
    from faster_whisper import WhisperModel
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
    import uvicorn
except ImportError as exc:
    sys.exit(f"Dependencia faltando: {exc}. Rode: python setup.py")

from pipeline import (
    FRAME_BYTES,
    DEFAULT_SILENCE_MS,
    DEFAULT_STREAM_PARTIAL_MS,
    DEFAULT_STREAM_MAX_MS,
    DEFAULT_STREAM_SILENCE_MS,
    PhraseDetector,
    StreamingAccumulator,
    get_translator,
    transcribe_and_translate,
)
from translation_cache import SQLiteTranslationCache, IncrementalTranslator
from marian_translator import MarianHub
from system_capture import LocalAudioCapture, list_devices as list_audio_devices
import youtube_lyrics


SCRIPT_DIR = Path(__file__).parent.resolve()
STATIC_DIR = SCRIPT_DIR / "static"

logger = logging.getLogger("tradutor")


# ===== Persistência de estado =====
# Configs que sobrevivem a reinício do servidor — gravadas em JSON ao lado
# do server.py. Atualmente: modo intérprete. Pode expandir.
STATE_FILE = Path(__file__).parent / "state.json"
_DEFAULT_DISPLAY_CONFIG = {
    "interpreter_mode": True,  # 🎭 modo intérprete (default ON)
}


def load_persisted_display_config():
    """Lê config do display do disco. Se arquivo não existe ou está corrompido,
    retorna defaults (e nao crasha)."""
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            saved = data.get("display_config", {}) if isinstance(data, dict) else {}
            # Mescla com defaults pra garantir todas as chaves esperadas
            merged = dict(_DEFAULT_DISPLAY_CONFIG)
            for k, v in saved.items():
                if k in _DEFAULT_DISPLAY_CONFIG and type(v) == type(_DEFAULT_DISPLAY_CONFIG[k]):
                    merged[k] = v
            return merged
    except Exception as exc:
        # Log no setup do logger ainda nao existe garantidamente — usa stderr
        print(f"[state] falha ao ler {STATE_FILE}: {exc}", file=sys.stderr)
    return dict(_DEFAULT_DISPLAY_CONFIG)


def save_persisted_display_config(cfg):
    """Persiste config no disco. Tolerante a erro (não derruba o servidor)."""
    try:
        existing = {}
        if STATE_FILE.exists():
            try:
                existing = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        existing["display_config"] = dict(cfg)
        STATE_FILE.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[state] falha ao salvar {STATE_FILE}: {exc}", file=sys.stderr)


class DisplayHub:
    """Broadcast pra todos os WebSockets de /ws/display abertos.
    Rastreia metadados de cada conexao pra montar painel de audiencia."""

    def __init__(self):
        self._sockets = set()
        self._meta = {}              # ws -> dict(ip, ua, first_seen, last_seen)
        self._device_history = {}    # device_id -> dict(ip, ua, first_seen, last_seen, sessions)
        self._peak_simultaneous = 0
        self._lock = asyncio.Lock()
        # Config compartilhada entre todos os displays — operador controla,
        # todos os clientes /ws/display recebem o estado. Quando um display
        # se conecta, recebe o snapshot atual. Quando o operador muda, todos
        # os displays já conectados recebem broadcast.
        # Carrega do disco (sobrevive a restart do servidor).
        self.display_config = load_persisted_display_config()

    @staticmethod
    def _device_id(ip, ua):
        """Hash curto de ip+ua pra identificar dispositivo unico."""
        import hashlib
        return hashlib.md5(f"{ip}|{ua}".encode()).hexdigest()[:10]

    @staticmethod
    def _short_ua(ua):
        """Reduz user-agent a algo legivel."""
        if not ua: return "?"
        u = ua.lower()
        if "iphone" in u or "ipad" in u: return "📱 iOS"
        if "android" in u: return "📱 Android"
        if "windows" in u: return "🖥 Windows"
        if "macintosh" in u or "mac os" in u: return "🖥 macOS"
        if "linux" in u: return "🖥 Linux"
        return "❓ Outro"

    async def add(self, ws, ip="?", ua=""):
        from datetime import datetime as _dt
        async with self._lock:
            self._sockets.add(ws)
            now = _dt.now().isoformat()
            dev_id = self._device_id(ip, ua)
            self._meta[ws] = {
                "device_id": dev_id, "ip": ip, "ua": ua,
                "ua_short": self._short_ua(ua),
                "first_seen": now,
            }
            # Atualiza historico (device permanente)
            hist = self._device_history.get(dev_id)
            if hist:
                hist["last_seen"] = now
                hist["sessions"] += 1
            else:
                self._device_history[dev_id] = {
                    "ip": ip, "ua_short": self._short_ua(ua),
                    "first_seen": now, "last_seen": now, "sessions": 1,
                }
            # Pico simultaneo
            if len(self._sockets) > self._peak_simultaneous:
                self._peak_simultaneous = len(self._sockets)

    async def remove(self, ws):
        from datetime import datetime as _dt
        async with self._lock:
            self._sockets.discard(ws)
            meta = self._meta.pop(ws, None)
            if meta:
                dev = self._device_history.get(meta["device_id"])
                if dev:
                    dev["last_seen"] = _dt.now().isoformat()

    async def broadcast(self, payload):
        async with self._lock:
            sockets = list(self._sockets)
        if not sockets:
            return
        dead = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._sockets.discard(ws)
                    self._meta.pop(ws, None)

    def count(self):
        return len(self._sockets)

    async def set_display_config(self, **kwargs):
        """Atualiza config dos displays e broadcasta o snapshot atual.
        Operador chama isso quando muda algo (ex: interpreter_mode).
        Todos os clientes /ws/display recebem o estado completo.
        Persiste em disco pra sobreviver a restart do servidor."""
        changed = False
        for k, v in kwargs.items():
            if k in self.display_config and self.display_config[k] != v:
                self.display_config[k] = v
                changed = True
        if changed:
            # Salva fora do lock pra não bloquear (I/O síncrono em thread separada)
            cfg_copy = dict(self.display_config)
            asyncio.create_task(asyncio.to_thread(save_persisted_display_config, cfg_copy))
        await self.broadcast({
            "type": "display-config",
            **self.display_config,
        })

    def display_config_snapshot(self):
        """Snapshot pra mandar ao display recém-conectado (initial sync)."""
        return {"type": "display-config", **self.display_config}

    def audience_stats(self):
        """Snapshot pra endpoint /api/audience."""
        connected_devices = {}
        for ws, meta in self._meta.items():
            did = meta["device_id"]
            if did not in connected_devices:
                connected_devices[did] = {
                    "device_id": did,
                    "ip": meta["ip"],
                    "ua_short": meta["ua_short"],
                    "first_seen": meta["first_seen"],
                    "tabs": 1,
                }
            else:
                connected_devices[did]["tabs"] += 1
        return {
            "connected_now": len(self._sockets),
            "unique_devices_connected": len(connected_devices),
            "unique_devices_total": len(self._device_history),
            "peak_simultaneous": self._peak_simultaneous,
            "devices_now": list(connected_devices.values()),
            "history": [
                {"device_id": did, **info}
                for did, info in sorted(
                    self._device_history.items(),
                    key=lambda x: x[1]["last_seen"], reverse=True
                )
            ][:50],
        }


def make_app(model, default_translator, default_backend,
             default_partial_ms, default_max_ms, default_silence_ms,
             default_streaming, cache=None):
    app = FastAPI(title="Tradutor PT<->ES")
    hub = DisplayHub()
    # Cache de músicas já preparadas (video_id -> {title, source, lines:[{start,pt,es}]})
    prepared_songs = {}

    @app.get("/")
    async def root(request: Request):
        """Roteamento por origem:
          - Acesso LOCAL (localhost / 127.0.0.1) → painel do operador
          - Acesso EXTERNO (tunnel, LAN, qualquer outro host) → tela pública
        Operador no celular pode acessar via /operator explicitamente."""
        host = (request.headers.get("host") or "").lower().split(":")[0]
        is_local = host in ("localhost", "127.0.0.1", "::1")
        page_name = "index.html" if is_local else "display.html"
        page = STATIC_DIR / page_name
        if not page.exists():
            return JSONResponse(
                {"error": f"static/{page_name} nao encontrado em {STATIC_DIR}"},
                status_code=500,
            )
        return FileResponse(page)

    @app.get("/operator")
    async def operator():
        """Acesso explicito ao painel do operador — util quando vem de fora
        (tunnel, LAN) e o operador esta usando outro dispositivo."""
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return JSONResponse({"error": "static/index.html nao encontrado"}, status_code=500)
        return FileResponse(index)

    @app.get("/display")
    async def display():
        page = STATIC_DIR / "display.html"
        if not page.exists():
            return JSONResponse({"error": f"static/display.html nao encontrado em {STATIC_DIR}"}, status_code=500)
        return FileResponse(page)

    @app.get("/display-v2")
    async def display_v2():
        """Display v2 — CART append-only com modo 'A mi ritmo'.
        Audiência toca botão pra avançar próxima frase no próprio ritmo.
        Resolve queixa: texto muda enquanto leio, me perco.
        Teste lado-a-lado com /display antigo."""
        page = STATIC_DIR / "display-v2.html"
        if not page.exists():
            return JSONResponse({"error": f"static/display-v2.html nao encontrado em {STATIC_DIR}"}, status_code=500)
        return FileResponse(page)

    @app.get("/v2")
    async def v2_alias():
        """Atalho pra /display-v2."""
        return await display_v2()

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "ts": datetime.now().isoformat(),
            "displays_connected": hub.count(),
        }

    @app.get("/qr")
    async def qr_code():
        """Pagina com QR code grande pra audiencia apontar o celular.

        Le a URL publica de <data>/tunnel_url.txt (escrita por tunnel.py).
        Se nao houver tunel ativo, mostra instrucoes.
        """
        from fastapi.responses import HTMLResponse
        import io

        tunnel_file = SCRIPT_DIR.parent / "data" / "tunnel_url.txt"
        if not tunnel_file.exists():
            return HTMLResponse(
                """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>QR</title>
<style>body{margin:0;background:#0f1115;color:#f5f5f5;
font-family:system-ui,sans-serif;display:flex;flex-direction:column;
align-items:center;justify-content:center;min-height:100vh;padding:24px;text-align:center}
h1{font-size:32px;margin:0 0 12px}
code{background:#161a22;padding:6px 12px;border-radius:6px;font-size:18px;display:inline-block;margin-top:8px}
p{font-size:18px;max-width:600px;line-height:1.55;color:#8a92a3}
</style></head><body>
<h1>Túnel não está rodando</h1>
<p>Rode em outro terminal:</p>
<code>python scripts/tunnel.py</code>
<p style="margin-top:24px">Isso vai criar uma URL HTTPS pública (Cloudflare Tunnel)
e essa página vai mostrar o QR code automaticamente.</p>
</body></html>""",
                status_code=200,
            )

        public_url = tunnel_file.read_text(encoding="utf-8").strip()
        display_url = f"{public_url}/display"

        try:
            import qrcode
            import qrcode.image.svg
            factory = qrcode.image.svg.SvgPathImage
            img = qrcode.make(display_url, image_factory=factory, box_size=20, border=2)
            buf = io.BytesIO()
            img.save(buf)
            qr_svg = buf.getvalue().decode("utf-8")
            # Remove o XML declaration pra inline em HTML
            qr_svg = qr_svg.split("?>", 1)[-1] if "?>" in qr_svg else qr_svg
        except Exception as exc:
            return HTMLResponse(f"<pre>Falha gerando QR: {exc}</pre>", status_code=500)

        html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>QR — Tradução ao vivo</title>
<style>
  body {{ margin: 0; background: #0f1115; color: #f5f5f5;
    font-family: "Inter", -apple-system, system-ui, sans-serif;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; min-height: 100vh; padding: 4vh 4vw;
    text-align: center; }}
  h1 {{ font-size: clamp(28px, 4vw, 64px); margin: 0 0 1.5vh; font-weight: 700; }}
  .sub {{ font-size: clamp(16px, 2vw, 28px); color: #8a92a3; margin-bottom: 4vh; max-width: 800px; }}
  .qr {{ background: #fff; padding: 3vh 3vw; border-radius: 16px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.6); }}
  .qr svg {{ width: 60vmin; height: 60vmin; display: block; }}
  .url {{ margin-top: 4vh; font-family: monospace; font-size: clamp(14px, 1.4vw, 22px);
    color: #ffb455; background: #161a22; padding: 12px 24px; border-radius: 8px;
    word-break: break-all; max-width: 90vw; }}
  .tip {{ margin-top: 3vh; font-size: clamp(14px, 1.4vw, 22px); color: #5fcf80; opacity: 0.8; }}
</style></head><body>
<h1>📱 Tradução ao vivo</h1>
<div class="sub">Aponte a câmera do celular pra esse código.<br>
A tela em espanhol abre automaticamente.</div>
<div class="qr">{qr_svg}</div>
<div class="url">{display_url}</div>
<div class="tip">PT → ES &middot; sin instalación &middot; gratuito</div>
</body></html>"""
        return HTMLResponse(html, status_code=200)

    @app.get("/api/audio-devices")
    async def api_audio_devices():
        """Lista mics + loopbacks disponiveis pra captura local no servidor."""
        return list_audio_devices()

    # ===== Modo Música: buscar no YouTube + letra traduzida =====
    @app.get("/api/youtube/search")
    async def api_youtube_search(q: str = ""):
        """Busca músicas no YouTube via yt-dlp."""
        if not youtube_lyrics.ytdlp_available():
            return JSONResponse(
                {"error": "yt-dlp não instalado", "results": []}, status_code=503)
        try:
            results = await asyncio.to_thread(youtube_lyrics.search, q, 8)
            return {"results": results}
        except Exception as exc:
            logger.warning("youtube search erro: %s", exc)
            return JSONResponse({"error": str(exc), "results": []}, status_code=500)

    @app.post("/api/youtube/prepare")
    async def api_youtube_prepare(request: Request):
        """Baixa a legenda do vídeo e traduz cada linha pro ES.
        Retorna {title, source, is_spanish, lines:[{start, pt, es}]}."""
        body = await request.json()
        video_id = (body.get("video_id") or "").strip()
        title = body.get("title") or video_id
        if not video_id:
            return JSONResponse({"error": "video_id obrigatório"}, status_code=400)

        # Cache hit
        if video_id in prepared_songs:
            return prepared_songs[video_id]

        cap = await asyncio.to_thread(youtube_lyrics.get_captions, video_id)
        if not cap or not cap.get("lines"):
            return JSONResponse({
                "error": "Esse vídeo não tem legenda de texto. Escolha outro "
                         "(geralmente os vídeos OFICIAIS têm).",
                "has_captions": False,
            }, status_code=404)

        is_spanish = cap.get("is_spanish", False)

        def _translate_all():
            out = []
            for ln in cap["lines"]:
                pt = ln["text"]
                if is_spanish:
                    es = pt  # já está em espanhol
                    pt_show = ""
                else:
                    try:
                        es = default_translator(pt, "pt", "es")
                    except Exception as exc:
                        logger.warning("traduzir linha falhou: %s", exc)
                        es = pt
                    pt_show = pt
                out.append({"start": ln["start"], "pt": pt_show, "es": es})
            return out

        lines = await asyncio.to_thread(_translate_all)
        song = {
            "video_id": video_id,
            "title": title,
            "source": cap.get("source"),
            "is_spanish": is_spanish,
            "auto": cap.get("auto", False),
            "lines": lines,
        }
        prepared_songs[video_id] = song
        return song

    @app.post("/api/youtube/show")
    async def api_youtube_show(request: Request):
        """Manda a música preparada pros displays (audiência vê a letra ES)."""
        body = await request.json()
        video_id = (body.get("video_id") or "").strip()
        song = prepared_songs.get(video_id)
        if not song:
            return JSONResponse({"error": "música não preparada"}, status_code=404)
        await hub.broadcast({
            "type": "song",
            "title": song["title"],
            "lines": song["lines"],
            "source": song["source"],
        })
        return {"ok": True, "displays": hub.count(), "lines": len(song["lines"])}

    @app.post("/api/youtube/hide")
    async def api_youtube_hide():
        """Tira a letra dos displays — volta pro modo tradução normal."""
        await hub.broadcast({"type": "song-hide"})
        return {"ok": True}

    @app.get("/api/audience")
    async def api_audience():
        """Stats de quem esta vendo a tela publica agora + historico de
        dispositivos unicos que ja conectaram durante a sessao."""
        return hub.audience_stats()

    @app.get("/api/display-config")
    async def api_display_config():
        """Estado atual do display_config (modo intérprete, etc). Útil pra
        debug e pra verificar a persistência do state.json."""
        return {
            "config": hub.display_config,
            "persisted_at": str(STATE_FILE) if STATE_FILE.exists() else None,
        }

    @app.post("/api/open-browser")
    async def api_open_browser():
        """Abre o tradutor no navegador PADRAO do sistema (com URL bar, abas, etc).
        Util quando o operador esta no aplicativo Windows (Edge --app sem URL bar)
        e quer testar/comparar no browser normal."""
        import webbrowser
        url = "http://127.0.0.1:8765"
        try:
            opened = webbrowser.open(url, new=2)  # new=2: nova aba se possivel
            return {"opened": opened, "url": url}
        except Exception as exc:
            return JSONResponse({"opened": False, "error": str(exc)}, status_code=500)

    @app.get("/selftest")
    async def selftest():
        """Pre-flight check antes do sermao. Operador clica e ve status de tudo."""
        import time as _t
        import numpy as _np
        checks = []

        def add(name, ok, msg="", t0=None):
            checks.append({
                "name": name, "ok": ok, "msg": msg,
                "duration_ms": int((_t.monotonic() - t0) * 1000) if t0 else None,
            })

        # 1. Static files
        for fname in ("index.html", "display.html"):
            ok = (STATIC_DIR / fname).exists()
            add(f"static/{fname}", ok, "ok" if ok else "ARQUIVO FALTANDO")

        # 2. Whisper transcribe (1s de audio sintético)
        try:
            t0 = _t.monotonic()
            test_audio = (_np.random.randn(16000) * 0.01).astype("float32")
            segs, info = await asyncio.to_thread(
                lambda: model.transcribe(test_audio, language="pt", beam_size=1)
            )
            list(segs)
            add("whisper transcribe", True, f"lang={info.language}", t0)
        except Exception as exc:
            add("whisper transcribe", False, str(exc)[:120])

        # 3. Translator com frase religiosa conhecida
        try:
            t0 = _t.monotonic()
            es = await asyncio.to_thread(
                lambda: default_translator("Jesus Cristo é o Senhor", "pt", "es")
            )
            ok = bool(es and ("Señor" in es or "Senor" in es))
            add("translator pt→es", ok, f'"{es}"' if ok else f"output suspeito: {es!r}", t0)
        except Exception as exc:
            add("translator pt→es", False, str(exc)[:120])

        # 4. VRAM disponível
        try:
            import torch
            if torch.cuda.is_available():
                free_b, total_b = torch.cuda.mem_get_info()
                free_mb = free_b / (1024**2)
                add("vram livre", free_mb > 300,
                    f"{int(free_mb)}MB livre de {int(total_b/(1024**2))}MB total")
        except Exception as exc:
            add("vram livre", False, str(exc)[:120])

        # 5. Cache SQLite
        if cache is not None:
            try:
                stats = cache.stats()
                add("cache sqlite", True,
                    f"{stats.get('entries', 0)} entradas, {stats.get('db_size_kb', 0)}KB")
            except Exception as exc:
                add("cache sqlite", False, str(exc)[:120])

        all_ok = all(c["ok"] for c in checks)
        return {"all_ok": all_ok, "checks": checks}

    @app.get("/api/telemetry")
    async def api_telemetry():
        """Stats em tempo real pra operador: CPU/RAM/disco/bateria/VRAM/displays.
        Tudo best-effort — falha silenciosa se uma fonte nao tiver."""
        out = {"ts": datetime.now().isoformat()}
        try:
            import psutil
            out["cpu_pct"] = round(psutil.cpu_percent(interval=None), 1)
            out["ram_pct"] = round(psutil.virtual_memory().percent, 1)
            disk = psutil.disk_usage(str(SCRIPT_DIR.parent))
            out["disk_free_gb"] = round(disk.free / (1024**3), 2)
            bat = psutil.sensors_battery()
            if bat is not None:
                out["battery_pct"] = round(bat.percent, 0)
                out["on_ac"] = bool(bat.power_plugged)
        except Exception as exc:
            out["psutil_error"] = str(exc)
        try:
            import torch
            if torch.cuda.is_available():
                free_b, total_b = torch.cuda.mem_get_info()
                out["vram_used_mb"] = round((total_b - free_b) / (1024**2), 0)
                out["vram_free_mb"] = round(free_b / (1024**2), 0)
                out["vram_total_mb"] = round(total_b / (1024**2), 0)
        except Exception:
            pass
        out["displays_connected"] = hub.count()
        if cache is not None:
            try:
                stats = cache.stats()
                out["cache_size"] = stats.get("entries", 0)
                out["cache_session_hits"] = stats.get("session_hits", 0)
            except Exception:
                pass
        return out

    @app.get("/cache/stats")
    async def cache_stats():
        if cache is None:
            return JSONResponse({"error": "cache desabilitado"}, status_code=404)
        return cache.stats()

    @app.get("/cache/top")
    async def cache_top():
        if cache is None:
            return JSONResponse({"error": "cache desabilitado"}, status_code=404)
        return {"top": cache.top_hits(20)}

    @app.websocket("/ws/display")
    async def ws_display(websocket: WebSocket):
        await websocket.accept()
        peer = f"{websocket.client.host}:{websocket.client.port}"
        # Tenta achar IP real (atrás de tunnel/proxy) via X-Forwarded-For
        xff = websocket.headers.get("x-forwarded-for", "")
        client_ip = xff.split(",")[0].strip() if xff else websocket.client.host
        user_agent = websocket.headers.get("user-agent", "")
        await hub.add(websocket, ip=client_ip, ua=user_agent)
        logger.info("[display %s ip=%s] conectado (total=%d)",
                    peer, client_ip, hub.count())
        try:
            await websocket.send_json({"type": "ready"})
            # Manda snapshot da config (modo intérprete, etc) pro display sincronizar
            await websocket.send_json(hub.display_config_snapshot())
            while True:
                # Display so recebe — descartamos qualquer envio
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("[display %s] erro: %s", peer, exc)
        finally:
            await hub.remove(websocket)
            logger.info("[display %s] desconectado (total=%d)", peer, hub.count())

    @app.websocket("/ws")
    async def ws_operator(websocket: WebSocket):
        await websocket.accept()
        peer = f"{websocket.client.host}:{websocket.client.port}"
        logger.info("[operador %s] conectado", peer)

        # Defaults: PT -> ES, modo streaming (tempo real)
        forced_src = "pt"
        forced_tgt = "es"
        backend_name = default_backend
        translator = default_translator
        vad_level = 2
        streaming = default_streaming
        partial_ms = default_partial_ms
        max_ms = default_max_ms
        silence_ms = default_silence_ms
        add_emojis = False  # Default OFF
        filter_profanity = True  # Default ON
        apply_religious = False  # Default OFF — tradução neutra, sem bias denominacional

        vad = webrtcvad.Vad(vad_level)

        def build_accumulator():
            if streaming:
                return StreamingAccumulator(
                    partial_ms=partial_ms,
                    max_ms=max_ms,
                    silence_ms=silence_ms,
                )
            return PhraseDetector(silence_ms=silence_ms)

        accumulator = build_accumulator()

        # Estado pra parciais: lock pra evitar backlog, cache de traducao
        partial_busy = False
        last_translated_src = None
        last_translated_payload = None
        phrase_seq = 0  # incrementa a cada commit final — display usa pra agrupar

        # Incremental translator (delta translation pra parciais)
        incremental = IncrementalTranslator(default_translator)

        # Watchdog stats — só alerta operador se virar padrão (3+ consecutivos)
        consecutive_timeouts = 0
        TIMEOUT_ALERT_THRESHOLD = 3

        # Fonte de audio: 'browser' (default — bytes via WS) ou 'loopback:<id>'/'mic:<id>'
        audio_source = "browser"
        local_capture = None
        loop = asyncio.get_event_loop()

        await websocket.send_json({
            "type": "ready",
            "model": getattr(model, "model_size_or_path", "?"),
            "backend": backend_name,
            "src": forced_src, "tgt": forced_tgt,
            "vad": vad_level,
            "streaming": streaming,
            "partial_ms": partial_ms, "max_ms": max_ms, "silence_ms": silence_ms,
            "add_emojis": add_emojis,
            "audio_source": audio_source,
            # Inclui o estado atual do display_config pro operador
            # sincronizar o UI (checkbox 🎭 Intérprete) com o que o servidor sabe.
            **{f"display_{k}": v for k, v in hub.display_config.items()},
        })

        async def run_event(kind, audio_bytes):
            """Executa transcribe+translate pra um evento ('partial' ou 'final')."""
            nonlocal partial_busy, last_translated_src, last_translated_payload
            nonlocal consecutive_timeouts

            if kind == "partial":
                if partial_busy:
                    return  # skip — anterior ainda processando
                partial_busy = True

            req_id = uuid.uuid4().hex[:8]
            log_ctx = {"request_id": req_id}

            try:
                notice = {"type": "phrase-detected", "kind": kind,
                          "ts": datetime.now().strftime("%H:%M:%S"),
                          "seq": phrase_seq, "request_id": req_id}
                await websocket.send_json(notice)
                if kind == "final":
                    await hub.broadcast(notice)

                # Watchdog — timeout pra evitar pipeline travado em frase OOM/longa.
                # Tunings:
                #   Partials: 15s — pode crescer pra 12s de audio, large-v3 leva 2-4s
                #   Finals: 30s — qualidade, pode demorar mais sem mata o fluxo
                # Só alerta operador se 3+ timeouts consecutivos (padrão real de falha).
                nonlocal_state = {"req_id": req_id}
                timeout_s = 15.0 if kind == "partial" else 30.0
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            transcribe_and_translate,
                            model, translator, audio_bytes,
                            forced_src=forced_src,
                            forced_tgt=forced_tgt,
                            add_emojis=add_emojis,
                            is_partial=(kind == "partial"),
                            incremental=incremental,
                            filter_profanity=filter_profanity,
                            apply_religious=apply_religious,
                        ),
                        timeout=timeout_s,
                    )
                    # Sucesso — zera contador
                    consecutive_timeouts = 0
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    logger.warning("watchdog timeout %s seq=%d after %.1fs (audio=%dKB) consecutive=%d",
                                   kind, phrase_seq, timeout_s, len(audio_bytes)//1024,
                                   consecutive_timeouts, extra=log_ctx)
                    # Só alerta operador se virar padrão
                    if consecutive_timeouts >= TIMEOUT_ALERT_THRESHOLD:
                        await websocket.send_json({
                            "type": "error",
                            "msg": f"⚠ Pipeline lento ({consecutive_timeouts} timeouts seguidos) — VRAM/CPU sobrecarregada",
                        })
                    return
                except Exception as exc:
                    logger.exception("transcribe_and_translate falhou: %s", exc,
                                     extra=log_ctx)
                    await websocket.send_json({"type": "error", "msg": f"⚠ pipeline: {exc}"})
                    return
                if result is None:
                    return

                # Frase bloqueada por filtro de conteudo — NÃO emite no display,
                # só log de auditoria anonimizado.
                if result.get("blocked"):
                    logger.warning(
                        "frase BLOQUEADA (filtro de conteudo) lang=%s matches=%d kind=%s seq=%d",
                        result.get("blocked_lang"), result.get("blocked_count", 0),
                        kind, phrase_seq, extra=log_ctx,
                    )
                    # Notifica APENAS o operador (não o display) que algo foi filtrado
                    await websocket.send_json({
                        "type": "content-filtered",
                        "lang": result.get("blocked_lang"),
                        "kind": kind,
                        "request_id": req_id,
                    })
                    return

                # Cache: se o texto fonte nao mudou desde a ultima vez, reusa
                # a traducao (Whisper as vezes devolve mesma saida em parciais
                # consecutivos sem fala nova suficiente)
                if (result["src_text"] == last_translated_src
                        and last_translated_payload is not None):
                    result["tgt_text"] = last_translated_payload
                else:
                    last_translated_src = result["src_text"]
                    last_translated_payload = result["tgt_text"]

                result["type"] = kind
                result["ts"] = datetime.now().strftime("%H:%M:%S")
                result["seq"] = phrase_seq
                result["request_id"] = req_id
                await websocket.send_json(result)
                await hub.broadcast(result)
                # Log de timing per request (debug perf) — com request_id
                logger.info(
                    "%s seq=%d audio=%.1fs stt=%dms mt=%dms mt_src=%s text=%r",
                    kind, phrase_seq, result["duration_s"],
                    int(result["stt_s"] * 1000), int(result["mt_s"] * 1000),
                    result.get("mt_source", "?"),
                    result["src_text"][:60],
                    extra=log_ctx,
                )
            finally:
                if kind == "partial":
                    partial_busy = False

        async def process_pcm_frame(frame_bytes):
            """Processa um chunk de PCM int16 16kHz mono — vem do browser OU da captura local."""
            nonlocal phrase_seq, last_translated_src, last_translated_payload
            for off in range(0, len(frame_bytes) - (len(frame_bytes) % FRAME_BYTES), FRAME_BYTES):
                chunk = frame_bytes[off:off + FRAME_BYTES]
                evt = accumulator.feed(chunk, vad)
                if evt is None:
                    continue
                if streaming:
                    kind, audio_bytes = evt
                else:
                    kind = "final"
                    audio_bytes = evt
                await run_event(kind, audio_bytes)
                if kind == "final":
                    last_translated_src = None
                    last_translated_payload = None
                    incremental.reset()
                    phrase_seq += 1

        def on_local_pcm(pcm_bytes):
            """Callback chamado da thread de captura local — agenda no event loop."""
            try:
                asyncio.run_coroutine_threadsafe(process_pcm_frame(pcm_bytes), loop)
            except Exception as exc:
                logger.warning("on_local_pcm schedule failed: %s", exc)

        def on_capture_error(msg):
            """Callback da thread de captura local quando ela falha/fica muda.
            Agenda envio de aviso pro operador (e marca status de erro)."""
            async def _notify():
                try:
                    await websocket.send_json({
                        "type": "error",
                        "msg": f"🔊 {msg}",
                        "source": "audio-capture",
                    })
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(_notify(), loop)
            except Exception as exc:
                logger.warning("on_capture_error schedule failed: %s", exc)

        async def set_audio_source(new_source):
            """Troca a fonte de audio. new_source ∈ {browser, loopback[:id], mic[:id]}."""
            nonlocal audio_source, local_capture, accumulator
            # Para captura anterior se houver
            if local_capture is not None:
                try:
                    local_capture.stop()
                except Exception:
                    pass
                local_capture = None
            # Reseta accumulator pra nao misturar bytes antigos
            accumulator = build_accumulator()
            if new_source == "browser":
                audio_source = "browser"
                return
            # Tenta iniciar captura local
            try:
                cap = LocalAudioCapture(new_source, on_local_pcm,
                                        on_error=on_capture_error)
                cap.start()
                local_capture = cap
                audio_source = new_source
            except Exception as exc:
                logger.error("falha iniciar captura local %s: %s", new_source, exc)
                audio_source = "browser"  # rollback
                raise

        try:
            while True:
                msg = await websocket.receive()

                if msg.get("type") == "websocket.disconnect":
                    break

                if "text" in msg and msg["text"] is not None:
                    try:
                        cmd = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        await websocket.send_json({"type": "error", "msg": "json invalido"})
                        continue

                    cmd_type = cmd.get("type")
                    if cmd_type == "config":
                        if "src" in cmd:
                            forced_src = cmd["src"] or None
                        if "tgt" in cmd:
                            forced_tgt = cmd["tgt"] or None
                        new_vad = cmd.get("vad")
                        if isinstance(new_vad, int) and 0 <= new_vad <= 3:
                            vad_level = new_vad
                            vad = webrtcvad.Vad(vad_level)
                        rebuild = False
                        new_streaming = cmd.get("streaming")
                        if isinstance(new_streaming, bool) and new_streaming != streaming:
                            streaming = new_streaming
                            rebuild = True
                        new_silence = cmd.get("silence_ms")
                        if isinstance(new_silence, int) and 100 <= new_silence <= 5000:
                            silence_ms = new_silence
                            rebuild = True
                        new_partial = cmd.get("partial_ms")
                        if isinstance(new_partial, int) and 400 <= new_partial <= 5000:
                            partial_ms = new_partial
                            rebuild = True
                        new_max = cmd.get("max_ms")
                        if isinstance(new_max, int) and 3000 <= new_max <= 30000:
                            max_ms = new_max
                            rebuild = True
                        if rebuild:
                            accumulator = build_accumulator()
                        new_backend = cmd.get("backend")
                        if new_backend and new_backend != backend_name:
                            try:
                                translator = get_translator(new_backend)
                                backend_name = new_backend
                            except Exception as exc:
                                await websocket.send_json({
                                    "type": "error",
                                    "msg": f"backend invalido: {exc}",
                                })
                        if "add_emojis" in cmd and isinstance(cmd["add_emojis"], bool):
                            add_emojis = cmd["add_emojis"]
                        if "filter_profanity" in cmd and isinstance(cmd["filter_profanity"], bool):
                            filter_profanity = cmd["filter_profanity"]
                        if "apply_religious" in cmd and isinstance(cmd["apply_religious"], bool):
                            apply_religious = cmd["apply_religious"]
                        # === Modo Intérprete (afeta o display, não o pipeline) ===
                        # Quando operador toggla, propaga pra todos os displays
                        # conectados. Estado fica no hub (compartilhado).
                        if "interpreter_mode" in cmd and isinstance(cmd["interpreter_mode"], bool):
                            await hub.set_display_config(interpreter_mode=cmd["interpreter_mode"])
                        await websocket.send_json({
                            "type": "config-ack",
                            "src": forced_src, "tgt": forced_tgt,
                            "vad": vad_level, "streaming": streaming,
                            "partial_ms": partial_ms, "max_ms": max_ms,
                            "silence_ms": silence_ms, "backend": backend_name,
                            "add_emojis": add_emojis,
                            "interpreter_mode": hub.display_config.get("interpreter_mode", True),
                        })
                    elif cmd_type == "reset":
                        accumulator = build_accumulator()
                        last_translated_src = None
                        last_translated_payload = None
                        await websocket.send_json({"type": "reset-ack"})
                    elif cmd_type == "clear-display":
                        await hub.broadcast({"type": "clear"})
                        await websocket.send_json({"type": "clear-display-ack",
                                                   "displays": hub.count()})
                    elif cmd_type == "panic":
                        # F1 do operador — escurece display público
                        await hub.broadcast({"type": "panic", "on": bool(cmd.get("on", True))})
                        await websocket.send_json({"type": "panic-ack",
                                                   "on": bool(cmd.get("on", True))})
                    elif cmd_type == "mute":
                        # Ctrl+M — descarta áudio por N ms
                        ms = int(cmd.get("duration_ms", 5000))
                        # Reset accumulator pra zerar frase em curso
                        accumulator = build_accumulator()
                        # Sinaliza pro display também
                        await hub.broadcast({"type": "muted", "duration_ms": ms})
                        await websocket.send_json({"type": "mute-ack", "duration_ms": ms})
                    elif cmd_type == "audio-source":
                        new_source = cmd.get("source", "browser")
                        try:
                            await set_audio_source(new_source)
                            await websocket.send_json({
                                "type": "audio-source-ack",
                                "source": audio_source,
                            })
                        except Exception as exc:
                            await websocket.send_json({
                                "type": "error",
                                "msg": f"falha trocar fonte: {exc}",
                            })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "msg": f"tipo desconhecido: {cmd_type}",
                        })
                    continue

                if "bytes" in msg and msg["bytes"] is not None:
                    # So aceita bytes do browser se source = browser
                    if audio_source != "browser":
                        continue
                    await process_pcm_frame(msg["bytes"])

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.exception("[operador %s] erro: %s", peer, exc)
            try:
                await websocket.send_json({"type": "error", "msg": str(exc)})
            except Exception:
                pass
        finally:
            # Cleanup captura local se estiver rodando
            if local_capture is not None:
                try:
                    local_capture.stop()
                except Exception:
                    pass
            logger.info("[operador %s] desconectado", peer)

    return app


def _detect_cuda():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Servidor de traducao PT<->ES via WebSocket")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Default 0.0.0.0 (LAN inteira ve). Use 127.0.0.1 pra so localhost")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="auto",
                        choices=["auto", "cpu", "cuda"],
                        help="Auto = cuda se disponivel, senao cpu")
    parser.add_argument("--model", default=None,
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Default: 'large-v3' em GPU, 'base' em CPU")
    parser.add_argument("--compute-type", default=None,
                        choices=["int8", "int8_float16", "float16", "bfloat16", "float32"],
                        help="Default: 'float16' em GPU, 'int8' em CPU")
    parser.add_argument("--backend", default="google",
                        choices=["google", "marian", "argos"],
                        help="google=online, marian=local GPU/CPU, argos=offline")
    parser.add_argument("--no-streaming", action="store_true",
                        help="Desliga modo streaming (volta pro modo VAD-only, espera pausa)")
    parser.add_argument("--partial-ms", type=int, default=DEFAULT_STREAM_PARTIAL_MS,
                        help=f"Intervalo entre parciais (default {DEFAULT_STREAM_PARTIAL_MS}ms)")
    parser.add_argument("--max-ms", type=int, default=DEFAULT_STREAM_MAX_MS,
                        help=f"Janela maxima antes de commit forcado (default {DEFAULT_STREAM_MAX_MS}ms)")
    parser.add_argument("--silence-ms", type=int, default=DEFAULT_STREAM_SILENCE_MS,
                        help=f"Silencio pra commit final (default {DEFAULT_STREAM_SILENCE_MS}ms)")
    parser.add_argument("--cache-db", default=None,
                        help="Caminho do SQLite de traducao (default: <scripts>/../data/translations.db). "
                             "Use 'none' pra desabilitar.")
    parser.add_argument("--no-seed", action="store_true",
                        help="Nao popular o cache com dicionario religioso pre-traduzido")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    # Logging — console + RotatingFileHandler em data/tradutor.log
    # (10MB × 5 backups = max 50MB de log, nao enche disco em sermao longo)
    log_dir = SCRIPT_DIR.parent / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tradutor.log"

    class _ReqIdFilter(logging.Filter):
        """Garante que toda mensagem tenha um campo request_id (vazio se ausente)."""
        def filter(self, record):
            if not hasattr(record, "request_id"):
                record.request_id = "-"
            return True

    fmt = "%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s"
    root = logging.getLogger()
    # Limpa handlers antigos (basicConfig anterior, se houver)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(args.log_level.upper())

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(fmt))
    console.addFilter(_ReqIdFilter())
    root.addHandler(console)

    file_h = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_h.setFormatter(logging.Formatter(fmt))
    file_h.addFilter(_ReqIdFilter())
    root.addHandler(file_h)
    logger.info("Log rotativo em %s (10MB × 5 backups)", log_path)

    # Resolucao do device
    if args.device == "auto":
        device = "cuda" if _detect_cuda() else "cpu"
    else:
        device = args.device
    # Defaults por device
    model_name = args.model or ("large-v3" if device == "cuda" else "base")
    compute_type = args.compute_type or ("float16" if device == "cuda" else "int8")

    logger.info("Carregando Whisper '%s' em %s (%s)...",
                model_name, device.upper(), compute_type)
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda":
            logger.error("Falha ao carregar em CUDA: %s", exc)
            logger.warning("Fallback automatico pra CPU com modelo 'base' (int8)")
            device = "cpu"
            model_name = "base"
            compute_type = "int8"
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
        else:
            raise
    model.model_size_or_path = model_name
    # Warmup: 1a chamada paga o JIT do CUDA. Fazemos com silencio pra nao
    # contaminar a fila do user.
    import numpy as _np
    try:
        warm = _np.zeros(16000, dtype=_np.float32)
        for _ in range(2):
            for _s in model.transcribe(warm, language="pt", beam_size=1)[0]:
                _ = _s
        logger.info("Whisper warmup OK.")
    except Exception as exc:
        logger.warning("Warmup falhou (ignorando): %s", exc)
    logger.info("Whisper pronto em %s.", device.upper())

    # SQLite cache de traducao (L2)
    cache = None
    if args.cache_db != "none":
        if args.cache_db is None:
            default_db = SCRIPT_DIR.parent / "data" / "translations.db"
            cache_path = default_db
        else:
            cache_path = args.cache_db
        cache = SQLiteTranslationCache(cache_path)
        before = cache.stats()
        if not args.no_seed:
            from religious import RELIGIOUS_PT_ES_PAIRS
            n = cache.bulk_seed(RELIGIOUS_PT_ES_PAIRS, "pt", "es", source="seed")
            if n > 0:
                logger.info("Cache: %d pares religiosos novos seeded em SQLite", n)
        after = cache.stats()
        logger.info("Cache SQLite: %s (%d entradas, %.1fKB)",
                    cache.db_path, after["entries"], after["db_size_kb"])

    # MarianHub se backend = marian
    marian_hub = None
    if args.backend == "marian":
        marian_data = SCRIPT_DIR.parent / "data"
        marian_hub = MarianHub(marian_data, device=device, compute_type=compute_type)
        available = marian_hub.available_pairs()
        if not available:
            logger.error("Nenhum modelo Marian convertido em %s. "
                         "Rode: python setup_marian.py", marian_data / "marian")
            sys.exit(1)
        logger.info("Marian pares disponiveis: %s", available)

    translator = get_translator(args.backend, sqlite_cache=cache, marian_hub=marian_hub)
    streaming = not args.no_streaming
    logger.info("Backend traducao: %s (com cache: %s)",
                args.backend, "sim" if cache else "nao")
    logger.info("Streaming: %s | partial=%dms max=%dms silence=%dms",
                streaming, args.partial_ms, args.max_ms, args.silence_ms)

    app = make_app(
        model, translator, args.backend,
        args.partial_ms, args.max_ms, args.silence_ms,
        streaming, cache=cache,
    )
    logger.info("Operador:  http://%s:%d/", args.host, args.port)
    logger.info("Display:   http://%s:%d/display", args.host, args.port)
    # ws_ping_interval/timeout: defaults sao 20s/20s, muito apertados quando
    # Whisper large-v3 ocupa GPU + event loop em frase longa. Sobe pra 30s/120s
    # (4x tolerancia) — sermao nao cai mais por "keepalive ping timeout".
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level,
                ws_max_size=16 * 1024 * 1024,
                ws_ping_interval=30, ws_ping_timeout=120)


if __name__ == "__main__":
    main()
