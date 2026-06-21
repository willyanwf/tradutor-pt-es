"""Cache persistente de traducoes em SQLite.

Camada L2 (entre LRU em memoria e Google Translate).
Em sermoes o pastor repete muito ('amem', 'em nome de Jesus', 'gloria a Deus',
'irmaos e irmas', 'a Palavra de Deus'). Cada uma dessas frases hita o cache
e economiza ~200-400ms vs chamada ao Google.

Schema:
  translations (
    key TEXT PRIMARY KEY,           -- '<src>><tgt>:<text_normalized>'
    text_src TEXT NOT NULL,
    text_tgt TEXT NOT NULL,
    lang_src TEXT NOT NULL,
    lang_tgt TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hit_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'auto'      -- 'seed' (dicionario), 'auto' (do Google)
  )
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("tradutor.cache")


class SQLiteTranslationCache:
    """Cache thread-safe por SQLite. Cada thread tem sua conexao via local."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS translations (
            key        TEXT PRIMARY KEY,
            text_src   TEXT NOT NULL,
            text_tgt   TEXT NOT NULL,
            lang_src   TEXT NOT NULL,
            lang_tgt   TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hit_count  INTEGER DEFAULT 0,
            source     TEXT DEFAULT 'auto'
        );
        CREATE INDEX IF NOT EXISTS idx_langs ON translations(lang_src, lang_tgt);
        CREATE INDEX IF NOT EXISTS idx_hits  ON translations(hit_count DESC);
    """

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._hit_count = 0
        self._miss_count = 0
        self._init_schema()

    def _conn(self):
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,   # autocommit
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        self._conn().executescript(self.SCHEMA)

    @staticmethod
    def _normalize_key(text, src, tgt):
        # Dedup: lower + collapse spaces + strip pontuacao final
        norm = " ".join(text.lower().strip().split())
        norm = norm.rstrip(".,!?;:¿¡")
        return f"{src}>{tgt}:{norm}"

    def get(self, text, src, tgt):
        if not text or not text.strip():
            return None
        key = self._normalize_key(text, src, tgt)
        try:
            cur = self._conn().execute(
                "SELECT text_tgt FROM translations WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
        except Exception as exc:
            logger.warning("cache get error: %s", exc)
            return None
        if row:
            self._hit_count += 1
            # Hit count update best-effort (nao bloqueia)
            try:
                self._conn().execute(
                    "UPDATE translations SET hit_count = hit_count + 1 WHERE key = ?",
                    (key,),
                )
            except Exception:
                pass
            return row[0]
        self._miss_count += 1
        return None

    def put(self, text_src, text_tgt, src, tgt, source="auto"):
        if not text_src or not text_tgt:
            return
        if text_tgt.startswith("[erro"):
            return
        key = self._normalize_key(text_src, src, tgt)
        try:
            self._conn().execute(
                "INSERT OR IGNORE INTO translations "
                "(key, text_src, text_tgt, lang_src, lang_tgt, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, text_src, text_tgt, src, tgt, source),
            )
        except Exception as exc:
            logger.warning("cache put error: %s", exc)

    def bulk_seed(self, pairs, src, tgt, source="seed"):
        """pairs: iteravel de (text_src, text_tgt). INSERT OR IGNORE — nao
        sobrescreve entradas existentes (importante: se o usuario corrigiu
        manualmente, preserva)."""
        conn = self._conn()
        inserted = 0
        for ts, tt in pairs:
            if not ts or not tt:
                continue
            key = self._normalize_key(ts, src, tgt)
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO translations "
                    "(key, text_src, text_tgt, lang_src, lang_tgt, source) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, ts, tt, src, tgt, source),
                )
                if cur.rowcount > 0:
                    inserted += 1
            except Exception as exc:
                logger.warning("cache seed error: %s", exc)
        return inserted

    def stats(self):
        try:
            cur = self._conn().execute(
                "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), "
                "       COUNT(*) FILTER (WHERE source = 'seed'), "
                "       COUNT(*) FILTER (WHERE source = 'auto') "
                "FROM translations"
            )
            row = cur.fetchone()
            total, total_hits, seeded, autos = row
        except Exception:
            total, total_hits, seeded, autos = 0, 0, 0, 0
        return {
            "entries": total,
            "seeded": seeded,
            "auto": autos,
            "total_hits": total_hits,
            "session_hits": self._hit_count,
            "session_misses": self._miss_count,
            "session_hit_rate": (
                round(self._hit_count / max(1, self._hit_count + self._miss_count), 3)
            ),
            "db_path": str(self.db_path),
            "db_size_kb": (
                round(self.db_path.stat().st_size / 1024, 1)
                if self.db_path.exists() else 0
            ),
        }

    def top_hits(self, limit=20):
        cur = self._conn().execute(
            "SELECT text_src, text_tgt, hit_count FROM translations "
            "WHERE hit_count > 0 ORDER BY hit_count DESC LIMIT ?",
            (limit,),
        )
        return [{"src": r[0], "tgt": r[1], "hits": r[2]} for r in cur.fetchall()]


class IncrementalTranslator:
    """Reduz chamadas ao backend traduzindo SOMENTE o delta entre parciais.

    Caso tipico em streaming Whisper — parciais sao versoes crescentes da
    mesma frase em construcao:
        p1: "Hoje vamos"
        p2: "Hoje vamos falar"
        p3: "Hoje vamos falar sobre o amor"

    Sem essa otimizacao, cada parcial chama Google com a frase inteira (caro).
    Com ela, so o pedaco novo passa pelo Google e e concatenado.

    Tradeoff: a traducao incremental pode nao ser 100% idiomatica (o backend
    nao reve o inicio com o novo contexto), mas pra streaming live e
    aceitavel — quando o FINAL chega, e re-traduzido por inteiro com qualidade.

    Estado por instancia — crie 1 por conexao WebSocket.
    """

    def __init__(self, base_translator,
                 min_overlap_chars=8, min_delta_words=2,
                 enabled_for=(("pt", "es"), ("es", "pt"))):
        self.base = base_translator
        self.min_overlap_chars = min_overlap_chars
        self.min_delta_words = min_delta_words
        self.enabled_for = set(enabled_for)
        self.last_src = ""
        self.last_tgt = ""
        # Stats
        self.delta_hits = 0
        self.full_calls = 0

    def reset(self):
        """Chamar entre frases (apos cada final)."""
        self.last_src = ""
        self.last_tgt = ""

    def translate(self, text, src, tgt, *, force_full=False):
        """Retorna (texto_traduzido, source).

        source ∈ {'full', 'delta', 'pass'}.
        - 'full' = base traduziu frase inteira
        - 'delta' = base traduziu so o pedaco novo, foi concatenado
        - 'pass' = texto vazio
        """
        if not text or not text.strip():
            return text, "pass"

        # Force full pra finais (qualidade)
        if force_full or (src, tgt) not in self.enabled_for:
            result = self.base(text, src, tgt)
            self.last_src = text
            self.last_tgt = result
            self.full_calls += 1
            return result, "full"

        # E extensao do ultimo?
        if self.last_src:
            tn = text.strip()
            ln = self.last_src.strip()
            if (len(tn) > len(ln) + self.min_overlap_chars
                    and tn.lower().startswith(ln.lower())):
                delta_pt = tn[len(ln):].strip()
                delta_words = delta_pt.split()
                if len(delta_words) >= self.min_delta_words:
                    try:
                        delta_es = self.base(delta_pt, src, tgt)
                        if delta_es and not (isinstance(delta_es, str)
                                             and delta_es.startswith("[erro")):
                            # Junta — tomando cuidado com pontuacao final
                            sep = " "
                            if self.last_tgt.endswith((",", ";")):
                                sep = " "
                            result = self.last_tgt.rstrip(".") + sep + delta_es
                            self.last_src = text
                            self.last_tgt = result
                            self.delta_hits += 1
                            return result, "delta"
                    except Exception:
                        pass

        # Fallback: traduz frase inteira
        result = self.base(text, src, tgt)
        self.last_src = text
        self.last_tgt = result
        self.full_calls += 1
        return result, "full"

    def stats(self):
        total = self.delta_hits + self.full_calls
        return {
            "delta_hits": self.delta_hits,
            "full_calls": self.full_calls,
            "delta_rate": round(self.delta_hits / max(1, total), 3),
        }
