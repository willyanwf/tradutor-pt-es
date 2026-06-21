"""Filtro de palavrões / obscenidades pra ambiente religioso.

Estratégia: descarta a FRASE INTEIRA se detectar termo proibido em PT ou ES.
Não vai pro display nem ao log público — só uma entrada anonimizada no
log do server pra auditoria.

Cobre:
  - PT-BR e PT-PT (gírias regionais)
  - ES (Espanha + Latino-América)
  - Variantes morfológicas (foda/foder/fodase) via word_boundary + word-chars

NÃO cobre (intencionalmente):
  - "duplo sentido" puro sem palavra explícita (impossível sem LLM)
  - palavras médicas legítimas em contexto bíblico ("circuncisão", etc)
  - tabu religioso (blasfêmia) — pode adicionar via blocklist customizada
"""

import re


# === PT-BR e PT-PT — termos diretos. Word boundary + \w* cobre flexões. ===
PROFANITY_PT = {
    # Sexuais / palavrões fortes
    "caralho", "carai", "carái",
    "porra",
    "foda", "foder", "fodase", "fudido", "fudida", "fudendo",
    "buceta", "boceta", "xereca", "xoxota", "xota",
    "pinto", "pica", "pirola", "piroca", "rola", "cacete",
    "pau",  # ambíguo, mas "pau no cu" / "tomar no pau"
    "cu", "cuzão", "cuzao", "cuzinho",
    "bunda", "bundão", "bundona",
    "tezão", "tesao", "tesão",
    "punheta", "punheteiro",
    "siririca",
    "boquete", "broxa",
    "putaria", "putinha", "putinho",
    # Insultos / xingamentos
    "merda", "merdinha",
    "bosta",
    "fdp", "filhodaputa",
    "viado", "veado",  # pejorativo
    "bicha",
    "cuzeiro",
    "vagabunda", "vagabundo",
    "puta", "putona",
    "vadia",
    # Combinações comuns
    "pqp", "vsf", "vtnc",
    "puta merda",
    "puta que pariu",
    "vai se fuder", "vai se foder", "vai tomar no cu",
}

# === ES — Espanha + Latino-América (México, Argentina, Colômbia, etc) ===
PROFANITY_ES = {
    # Sexuais / fortes
    "mierda",
    "joder", "jodido", "jodida", "jodete",
    "coño", "cono",
    "polla", "pollas",
    "verga", "verguero",
    "pija",  # Argentina/Cuba
    "concha", "conchudo",  # Argentina/Uruguay (vulgar)
    "chocho", "chucha", "panocha",
    "culo", "culero",  # culero é forte
    "follar",
    "chingar", "chingada", "chingón",  # México
    "cojer", "cogida", "cogiendo",     # Lat-Am vulgar
    "mamada", "mamar",
    "pajero", "pajearse",
    "puta", "putada", "putita", "putero",
    "puto",  # ambíguo: pode ser xingamento
    "cabrón", "cabron", "cabrona",
    "pendejo", "pendeja",  # México
    "gilipollas", "gilipuertas",
    "huevón", "huevon", "weón", "weon",  # Chile/Mex/Arg
    "boludo", "boluda", "pelotudo",      # Argentina
    "carajo",
    "hostia", "hostias",  # Espanha
    "joputa", "hijoputa", "hijodeputa",
    "maricón", "maricon", "marica",
    # Insultos comuns
    "imbécil", "imbecil",  # leve, mas pode filtrar
    "estúpido",            # leve
}


def _build_regex(words):
    r"""Constrói regex com word boundary + \w* pra pegar flexões.

    Ex: 'foda' captura 'fodase', 'foder', 'fodido', etc.
    """
    # Ordena por tamanho decrescente pra priorizar matches longos
    sorted_words = sorted(words, key=len, reverse=True)
    escaped = [re.escape(w) for w in sorted_words]
    pattern = r"\b(" + "|".join(escaped) + r")\w*\b"
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


_PROFANITY_PT_RE = _build_regex(PROFANITY_PT)
_PROFANITY_ES_RE = _build_regex(PROFANITY_ES)


def is_obscene(text, lang):
    """True se o texto contém palavrão na lista da língua dada."""
    if not text:
        return False
    if lang == "pt" or lang == "pt-BR":
        return bool(_PROFANITY_PT_RE.search(text))
    if lang == "es":
        return bool(_PROFANITY_ES_RE.search(text))
    return False


def find_obscene(text, lang):
    """Retorna lista de matches (debug only) — usado pelo log de auditoria."""
    if not text:
        return []
    pat = _PROFANITY_PT_RE if lang.startswith("pt") else _PROFANITY_ES_RE
    return [m.group(0) for m in pat.finditer(text)]


def censor(text, lang, replacement="[...]"):
    """Substitui palavrões por replacement. Útil se quiser mascarar
    em vez de descartar (não usado por padrão — fica como opção)."""
    if not text:
        return text
    pat = _PROFANITY_PT_RE if lang.startswith("pt") else _PROFANITY_ES_RE
    return pat.sub(replacement, text)


if __name__ == "__main__":
    # Smoke test
    tests = [
        ("Hoje vamos falar sobre o amor de Deus", "pt", False),
        ("Que merda", "pt", True),
        ("Vai tomar no cu", "pt", True),
        ("Jesus é o caminho", "pt", False),
        ("Hoy hablaremos del amor", "es", False),
        ("Qué mierda es esto", "es", True),
        ("Ese cabrón", "es", True),
    ]
    for text, lang, expected in tests:
        got = is_obscene(text, lang)
        ok = "OK" if got == expected else "FAIL"
        print(f"[{ok}] is_obscene({text!r}, {lang!r}) = {got} (esperado {expected})")
