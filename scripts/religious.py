"""Adaptacao de dominio religioso (PT-BR sermao evangelico/pentecostal) -> ES.

Centraliza:
  - INITIAL_PROMPT (framing do Whisper)
  - HOTWORDS (bias do decoder pra vocabulario religioso)
  - normalize_bible_refs_pt() — pre-traducao
  - post_edit_es()           — pos-traducao (livros biblicos + idiomas)
"""

import re
from functools import lru_cache


# ============================================================================
# 1) WHISPER PRIMING
# ============================================================================

# Frase curta de "fake transcript" pra primar estilo, casing e formato de
# referencia biblica. NAO ultrapassar ~150 palavras (Whisper trunca em ~224
# tokens e voce perde o framing).
INITIAL_PROMPT_PT = (
    "Transcricao de pregacao evangelica pentecostal em portugues do Brasil. "
    "O pregador cita versiculos como Joao 3:16, Salmos 23, Romanos 8:28, Atos 2:38. "
    "Fala sobre Jesus, Cristo, Deus, Senhor, Espirito Santo, fe, graca, salvacao, "
    "uncao, milagre, cura, libertacao, batismo, dizimo e oferta. "
    "Usa expressoes como aleluia, gloria a Deus, amem, em nome de Jesus, "
    "bota a mao, recebe o Espirito, levanta essa mao, irmaos e irmas."
)

# Lista de hotwords pra `faster-whisper.transcribe(hotwords=...)`.
# Boost token-level no decoder; nao consome janela de prompt.
# Mantido abaixo de ~200 tokens.
_HOTWORDS_LIST = [
    # Nomes divinos
    "Jesus", "Cristo", "Deus", "Senhor", "Espírito Santo", "Jeová",
    "Yeshua", "Salvador", "Messias", "Cordeiro", "Emanuel", "Altíssimo",
    # Personagens biblicos
    "Maria", "Pedro", "Paulo", "João", "João Batista", "Moisés",
    "Abraão", "Isaque", "Jacó", "Davi", "Salomão", "Elias", "Eliseu",
    "Daniel", "Ester", "Rute", "José", "Judas",
    # Conceitos
    "salvação", "redenção", "arrependimento", "perdão", "graça",
    "misericórdia", "fé", "esperança", "glória", "louvor", "adoração",
    "comunhão", "santificação", "consagração", "avivamento",
    "libertação", "cura", "milagre",
    # Praticas
    "batismo", "dízimo", "oferta", "primícias", "jejum", "vigília",
    "intercessão", "testemunho", "ceia",
    # Cargos
    "pastor", "apóstolo", "profeta", "evangelista", "presbítero",
    "diácono", "obreiro", "ungido", "varão", "serva", "irmão", "irmã",
    # Lugares
    "igreja", "congregação", "tabernáculo", "templo", "altar", "púlpito",
    # Expressoes
    "aleluia", "amém", "glória a Deus", "em nome de Jesus",
    "bota a mão", "levanta essa mão", "recebe o Espírito",
]

HOTWORDS_PT = " ".join(_HOTWORDS_LIST)


# ============================================================================
# 2) NORMALIZACAO DE REFERENCIAS BIBLICAS (PT, ANTES DE TRADUZIR)
# ============================================================================

# Numeros por extenso -> digito (1 a 200). 1-20 manual, resto composicional.
_NUM_BASE = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3,
    "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18,
    "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50,
    "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100,
    "cento": 100,
}


def _word_to_int(word):
    """Converte 'tres' -> 3, 'vinte e um' -> 21, etc. None se nao reconhece."""
    if not word:
        return None
    word = word.lower().strip()
    if word.isdigit():
        return int(word)
    # Composicao "X e Y" (vinte e um, trinta e cinco)
    if " e " in word:
        parts = word.split(" e ")
        if len(parts) == 2:
            a = _NUM_BASE.get(parts[0].strip())
            b = _NUM_BASE.get(parts[1].strip())
            if a is not None and b is not None:
                return a + b
    return _NUM_BASE.get(word)


# Regex dos livros (variantes PT-BR)
_BOOKS_PT = (
    r"Gênesis|Genesis|Êxodo|Exodo|Levítico|Levitico|Números|Numeros|"
    r"Deuteronômio|Deuteronomio|Josué|Josue|Juízes|Juizes|Rute|"
    r"(?:[1-3I-III]\s*)?Samuel|(?:[1-3I-III]\s*)?Reis|"
    r"(?:[1-3I-III]\s*)?Crônicas|Cronicas|Esdras|Neemias|Ester|"
    r"Jó|Job|Salmos?|Provérbios|Proverbios|Eclesiastes|"
    r"Cantares|Cântico\s+dos\s+Cânticos|Cantico\s+dos\s+Canticos|"
    r"Isaías|Isaias|Jeremias|Lamentações|Lamentacoes|Ezequiel|Daniel|"
    r"Oséias|Oseias|Joel|Amós|Amos|Obadias|Jonas|Miquéias|Miqueias|"
    r"Naum|Habacuque|Sofonias|Ageu|Zacarias|Malaquias|"
    r"Mateus|Marcos|Lucas|João|Joao|Atos(?:\s+dos\s+Apóstolos)?|"
    r"Romanos|(?:[1-3I-III]\s*)?Coríntios|(?:[1-3I-III]\s*)?Corintios|"
    r"Gálatas|Galatas|Efésios|Efesios|Filipenses|Colossenses|"
    r"(?:[1-3I-III]\s*)?Tessalonicenses|(?:[1-3I-III]\s*)?Timóteo|"
    r"(?:[1-3I-III]\s*)?Timoteo|Tito|Filemom|Hebreus|Tiago|"
    r"(?:[1-3I-III]\s*)?Pedro|(?:[1-3I-III]\s*)?João|"
    r"(?:[1-3I-III]\s*)?Joao|Judas|Apocalipse"
)

# "Joao tres dezesseis", "Joao 3 16", "Joao 3:16", "Joao capitulo 3 versiculo 16"
_REF_PATTERN = re.compile(
    rf"\b(?P<book>{_BOOKS_PT})\s+"
    r"(?:cap(?:[íi]tulo)?\.?\s+)?"
    r"(?P<ch>(?:\w+(?:\s+e\s+\w+)?)|\d+)"
    r"(?:\s*[:,.]\s*|\s+(?:vers(?:[íi]culo)?s?\.?\s+)?)"
    r"(?P<vs>(?:\w+(?:\s+e\s+\w+)?)|\d+)",
    re.IGNORECASE,
)


def normalize_bible_refs_pt(text):
    """Converte 'Joao tres versiculo dezesseis' -> 'Joao 3:16'.

    Roda ANTES da traducao pra evitar que o Google corrompa numeros."""
    def repl(m):
        book = m.group("book").strip()
        ch_raw = m.group("ch").strip()
        vs_raw = m.group("vs").strip()
        ch = _word_to_int(ch_raw)
        vs = _word_to_int(vs_raw)
        if ch is None or vs is None:
            # Se nao da pra converter um dos dois, deixa como estava
            return m.group(0)
        return f"{book} {ch}:{vs}"

    return _REF_PATTERN.sub(repl, text)


# ============================================================================
# 3) MAPEAMENTO DE LIVROS BIBLICOS PT -> ES (POS-TRADUCAO)
# ============================================================================

# Casos onde Google Translate erra ou deixa em PT
_BIBLE_BOOKS_PT_ES = {
    # As traducoes "obvias" que Google ja acerta nao precisam estar aqui;
    # so as armadilhas conhecidas.
    "Génesis": "Génesis", "Gênesis": "Génesis",
    "Êxodo": "Éxodo", "Exodo": "Éxodo",
    "Juízes": "Jueces", "Juizes": "Jueces",
    "Rute": "Rut",
    "Crônicas": "Crónicas", "Cronicas": "Crónicas",
    "Neemias": "Nehemías",
    "Jó": "Job",
    "Eclesiastes": "Eclesiastés",
    "Cantares": "Cantares",
    "Isaías": "Isaías", "Isaias": "Isaías",
    "Lamentações": "Lamentaciones", "Lamentacoes": "Lamentaciones",
    "Oséias": "Oseas", "Oseias": "Oseas",
    "Obadias": "Abdías",
    "Miquéias": "Miqueas", "Miqueias": "Miqueas",
    "Naum": "Nahúm",
    "Habacuque": "Habacuc",
    "Ageu": "Hageo",
    "Zacarias": "Zacarías",
    "Malaquias": "Malaquías",
    "João": "Juan",   # nome do livro/personagem — atencao com falsos positivos
    "Joao": "Juan",
    "Atos": "Hechos",
    "Atos dos Apóstolos": "Hechos de los Apóstoles",
    "Tiago": "Santiago",
    "Filemom": "Filemón",
    "Hebreus": "Hebreos",
    "Apocalipse": "Apocalipsis",
}

# So aplica swap quando o nome aparece como livro biblico de verdade
# (precedido de numero ou seguido de capítulo). Evita trocar "João" o nome
# proprio sempre.
_BIBLE_SWAP_PATTERNS = []
for pt, es in _BIBLE_BOOKS_PT_ES.items():
    # Trocar quando: vier seguido de digito (capitulo) OU preceddo por "de"+digito
    _BIBLE_SWAP_PATTERNS.append((
        re.compile(rf"\b{re.escape(pt)}(\s+\d)", re.IGNORECASE),
        rf"{es}\1",
    ))


def swap_bible_books_es(text):
    """Aplica trocas de livro biblico no texto ES, so quando seguido de capitulo."""
    for pat, repl in _BIBLE_SWAP_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ============================================================================
# 4) GLOSSARIO PT->ES IDIOMAS EVANGELICOS (POS-TRADUCAO)
# ============================================================================

# Aplicado sobre o texto JA traduzido em ES — corrige erros recorrentes do
# Google Translate em contexto religioso pentecostal.
_GLOSSARY_ES = [
    # (pattern ES erroneo, replacement ES correto, descricao)
    (r"\bponga la mano\b", "imponga las manos"),
    (r"\bponer la mano\b", "imponer las manos"),
    (r"\bpon la mano\b", "impón las manos"),
    (r"\buntado\b", "ungido"),
    (r"\buntada\b", "ungida"),
    (r"\buntura\b", "unción"),
    (r"\bpon fe\b", "ten fe"),
    (r"\bministrar la palabra\b", "predicar la Palabra"),
    (r"\bministrar la Palabra\b", "predicar la Palabra"),
    (r"\btrabajador del Señor\b", "obrero del Señor"),
    (r"\bdiezmo y oferta\b", "diezmo y ofrenda"),
    (r"\boferta del Señor\b", "ofrenda del Señor"),
    (r"\barrepiéntase\b", "arrepiéntete"),
    (r"\bel espíritu\b", "el Espíritu"),
    (r"\bespíritu santo\b", "Espíritu Santo"),
    (r"\bfuego del espíritu\b", "fuego del Espíritu"),
    (r"\bsangre de Jesús\b", "sangre de Jesús"),  # garante capitalizacao
    (r"\bSeñor de los Ejércitos\b", "Jehová de los Ejércitos"),
    (r"\breavivamiento\b", "avivamiento"),
    (r"\bApocalypse\b", "Apocalipsis"),
    (r"\bApocalipse\b", "Apocalipsis"),
    (r"\bhombre de Dios\b", "varón de Dios"),
    # Capitalizar nomes divinos depois de ponto/inicio
    (r"(^|\.\s+)dios\b", r"\1Dios"),
    (r"(^|\.\s+)jesús\b", r"\1Jesús"),
]

_GLOSSARY_COMPILED = [
    (re.compile(pat, re.IGNORECASE) if pat.startswith(r"\b") else re.compile(pat),
     repl)
    for pat, repl in _GLOSSARY_ES
]


def apply_glossary_es(text):
    for pat, repl in _GLOSSARY_COMPILED:
        text = pat.sub(repl, text)
    return text


def capitalize_es(text):
    """Normalização básica: capitaliza primeira letra + primeiras letras após
    ponto. Não é 'predição' — só corrige NLLB que às vezes devolve minúsculo."""
    if not text:
        return text
    text = text.lstrip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    text = re.sub(
        r"([.!?]\s+)([a-záéíóúñ])",
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text


def post_edit_es(text):
    """Pós-edita texto ES com vocabulário religioso (modo religioso ON apenas):
        1) corrige idiomas evangélicos errados (ungido, varón, imponer las manos)
        2) troca nomes de livros bíblicos (Tiago→Santiago, Apocalipse→Apocalipsis)

    Capitalização foi movida pra `capitalize_es()` (sempre aplicada,
    independente de modo religioso).
    """
    text = apply_glossary_es(text)
    text = swap_bible_books_es(text)
    text = capitalize_es(text)
    return text


# ============================================================================
# 5) EMOJIS CONTEXTUAIS — ajuda audiencia a entender o fluxo
# ============================================================================

# Inserido apos a traducao + pos-edicao. Conservador: poucos emojis, so onde
# realmente sinaliza algo (referencia biblica, oracao, louvor).

# Refs biblicas em ES: "Juan 3:16", "Salmos 23" → "📖 Juan 3:16"
_REF_ES_PATTERN = re.compile(
    r"\b((?:Génesis|Éxodo|Levítico|Números|Deuteronomio|Josué|Jueces|Rut|"
    r"(?:[1-3]\s+)?Samuel|(?:[1-3]\s+)?Reyes|(?:[1-3]\s+)?Crónicas|"
    r"Esdras|Nehemías|Ester|Job|Salmos?|Proverbios|Eclesiastés|Cantares|"
    r"Isaías|Jeremías|Lamentaciones|Ezequiel|Daniel|Oseas|Joel|Amós|"
    r"Abdías|Jonás|Miqueas|Nahúm|Habacuc|Sofonías|Hageo|Zacarías|Malaquías|"
    r"Mateo|Marcos|Lucas|Juan|Hechos|Romanos|(?:[1-3]\s+)?Corintios|"
    r"Gálatas|Efesios|Filipenses|Colosenses|(?:[1-3]\s+)?Tesalonicenses|"
    r"(?:[1-3]\s+)?Timoteo|Tito|Filemón|Hebreos|Santiago|"
    r"(?:[1-3]\s+)?Pedro|(?:[1-3]\s+)?Juan|Judas|Apocalipsis)\s+\d+(?::\d+)?)\b",
    re.IGNORECASE,
)

# Palavras-gatilho com seus emojis. Aplicacao com word boundary + nao duplicar
# se ja tem emoji adjacente.
_EMOJI_TRIGGERS = [
    # Oracao
    (re.compile(r"(?<![🙏✨📖🎵👋])\b(Amén|Amen)\b(?![🙏✨📖🎵👋])", re.IGNORECASE), r"\1 🙏"),
    (re.compile(r"(?<![🙏✨📖🎵👋])\b(oremos|oración)\b", re.IGNORECASE), r"🙏 \1"),
    # Louvor / gloria
    (re.compile(r"(?<![🙏✨📖🎵👋])\b(¡?Aleluya!?)\b", re.IGNORECASE), r"\1 ✨"),
    (re.compile(r"(?<![🙏✨📖🎵👋])\b(gloria a Dios)\b", re.IGNORECASE), r"\1 ✨"),
    # Canto
    (re.compile(r"(?<![🙏✨📖🎵👋])\b(cantemos|cantamos|alabanza|himno)\b", re.IGNORECASE), r"🎵 \1"),
    # Saudacao
    (re.compile(r"(?<![🙏✨📖🎵👋])\b(hermanos y hermanas)\b", re.IGNORECASE), r"👋 \1"),
]


def add_emojis_es(text):
    """Adiciona emojis contextuais ao texto ES — sutilmente.

    - 📖 antes de referencias biblicas
    - 🙏 depois de 'Amen', antes de 'oremos/oracion'
    - ✨ depois de 'Aleluya', 'gloria a Dios'
    - 🎵 antes de 'cantemos/alabanza/himno'
    - 👋 antes de 'hermanos y hermanas'
    """
    # Refs biblicas primeiro (poderia confundir com palavras-gatilho)
    text = _REF_ES_PATTERN.sub(r"📖 \1", text)
    # Outros gatilhos
    for pat, repl in _EMOJI_TRIGGERS:
        text = pat.sub(repl, text)
    return text


# ============================================================================
# 6) DICIONARIO PRE-TRADUZIDO PT-BR -> ES (seed do cache SQLite)
# ============================================================================
# Frases recorrentes em sermao pentecostal. Traducoes feitas a mao —
# garante qualidade (Google as vezes erra ungido/imponer/varon).
# Sao o SEED do cache SQLite — na primeira execucao todas estas frases
# viram cache hit instantaneo.

RELIGIOUS_PT_ES_PAIRS = [
    # Bordoes de oracao
    ("amém", "amén"),
    ("amém amém", "amén, amén"),
    ("aleluia", "aleluya"),
    ("glória a Deus", "gloria a Dios"),
    ("graças a Deus", "gracias a Dios"),
    ("Deus seja louvado", "Dios sea alabado"),
    ("em nome de Jesus", "en el nombre de Jesús"),
    ("em nome de Jesus Cristo", "en el nombre de Jesucristo"),
    ("em nome do Pai do Filho e do Espírito Santo",
     "en el nombre del Padre, del Hijo y del Espíritu Santo"),
    ("oremos", "oremos"),
    ("vamos orar", "vamos a orar"),
    ("vamos orar juntos", "vamos a orar juntos"),
    ("oração", "oración"),

    # Frases de chamada e exortacao
    ("levanta as mãos pro céu", "levanta las manos al cielo"),
    ("levantem-se", "levántense"),
    ("levanta essa mão", "levanta esa mano"),
    ("levanta essa mão pra Deus", "levanta esa mano a Dios"),
    ("bota a mão na pessoa do lado",
     "impón las manos sobre la persona a tu lado"),
    ("bota a mão", "impón las manos"),
    ("recebe o Espírito Santo", "recibe el Espíritu Santo"),
    ("recebe a unção", "recibe la unción"),
    ("recebe a benção", "recibe la bendición"),
    ("recebe a cura", "recibe la cura"),
    ("recebe a vitória", "recibe la victoria"),
    ("toma posse", "toma posesión"),

    # Doutrina central
    ("Jesus Cristo é o Senhor", "Jesucristo es el Señor"),
    ("Jesus é o caminho", "Jesús es el camino"),
    ("Jesus é o caminho a verdade e a vida",
     "Jesús es el camino, la verdad y la vida"),
    ("Cristo morreu por nós", "Cristo murió por nosotros"),
    ("Cristo morreu pelos nossos pecados",
     "Cristo murió por nuestros pecados"),
    ("Cristo ressuscitou", "Cristo resucitó"),
    ("ele vive", "él vive"),
    ("Cristo é a esperança", "Cristo es la esperanza"),
    ("a fé move montanhas", "la fe mueve montañas"),
    ("Deus é amor", "Dios es amor"),
    ("Deus é fiel", "Dios es fiel"),
    ("Deus é bom", "Dios es bueno"),
    ("Deus é grande", "Dios es grande"),
    ("Deus é poderoso", "Dios es poderoso"),
    ("o Senhor é meu pastor", "el Señor es mi pastor"),
    ("o Senhor é a minha luz", "el Señor es mi luz"),
    ("a paz do Senhor", "la paz del Señor"),
    ("a paz do Senhor esteja convosco",
     "la paz del Señor sea con vosotros"),
    ("o sangue de Jesus tem poder", "la sangre de Jesús tiene poder"),
    ("pelo sangue de Cristo", "por la sangre de Cristo"),

    # Bibliologia
    ("a Bíblia diz", "la Biblia dice"),
    ("a Palavra de Deus", "la Palabra de Dios"),
    ("a Palavra do Senhor", "la Palabra del Señor"),
    ("a Palavra diz", "la Palabra dice"),
    ("vamos ler a Palavra", "vamos a leer la Palabra"),
    ("abram a Bíblia", "abran la Biblia"),
    ("vamos abrir a Bíblia", "vamos a abrir la Biblia"),
    ("conforme está escrito", "como está escrito"),
    ("diz a Escritura", "dice la Escritura"),

    # Acoes do servico
    ("levantem-se em pé", "pónganse de pie"),
    ("sentem-se", "siéntense"),
    ("vamos cantar", "vamos a cantar"),
    ("cantemos juntos", "cantemos juntos"),
    ("louvor", "alabanza"),

    # Saudacoes
    ("muito obrigado", "muchas gracias"),
    ("muito obrigada", "muchas gracias"),
    ("boa noite", "buenas noches"),
    ("bom dia", "buenos días"),
    ("boa tarde", "buenas tardes"),
    ("paz do Senhor", "paz del Señor"),

    # Audiencia
    ("irmãos", "hermanos"),
    ("irmãs", "hermanas"),
    ("irmãos e irmãs", "hermanos y hermanas"),
    ("queridos irmãos", "amados hermanos"),
    ("irmão", "hermano"),
    ("irmã", "hermana"),
    ("filhos de Deus", "hijos de Dios"),
    ("povo de Deus", "pueblo de Dios"),
    ("crentes", "creyentes"),

    # Conceitos teologicos
    ("o Reino de Deus", "el Reino de Dios"),
    ("o Reino dos Céus", "el Reino de los Cielos"),
    ("vida eterna", "vida eterna"),
    ("a graça de Deus", "la gracia de Dios"),
    ("a misericórdia de Deus", "la misericordia de Dios"),
    ("a presença de Deus", "la presencia de Dios"),
    ("a presença do Espírito", "la presencia del Espíritu"),
    ("o amor de Deus", "el amor de Dios"),
    ("a vontade de Deus", "la voluntad de Dios"),
    ("o propósito de Deus", "el propósito de Dios"),
    ("a glória do Senhor", "la gloria del Señor"),
    ("o nome do Senhor", "el nombre del Señor"),
    ("a face do Senhor", "el rostro del Señor"),

    # Pessoas/lugares
    ("o Espírito Santo", "el Espíritu Santo"),
    ("os anjos do Senhor", "los ángeles del Señor"),
    ("o Deus de Israel", "el Dios de Israel"),
    ("o Deus de Abraão", "el Dios de Abraham"),
    ("o Deus de Abraão de Isaque e de Jacó",
     "el Dios de Abraham, de Isaac y de Jacob"),
    ("o Deus dos exércitos", "Jehová de los Ejércitos"),

    # Apelos/altares
    ("se alguém aqui", "si alguien aquí"),
    ("alguém aqui", "alguien aquí"),
    ("quem aceita Jesus", "quien acepta a Jesús"),
    ("aceita Jesus como seu Senhor", "acepta a Jesús como tu Señor"),
    ("Jesus quer entrar na sua vida", "Jesús quiere entrar en tu vida"),
    ("a salvação é pela fé", "la salvación es por la fe"),
    ("a salvação é pela graça", "la salvación es por la gracia"),

    # Resultados / declaracoes de fe
    ("milagre acontece agora", "el milagro sucede ahora"),
    ("milagre vem", "viene el milagro"),
    ("a vitória é nossa", "la victoria es nuestra"),
    ("a vitória é do Senhor", "la victoria es del Señor"),
    ("nada é impossível para Deus", "nada es imposible para Dios"),
    ("tudo é possível ao que crê", "todo es posible al que cree"),

    # Conexoes
    ("em Cristo Jesus", "en Cristo Jesús"),
    ("pela fé", "por la fe"),
    ("pela graça", "por la gracia"),
    ("pelo poder do Espírito", "por el poder del Espíritu"),
    ("pelo sangue", "por la sangre"),
    ("pelo nome de Jesus", "por el nombre de Jesús"),
]


# ============================================================================
# 7) CACHE DE TRADUCAO (chaveado pelo texto PT pos-normalizacao)
# ============================================================================

def make_cached_translator(translator_fn, maxsize=512):
    """Envolve translator(text, src, tgt) com LRU cache.

    Cache hit em frases repetidas tipo 'em nome de Jesus', 'gloria a Deus',
    'amem' — cortam ~30-50% de chamadas externas em sermao.
    """
    @lru_cache(maxsize=maxsize)
    def cached(text, src, tgt):
        return translator_fn(text, src, tgt)
    cached.cache_info_fn = lambda: cached.cache_info()
    return cached
