"""Atualiza o destino de um link curto do short.io via API.

Por que: o tunnel do Cloudflare muda de URL toda vez que reinicia. Mas o link
curto do short.io (ex: te0u7om.s.gy/IglesiaBatistaGracaPaz) é PERMANENTE — a
audiência sempre usa ele. Esse script aponta o link curto pro tunnel atual,
automaticamente, pra ninguém precisar reconfigurar nada.

Config: lê de shortio_config.json (ao lado deste arquivo):
    {
      "api_key": "sk_xxxxxxxx",
      "domain": "te0u7om.s.gy",
      "path": "IglesiaBatistaGracaPaz"
    }
A chave é SECRETA — esse arquivo NÃO deve ser compartilhado nem commitado.

Uso:
    python shortio_update.py "https://novo-tunnel.trycloudflare.com/v2"
    python shortio_update.py --test      (só testa a config/conexão)
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "shortio_config.json"
API_BASE = "https://api.short.io"


def load_config():
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[shortio] config inválida: {exc}", file=sys.stderr)
        return None


def _req(method, url, api_key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", api_key)
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}


def find_link_id(api_key, domain, path):
    """Acha o idString do link pelo domínio + path (slug).
    Lista os domínios pra achar o domain_id, depois lista os links e casa o path.
    (Mais robusto que /links/expand, que retorna 404 em alguns casos.)"""
    doms = _req("GET", f"{API_BASE}/api/domains", api_key)
    domain_id = None
    for d in (doms or []):
        if d.get("hostname") == domain:
            domain_id = d.get("id")
            break
    if not domain_id:
        return None
    path_norm = path.lstrip("/")
    data = _req("GET", f"{API_BASE}/api/links?domain_id={domain_id}&limit=150", api_key)
    links = data.get("links", []) if isinstance(data, dict) else (data or [])
    for l in links:
        if l.get("path") == path_norm:
            return l.get("idString") or l.get("id")
    return None


def update_destination(new_url, config=None):
    """Aponta o link curto configurado pro new_url. Retorna (ok, msg)."""
    config = config or load_config()
    if not config:
        return False, "sem shortio_config.json (não configurado)"
    api_key = config.get("api_key")
    domain = config.get("domain")
    path = config.get("path")
    if not (api_key and domain and path):
        return False, "config incompleta (precisa api_key, domain, path)"

    try:
        link_id = find_link_id(api_key, domain, path)
    except urllib.error.HTTPError as e:
        return False, f"erro achando link ({e.code}): {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        return False, f"erro achando link: {e}"
    if not link_id:
        return False, f"link {domain}/{path} não encontrado"

    try:
        _req("POST", f"{API_BASE}/links/{link_id}", api_key,
             body={"originalURL": new_url})
    except urllib.error.HTTPError as e:
        return False, f"erro atualizando ({e.code}): {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        return False, f"erro atualizando: {e}"

    return True, f"{domain}/{path} -> {new_url}"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = load_config()
    if "--test" in sys.argv:
        if not cfg:
            print("[shortio] NÃO configurado — crie shortio_config.json")
            sys.exit(1)
        try:
            lid = find_link_id(cfg["api_key"], cfg["domain"], cfg["path"])
            print(f"[shortio] OK — link {cfg['domain']}/{cfg['path']} id={lid}")
        except Exception as e:
            print(f"[shortio] falha: {e}")
            sys.exit(1)
        sys.exit(0)
    if len(sys.argv) < 2:
        print("uso: python shortio_update.py <nova_url> | --test")
        sys.exit(1)
    ok, msg = update_destination(sys.argv[1], cfg)
    print(("[shortio] ✓ " if ok else "[shortio] ✗ ") + msg)
    sys.exit(0 if ok else 1)
