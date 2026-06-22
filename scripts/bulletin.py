#!/usr/bin/env python3
"""Boletim do culto (ordem do culto) — parsing + tradução PT→ES.

O operador digita/cola a ordem do culto num formato simples e tolerante; aqui a
gente transforma em itens estruturados e traduz os RÓTULOS e os VERSÍCULOS pro
espanhol (nomes de cânticos e pessoas ficam no original — são nomes próprios).

Formato aceito (uma coisa por linha, em branco separa):
    @title Igreja Batista Graça e Paz
    @date  Domingo, 21 de junho de 2026

    # Boas-vindas/Oração                      -> seção (cabeçalho)
    " Ele nos resgatou... (Colossenses 1:13)  -> versículo (traduz + referência)
    - Chamada à adoração | Redenção (Projeto Sola)   -> item: rótulo | detalhe
    ## Exposição Bíblica | Pr. Igor Soares    -> título central (com detalhe)
    Aniversariantes                           -> item só com rótulo

Cada item vira um dict:
    {"kind": "section|title|verse|item",
     "label_pt", "label_es",          # rótulo/título (traduzido)
     "detail",                         # detalhe (cânticos/nomes — original)
     "verse_pt", "verse_es",           # texto do versículo (traduzido)
     "ref_pt", "ref_es"}               # referência (livro traduzido)
"""
import re

# ---- Mapa de livros da Bíblia PT -> ES (referências) ----
BIBLE_BOOKS_PT_ES = {
    "gênesis": "Génesis", "genesis": "Génesis",
    "êxodo": "Éxodo", "exodo": "Éxodo",
    "levítico": "Levítico", "levitico": "Levítico",
    "números": "Números", "numeros": "Números",
    "deuteronômio": "Deuteronomio", "deuteronomio": "Deuteronomio",
    "josué": "Josué", "josue": "Josué",
    "juízes": "Jueces", "juizes": "Jueces",
    "rute": "Rut",
    "samuel": "Samuel", "reis": "Reyes", "crônicas": "Crónicas", "cronicas": "Crónicas",
    "esdras": "Esdras", "neemias": "Nehemías", "ester": "Ester",
    "jó": "Job", "jo": "Job",
    "salmos": "Salmos", "salmo": "Salmo",
    "provérbios": "Proverbios", "proverbios": "Proverbios",
    "eclesiastes": "Eclesiastés",
    "cânticos": "Cantares", "canticos": "Cantares", "cantares": "Cantares",
    "isaías": "Isaías", "isaias": "Isaías",
    "jeremias": "Jeremías",
    "lamentações": "Lamentaciones", "lamentacoes": "Lamentaciones",
    "ezequiel": "Ezequiel", "daniel": "Daniel",
    "oséias": "Oseas", "oseias": "Oseas",
    "joel": "Joel", "amós": "Amós", "amos": "Amós",
    "obadias": "Abdías", "jonas": "Jonás", "jonás": "Jonás",
    "miquéias": "Miqueas", "miqueias": "Miqueas",
    "naum": "Nahúm", "habacuque": "Habacuc",
    "sofonias": "Sofonías", "ageu": "Hageo",
    "zacarias": "Zacarías", "malaquias": "Malaquías",
    "mateus": "Mateo", "marcos": "Marcos", "lucas": "Lucas",
    "joão": "Juan", "joao": "Juan",
    "atos": "Hechos", "romanos": "Romanos",
    "coríntios": "Corintios", "corintios": "Corintios",
    "gálatas": "Gálatas", "galatas": "Gálatas",
    "efésios": "Efesios", "efesios": "Efesios",
    "filipenses": "Filipenses",
    "colossenses": "Colosenses",
    "tessalonicenses": "Tesalonicenses",
    "timóteo": "Timoteo", "timoteo": "Timoteo",
    "tito": "Tito", "filemom": "Filemón", "filemon": "Filemón",
    "hebreus": "Hebreos", "tiago": "Santiago",
    "pedro": "Pedro", "judas": "Judas",
    "apocalipse": "Apocalipsis",
}

_REF_RE = re.compile(
    r"^\s*([123]\s+|[IiVv]{1,3}\s+)?([A-Za-zÀ-ÿ\.\s]+?)\s*([\d][\d:.,\-–\s]*)?$"
)


def translate_reference(ref):
    """Traduz a referência bíblica: livro PT->ES, mantém números.
    Ex.: 'Colossenses 1:13,14' -> 'Colosenses 1:13,14'."""
    if not ref:
        return ref
    m = _REF_RE.match(ref.strip())
    if not m:
        return ref
    ordinal = (m.group(1) or "").strip()
    book = (m.group(2) or "").strip()
    loc = (m.group(3) or "").strip()
    book_es = BIBLE_BOOKS_PT_ES.get(book.lower())
    if not book_es:
        return ref  # livro desconhecido: deixa como está
    parts = []
    if ordinal:
        parts.append(re.sub(r"\s+", "", ordinal))  # "1 " -> "1"
    parts.append(book_es)
    out = " ".join(parts)
    if loc:
        out += " " + loc
    return out


def _split_ref(text):
    """Separa 'frase (Ref)' em (frase, ref). Pega o ÚLTIMO parêntese."""
    m = re.search(r"\(([^()]+)\)\s*$", text)
    if m:
        return text[:m.start()].strip().strip('"“”'), m.group(1).strip()
    return text.strip().strip('"“”'), ""


def parse_bulletin_text(raw):
    """Converte o texto do operador em {title, date, items:[...]} (só PT)."""
    title, date = "", ""
    items = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("@title"):
            title = s[6:].strip()
        elif low.startswith("@date"):
            date = s[5:].strip()
        elif s.startswith("##"):
            body = s[2:].strip()
            label, _, detail = body.partition("|")
            items.append({"kind": "title", "label_pt": label.strip(),
                          "detail": detail.strip()})
        elif s.startswith("#"):
            items.append({"kind": "section", "label_pt": s[1:].strip(),
                          "detail": ""})
        elif s.startswith('"') or s.startswith("“") or s.startswith(">"):
            body = s.lstrip('">“ ').strip()
            verse, ref = _split_ref(body)
            items.append({"kind": "verse", "verse_pt": verse, "ref_pt": ref})
        elif s.startswith("-"):
            body = s[1:].strip()
            label, _, detail = body.partition("|")
            items.append({"kind": "item", "label_pt": label.strip(),
                          "detail": detail.strip()})
        else:
            label, _, detail = s.partition("|")
            items.append({"kind": "item", "label_pt": label.strip(),
                          "detail": detail.strip()})
    return {"title": title, "date": date, "items": items}


def translate_bulletin(parsed, translate_fn):
    """Preenche os campos _es usando translate_fn(text)->es. Tolerante a erro."""
    def tr(text):
        text = (text or "").strip()
        if not text:
            return ""
        try:
            return (translate_fn(text) or "").strip() or text
        except Exception:
            return text

    def cap_first(s):
        # Rótulos de itens são títulos -> 1ª letra maiúscula ("oración" -> "Oración")
        return (s[:1].upper() + s[1:]) if s else s

    out_items = []
    for it in parsed.get("items", []):
        kind = it.get("kind")
        new = dict(it)
        if kind in ("section", "title", "item"):
            new["label_es"] = cap_first(tr(it.get("label_pt", "")))
        if kind == "verse":
            new["verse_es"] = tr(it.get("verse_pt", ""))
            new["ref_es"] = translate_reference(it.get("ref_pt", ""))
        out_items.append(new)

    return {
        "title": parsed.get("title", ""),
        "date_pt": parsed.get("date", ""),
        "date_es": tr(parsed.get("date", "")),
        "items": out_items,
    }


if __name__ == "__main__":
    # Teste rápido (sem tradutor real — usa identidade)
    sample = """@title Igreja Batista Graça e Paz
@date Domingo, 21 de junho de 2026

# Boas-vindas/Oração
" Ele nos resgatou do poder das trevas (Colossenses 1:13,14)
- Chamada à adoração | Redenção (Projeto Sola)
## Exposição Bíblica | Pr. Igor Soares
" o mesmo sentimento de Cristo Jesus (Filipenses 2:5-11)
Aniversariantes
"""
    import json as _json
    parsed = parse_bulletin_text(sample)
    res = translate_bulletin(parsed, lambda t: t)
    print(_json.dumps(res, indent=2, ensure_ascii=False))
    print("\nRef test:", translate_reference("Colossenses 1:13,14"),
          "|", translate_reference("1 Coríntios 13:4-7"),
          "|", translate_reference("Deuteronômio 16:17"))
