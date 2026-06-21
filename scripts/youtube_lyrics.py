"""Busca de músicas no YouTube + download de legenda (letra) via yt-dlp.

Fluxo do "modo música" do tradutor:
  1. search(query)        -> lista de vídeos (id, título, canal, duração)
  2. get_captions(id)     -> letra com tempos (prioriza legenda MANUAL em PT)
  3. (server traduz cada linha pro ES e mostra no display)

Por que legenda em vez de transcrever o canto: vídeos OFICIAIS quase sempre
têm legenda de texto (a letra exata, feita por humano), com timestamps. Isso
pula completamente o problema de "transcrever música cantada" — que é ruim.

Requer yt-dlp no PATH (ou python -m yt_dlp). Tudo via subprocess pra isolar.
"""

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("tradutor.youtube")


def _ytdlp_cmd():
    """Acha o yt-dlp: binário no PATH ou módulo python."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    # fallback: python -m yt_dlp
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_available():
    try:
        r = subprocess.run(_ytdlp_cmd() + ["--version"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _run(args, timeout=90):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _fmt_duration(secs):
    try:
        s = int(float(secs))
    except (ValueError, TypeError):
        return ""
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def search(query, max_results=8):
    """Busca no YouTube. Retorna lista de dicts {id,title,channel,duration,thumbnail}."""
    query = (query or "").strip()
    if not query:
        return []
    args = _ytdlp_cmd() + [
        f"ytsearch{int(max_results)}:{query}",
        "--flat-playlist", "--no-warnings", "--ignore-errors",
        "--print", "%(id)s\t%(title)s\t%(channel)s\t%(duration)s",
    ]
    try:
        r = _run(args, timeout=45)
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp search timeout")
        return []
    out = []
    seen = set()
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        vid = parts[0].strip() if parts else ""
        if not vid or vid in seen or len(vid) < 6:
            continue
        seen.add(vid)
        out.append({
            "id": vid,
            "title": parts[1] if len(parts) > 1 else vid,
            "channel": parts[2] if len(parts) > 2 else "",
            "duration": _fmt_duration(parts[3]) if len(parts) > 3 else "",
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


def _try_download_subs(video_id, sub_langs, auto, tmpdir):
    """Uma tentativa de baixar legenda. Retorna caminho do .srt achado ou None."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    flag = "--write-auto-subs" if auto else "--write-subs"
    args = _ytdlp_cmd() + [
        url, "--skip-download", flag,
        "--sub-langs", sub_langs, "--convert-subs", "srt",
        "--no-warnings", "--ignore-errors",
        "-o", os.path.join(tmpdir, "%(id)s.%(ext)s"),
    ]
    try:
        _run(args, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    files = sorted(glob.glob(os.path.join(tmpdir, "*.srt")))
    return files[0] if files else None


def get_captions(video_id):
    """Baixa a letra do vídeo. Prioridade:
        1. manual PT  (letra exata, melhor)
        2. auto PT    (ASR — pode ter erro em canto)
        3. manual ES  (já em espanhol)
        4. auto ES
    Retorna dict {source, lang, is_spanish, lines:[{start,text}]} ou None.
    """
    attempts = [
        ("manual-pt", "pt.*", False, False),
        ("auto-pt", "pt.*", True, False),
        ("manual-es", "es.*", False, True),
        ("auto-es", "es.*", True, True),
    ]
    for source, langs, auto, is_es in attempts:
        with tempfile.TemporaryDirectory() as td:
            srt = _try_download_subs(video_id, langs, auto, td)
            if not srt:
                continue
            lines = _parse_srt(srt)
            if lines:
                logger.info("legenda achada: %s (%s) %d linhas", video_id, source, len(lines))
                return {
                    "source": source,
                    "is_spanish": is_es,
                    "auto": auto,
                    "lines": lines,
                }
    return None


def _srt_time_to_s(t):
    # formato: HH:MM:SS,mmm
    t = t.strip().replace(",", ".")
    parts = t.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
    except ValueError:
        pass
    return 0.0


def _parse_srt(path):
    """Parseia .srt -> [{start, text}], removendo tags e linhas repetidas
    consecutivas (auto-subs costumam repetir a linha 'rolando')."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    blocks = re.split(r"\n\s*\n", text.strip())
    raw = []
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        timing_idx = None
        for i, l in enumerate(lines):
            if "-->" in l:
                timing_idx = i
                break
        if timing_idx is None:
            continue
        start = _srt_time_to_s(lines[timing_idx].split("-->")[0])
        content = " ".join(lines[timing_idx + 1:]).strip()
        content = re.sub(r"<[^>]+>", "", content)        # tags <c> etc
        content = re.sub(r"\{[^}]*\}", "", content)        # {\an8} etc
        content = re.sub(r"\s+", " ", content).strip()
        if content:
            raw.append({"start": round(start, 2), "text": content})

    # Dedupe: remove repetição consecutiva (case-insensitive) e linhas que
    # são prefixo da próxima (típico de auto-subs rolando).
    out = []
    for item in raw:
        t = item["text"]
        if out:
            prev = out[-1]["text"]
            if t.lower() == prev.lower():
                continue
            if t.lower().startswith(prev.lower()) and len(t) > len(prev):
                # auto-sub crescendo: substitui o anterior pela versão maior
                out[-1] = item
                continue
            if prev.lower().startswith(t.lower()) and len(prev) > len(t):
                continue
        out.append(item)
    return out


if __name__ == "__main__":
    import json
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        print(json.dumps(search(" ".join(sys.argv[2:])), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "captions":
        print(json.dumps(get_captions(sys.argv[2]), indent=2, ensure_ascii=False))
    else:
        print("uso: youtube_lyrics.py search <query> | captions <video_id>")
        print("yt-dlp disponível:", ytdlp_available())
