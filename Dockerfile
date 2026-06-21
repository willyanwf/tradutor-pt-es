# ============================================================
# Tradutor PT->ES — imagem Docker (modo SERVIDOR, sem GPU)
#
# Roda o server.py que recebe áudio por WebSocket (do church_sender
# ou do navegador), transcreve (Whisper small) e traduz (NLLB/Marian),
# e serve a tela /v2 pra audiência. Porta 3080.
#
# OBS: modo CPU. Whisper large-v3 precisa de GPU; aqui usamos 'small'
# (int8), que roda em CPU mas é mais lento (~2-3s por frase).
#
# Build:   docker build -t tradutor .
# Run:     docker compose up -d   (ver docker-compose.yml)
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# Dependências Python (modo servidor — NÃO precisa de captura de áudio local,
# então sem soundcard/sounddevice). O áudio chega por WebSocket.
RUN pip install \
      fastapi "uvicorn[standard]" \
      numpy webrtcvad-wheels \
      faster-whisper ctranslate2 transformers sentencepiece \
      deep-translator psutil yt-dlp

# Código
COPY scripts/ ./scripts/
COPY README.md ./

# 1) Baixa o Whisper 'small' (int8) — fica embutido na imagem (~480MB)
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('whisper small OK')"

# 2) Baixa + converte o modelo de tradução NLLB-200 -> data/marian/bible-ct2/
#    (backend 'marian'). É o passo mais pesado do build (~600MB download).
RUN python scripts/setup_marian.py --quantization int8 && echo "NLLB OK"

EXPOSE 3080

# Healthcheck pro Coolify saber quando está pronto
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3080/health',timeout=3).status==200 else 1)" || exit 1

# Variáveis sobrescrevíveis no compose (sem precisar rebuildar)
ENV PORT=3080 DEVICE=cpu MODEL=small COMPUTE_TYPE=int8 BACKEND=marian \
    PARTIAL_MS=1500 SILENCE_MS=700

CMD ["sh", "-c", "python scripts/server.py --host 0.0.0.0 --port ${PORT} --device ${DEVICE} --model ${MODEL} --compute-type ${COMPUTE_TYPE} --backend ${BACKEND} --partial-ms ${PARTIAL_MS} --silence-ms ${SILENCE_MS}"]
