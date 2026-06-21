"""Tradutor local NLLB-200 via ctranslate2 — GPU-acelerado.

Modelo: facebook/nllb-200-distilled-600M
  - 600M params, ~600MB em float16 CT2
  - Estado-da-arte open-source pra MT multilingue (200 idiomas)
  - Latencia em GPU float16: ~150-300ms por frase
  - Qualidade muito superior ao opus-mt biblico (que alucinava feio)

Conversão pra CT2 e feita uma vez via `setup_marian.py`. Modelo fica em
<skill>/data/marian/bible-ct2/ (mantemos o nome legado pra compat).
"""

import logging
import threading
from pathlib import Path

logger = logging.getLogger("tradutor.nllb")


NLLB_MODEL_ID = "facebook/nllb-200-distilled-600M"

# Codigos BCP-47 do NLLB. https://huggingface.co/facebook/nllb-200-distilled-600M
LANG_CODES = {
    "pt": "por_Latn",
    "pt-BR": "por_Latn",
    "es": "spa_Latn",
    "en": "eng_Latn",
    "fr": "fra_Latn",
    "it": "ita_Latn",
    "de": "deu_Latn",
    "ca": "cat_Latn",
}


class NLLBTranslator:
    """1 modelo NLLB-200 multilingue carregado em GPU/CPU."""

    def __init__(self, ct2_dir, hf_model_name=NLLB_MODEL_ID,
                 device="cuda", compute_type="float16"):
        import ctranslate2
        from transformers import AutoTokenizer

        self.ct2_dir = Path(ct2_dir)
        self.hf_model_name = hf_model_name
        self.device = device
        self.compute_type = compute_type

        logger.info("Carregando NLLB-200 em %s (%s)...", device, compute_type)
        self.translator = ctranslate2.Translator(
            str(self.ct2_dir),
            device=device,
            compute_type=compute_type,
        )
        # NLLB usa tokenizer "fast" baseado em sentencepiece
        self.tokenizer = AutoTokenizer.from_pretrained(hf_model_name)
        self._lock = threading.Lock()
        logger.info("NLLB-200 pronto.")

    def translate(self, text, src, tgt, *, beam_size=2, max_input_len=512):
        if not text or not text.strip():
            return ""
        text = text.strip()[:1500]

        src_code = LANG_CODES.get(src)
        tgt_code = LANG_CODES.get(tgt)
        if not src_code or not tgt_code:
            raise ValueError(f"Par {src}->{tgt} nao suportado (NLLB)")

        with self._lock:
            # Setar src_lang faz o tokenizer adicionar o token de idioma source
            self.tokenizer.src_lang = src_code
            input_ids = self.tokenizer.encode(text)
            source_tokens = self.tokenizer.convert_ids_to_tokens(input_ids)
            # NLLB exige target_prefix com o codigo de destino
            target_prefix = [tgt_code]

            results = self.translator.translate_batch(
                [source_tokens],
                target_prefix=[target_prefix],
                beam_size=beam_size,
                max_input_length=max_input_len,
                max_decoding_length=min(512, len(source_tokens) * 2 + 50),
                repetition_penalty=1.1,
                no_repeat_ngram_size=4,
            )

        if not results or not results[0].hypotheses:
            return ""
        out_tokens = results[0].hypotheses[0]
        # Skip o prefixo (que e o codigo de target)
        if out_tokens and out_tokens[0] == tgt_code:
            out_tokens = out_tokens[1:]
        out_text = self.tokenizer.decode(
            self.tokenizer.convert_tokens_to_ids(out_tokens),
            skip_special_tokens=True,
        )
        return out_text.strip()


class MarianHub:
    """Wrapper compativel — agora usando NLLB-200 internamente.

    O nome "MarianHub" foi mantido pra nao quebrar imports do server.py.
    """

    def __init__(self, data_root, device="cuda", compute_type="float16"):
        self.data_root = Path(data_root)
        self.device = device
        self.compute_type = compute_type
        self._translator = None
        self._lock = threading.Lock()

    def _ct2_dir(self):
        return self.data_root / "marian" / "bible-ct2"

    def _ensure_loaded(self):
        with self._lock:
            if self._translator is None:
                ct2_dir = self._ct2_dir()
                if not ct2_dir.exists():
                    raise FileNotFoundError(
                        f"Modelo NLLB nao encontrado em {ct2_dir}. "
                        f"Rode: python setup_marian.py"
                    )
                self._translator = NLLBTranslator(
                    ct2_dir, NLLB_MODEL_ID, self.device, self.compute_type
                )
            return self._translator

    def translate(self, text, src, tgt, **kwargs):
        return self._ensure_loaded().translate(text, src, tgt, **kwargs)

    def available_pairs(self):
        if not self._ct2_dir().exists():
            return []
        langs = list(LANG_CODES.keys())
        return [(s, t) for s in langs for t in langs if s != t]

    def get(self, src, tgt):
        return self._ensure_loaded()
