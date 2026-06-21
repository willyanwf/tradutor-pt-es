"""Pipeline reutilizavel: VAD, deteccao de frase, Whisper, tradutor.

Usado pelo CLI (translate_live.py) e pelo server WebSocket (server.py).
"""

import collections
import time

import numpy as np


SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)
FRAME_BYTES = FRAME_SIZE * 2  # int16 = 2 bytes/sample

DEFAULT_SILENCE_MS = 600        # silencio pra fechar uma frase
DEFAULT_MIN_PHRASE_MS = 300     # minimo de voz pra considerar frase util
DEFAULT_MAX_PHRASE_MS = 15000   # corta forcado em frases muito longas
DEFAULT_PREROLL_MS = 300        # audio capturado antes do gatilho de fala

# Streaming (modo "tempo real" - retranscreve janela rolante)
DEFAULT_STREAM_PARTIAL_MS = 1200    # intervalo entre parciais
DEFAULT_STREAM_MAX_MS = 12000       # janela maxima antes de commit forcado
DEFAULT_STREAM_SILENCE_MS = 800     # silencio pra commit final
DEFAULT_STREAM_MIN_AUDIO_MS = 800   # nao transcrever buffers menores que isso


def _ms_to_frames(ms):
    return max(1, int(round(ms / FRAME_DURATION_MS)))


class PhraseDetector:
    """Maquina de estado pra agrupar frames de audio em frases via VAD.

    `feed(frame_bytes, vad)` recebe 1 frame de 30ms (960 bytes int16).
    Retorna `None` se a frase ainda nao terminou, ou os bytes concatenados
    da frase completa quando detecta silencio prolongado apos fala.

    Parametros em ms (convertidos pra frames internamente):
    - silence_ms: silencio que fecha a frase (default 600ms)
    - min_phrase_ms: minimo de voz pra valer (default 300ms)
    - max_phrase_ms: teto antes de cortar forcado (default 15s)
    - preroll_ms: audio antes do gatilho (default 300ms)
    """

    def __init__(self,
                 silence_ms=DEFAULT_SILENCE_MS,
                 min_phrase_ms=DEFAULT_MIN_PHRASE_MS,
                 max_phrase_ms=DEFAULT_MAX_PHRASE_MS,
                 preroll_ms=DEFAULT_PREROLL_MS):
        self.silence_threshold = _ms_to_frames(silence_ms)
        self.min_phrase = _ms_to_frames(min_phrase_ms)
        self.max_phrase = _ms_to_frames(max_phrase_ms)
        self.preroll = _ms_to_frames(preroll_ms)

        self.triggered = False
        self.voiced_frames = []
        self.ring_buffer = collections.deque(maxlen=self.preroll)
        self.silence_count = 0

    def feed(self, frame_bytes, vad):
        if len(frame_bytes) != FRAME_BYTES:
            return None

        is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)

        if not self.triggered:
            self.ring_buffer.append((frame_bytes, is_speech))
            num_voiced = sum(1 for _, s in self.ring_buffer if s)
            if num_voiced > 0.7 * self.ring_buffer.maxlen:
                self.triggered = True
                self.voiced_frames.extend(f for f, _ in self.ring_buffer)
                self.ring_buffer.clear()
                self.silence_count = 0
            return None

        self.voiced_frames.append(frame_bytes)
        if is_speech:
            self.silence_count = 0
        else:
            self.silence_count += 1
            if self.silence_count >= self.silence_threshold:
                phrase = b"".join(self.voiced_frames) if len(self.voiced_frames) >= self.min_phrase else None
                self.voiced_frames = []
                self.triggered = False
                self.silence_count = 0
                return phrase

        if len(self.voiced_frames) >= self.max_phrase:
            phrase = b"".join(self.voiced_frames)
            self.voiced_frames = []
            self.triggered = False
            self.silence_count = 0
            return phrase

        return None


class StreamingAccumulator:
    """Acumula audio com VAD e gera eventos pra streaming em tempo real.

    `feed(frame_bytes, vad)` retorna:
      None                              - nada a fazer
      ('partial', audio_bytes)          - retranscrever o buffer (frase em curso)
      ('final',   audio_bytes)          - silencio detectado ou buffer cheio,
                                          frase fechada
    """

    def __init__(self,
                 partial_ms=DEFAULT_STREAM_PARTIAL_MS,
                 max_ms=DEFAULT_STREAM_MAX_MS,
                 silence_ms=DEFAULT_STREAM_SILENCE_MS,
                 min_audio_ms=DEFAULT_STREAM_MIN_AUDIO_MS,
                 preroll_ms=DEFAULT_PREROLL_MS):
        self.partial_frames = _ms_to_frames(partial_ms)
        self.max_frames = _ms_to_frames(max_ms)
        self.silence_frames = _ms_to_frames(silence_ms)
        self.min_audio_frames = _ms_to_frames(min_audio_ms)
        self.preroll_frames = _ms_to_frames(preroll_ms)

        self._reset()
        self.preroll = collections.deque(maxlen=self.preroll_frames)

    def _reset(self):
        self.buffer = []
        self.has_speech = False
        self.silence_count = 0
        self.frames_since_partial = 0

    def feed(self, frame_bytes, vad):
        if len(frame_bytes) != FRAME_BYTES:
            return None

        is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)

        if not self.has_speech:
            # Pre-roll antes do gatilho — guarda contexto curto pra incluir
            # o inicio da palavra que disparou o VAD
            self.preroll.append(frame_bytes)
            if is_speech:
                self.has_speech = True
                self.buffer.extend(self.preroll)
                self.preroll.clear()
                self.silence_count = 0
                self.frames_since_partial = 0
            return None

        # Ja estamos em fala
        self.buffer.append(frame_bytes)
        self.frames_since_partial += 1

        if is_speech:
            self.silence_count = 0
        else:
            self.silence_count += 1

        # Commit final por silencio
        if self.silence_count >= self.silence_frames:
            audio = b"".join(self.buffer)
            audio_frames = len(self.buffer)
            self._reset()
            if audio_frames >= self.min_audio_frames:
                return ("final", audio)
            return None

        # Commit final por buffer cheio
        if len(self.buffer) >= self.max_frames:
            audio = b"".join(self.buffer)
            self._reset()
            return ("final", audio)

        # Parcial
        if (self.frames_since_partial >= self.partial_frames
                and len(self.buffer) >= self.min_audio_frames):
            self.frames_since_partial = 0
            return ("partial", b"".join(self.buffer))

        return None


def pcm_bytes_to_float(pcm_bytes):
    """Converte bytes int16 -> ndarray float32 [-1, 1] pro Whisper."""
    pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def get_translator(backend, sqlite_cache=None, marian_hub=None):
    """Retorna callable translate(text, src, tgt) -> str.

    Pipeline em camadas:
        L1: LRU em memoria (instantaneo, max 512 entradas, scope = run atual)
        L2: SQLite persistente (~0.5ms, scope = vida da skill, milhares de entradas)
        L3: backend real
              Google ~200-400ms (rede)
              Marian ~30-50ms em GPU (local)
              Argos  ~30-80ms (local, qualidade meh)

    Em sermao, repeticoes ('amem', 'gloria a Deus', 'em nome de Jesus',
    'irmaos e irmas') hitam L1 ou L2 e cortam ~95% da latencia.
    """
    from religious import make_cached_translator

    if backend == "google":
        from deep_translator import GoogleTranslator

        def real_translate(text, src, tgt):
            return GoogleTranslator(source=src, target=tgt).translate(text)

    elif backend == "marian":
        if marian_hub is None:
            raise ValueError("Backend 'marian' requer marian_hub != None")

        def real_translate(text, src, tgt):
            return marian_hub.translate(text, src, tgt)

    elif backend == "argos":
        import argostranslate.translate as at

        def real_translate(text, src, tgt):
            return at.translate(text, src, tgt)
    else:
        raise ValueError(f"Backend desconhecido: {backend}")

    if sqlite_cache is None:
        # So L1 (LRU) por cima do backend real
        return make_cached_translator(real_translate)

    def cached_through_sqlite(text, src, tgt):
        if not text or not text.strip():
            return text
        hit = sqlite_cache.get(text, src, tgt)
        if hit is not None:
            return hit
        result = real_translate(text, src, tgt)
        if result and not (isinstance(result, str) and result.startswith("[erro")):
            sqlite_cache.put(text, result, src, tgt, source="auto")
        return result

    # L1 (LRU) por cima do L2+L3 combinados
    return make_cached_translator(cached_through_sqlite)


def pick_target(src_lang, forced_tgt=None):
    if forced_tgt:
        return forced_tgt
    if src_lang == "pt":
        return "es"
    if src_lang == "es":
        return "pt"
    return "pt"


def transcribe_and_translate(model, translator, pcm_bytes, *,
                             forced_src=None, forced_tgt=None,
                             apply_religious=False, add_emojis=False,
                             is_partial=False,
                             incremental=None,
                             filter_profanity=True):
    """Processa 1 frase: Whisper + traducao + (opcional) pos-edicao religiosa.

    Otimizacao de latencia:
      - is_partial=True: beam_size=1 (greedy, ~3x mais rapido)
      - is_partial=False (final): beam_size=5 (mais estavel pra commit)
      Em ambos: condition_on_previous_text=False, temperature=0.0,
                vad_filter=True (anti-aluc), hotwords + initial_prompt.
    """
    audio_f32 = pcm_bytes_to_float(pcm_bytes)
    duration_s = float(len(audio_f32)) / SAMPLE_RATE

    if apply_religious:
        from religious import (
            HOTWORDS_PT, INITIAL_PROMPT_PT,
            normalize_bible_refs_pt, post_edit_es, add_emojis_es,
        )
        hotwords = HOTWORDS_PT
        initial_prompt = INITIAL_PROMPT_PT
    else:
        hotwords = None
        initial_prompt = None

    # Em parciais, prioriza velocidade. Em finais, qualidade.
    beam_size = 1 if is_partial else 5

    t0 = time.monotonic()
    try:
        segments, info = model.transcribe(
            audio_f32,
            language=forced_src,
            beam_size=beam_size,
            temperature=0.0,
            repetition_penalty=1.1,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            hallucination_silence_threshold=2.0,
            hotwords=hotwords,
            initial_prompt=initial_prompt,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            word_timestamps=False,   # nao usado, gasta ~30% de CPU
        )
    except TypeError:
        segments, info = model.transcribe(
            audio_f32,
            language=forced_src,
            beam_size=beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            initial_prompt=initial_prompt,
            vad_filter=True,
        )

    text = " ".join(s.text.strip() for s in segments).strip()
    stt_s = time.monotonic() - t0

    if not text:
        return None

    src_lang = info.language
    tgt_lang = pick_target(src_lang, forced_tgt)

    # Filtro de conteudo (PT) — bloqueia frase no source ja
    if filter_profanity:
        try:
            from profanity import is_obscene, find_obscene
            if is_obscene(text, src_lang):
                # Marker especial pro server logar sem expor a palavra
                return {
                    "blocked": True,
                    "blocked_lang": src_lang,
                    "blocked_count": len(find_obscene(text, src_lang)),
                    "duration_s": duration_s,
                    "stt_s": stt_s,
                    "is_partial": is_partial,
                }
        except ImportError:
            pass

    # Pre-traducao PT: normaliza refs biblicas
    src_text_normalized = text
    if apply_religious and src_lang == "pt":
        src_text_normalized = normalize_bible_refs_pt(text)

    t1 = time.monotonic()
    mt_source = "full"
    try:
        if incremental is not None:
            # Em parciais: tenta delta translation. Em finais: forca frase inteira.
            translation, mt_source = incremental.translate(
                src_text_normalized, src_lang, tgt_lang,
                force_full=not is_partial,
            )
        else:
            translation = translator(src_text_normalized, src_lang, tgt_lang)
    except Exception as exc:
        translation = f"[erro traducao: {exc}]"
        mt_source = "error"
    mt_s = time.monotonic() - t1

    # Pos-traducao ES:
    #   - capitalize SEMPRE (normalizacao, nao e "predicao")
    #   - glossario evangelico + livros biblicos + emojis SO se modo religioso ON
    if tgt_lang == "es" and not translation.startswith("[erro"):
        from religious import capitalize_es
        translation = capitalize_es(translation)
        if apply_religious:
            translation = post_edit_es(translation)
            if add_emojis:
                translation = add_emojis_es(translation)

    # Filtro de conteudo (target) — caso PT tenha passado mas NLLB gerou palavrao
    if filter_profanity and not translation.startswith("[erro"):
        try:
            from profanity import is_obscene, find_obscene
            if is_obscene(translation, tgt_lang):
                return {
                    "blocked": True,
                    "blocked_lang": tgt_lang,
                    "blocked_count": len(find_obscene(translation, tgt_lang)),
                    "duration_s": duration_s,
                    "stt_s": stt_s,
                    "mt_s": mt_s,
                    "is_partial": is_partial,
                }
        except ImportError:
            pass

    return {
        "src_lang": src_lang,
        "tgt_lang": tgt_lang,
        "src_text": src_text_normalized,
        "src_raw": text,
        "tgt_text": translation,
        "duration_s": duration_s,
        "stt_s": stt_s,
        "mt_s": mt_s,
        "mt_source": mt_source,
        "is_partial": is_partial,
    }
