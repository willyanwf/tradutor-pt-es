"""Captura de audio LOCAL no servidor — WASAPI loopback ou microfone local.

Diferencia-se da captura do browser (que vem via WebSocket): aqui o servidor
Python captura direto do hardware via `soundcard` lib.

Casos de uso:
  - Operador roda servidor numa maquina onde tem o som do Zoom/Teams tocando
    e quer transcrever o audio do app (loopback)
  - Mic local conectado direto no PC do servidor (sem precisar do browser)

Pipeline:
  hardware  ->  soundcard recorder (sample rate nativo)
            ->  downsample pra 16kHz mono
            ->  PCM int16 (mesmo formato do browser worklet)
            ->  callback / queue
"""

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger("tradutor.capture")


# Tamanho do chunk em ms (igual ao browser worklet)
CHUNK_MS = 30
TARGET_RATE = 16000


def list_devices():
    """Lista devices de audio disponiveis pra captura local.

    Retorna dict com:
      - microphones: lista de mics fisicos
      - loopbacks: lista de "speakers como mic" (loopback do sistema)
      - default_mic_id, default_speaker_id

    Soundcard expoe loopback do speaker como SpeakerInputMicrophone — capturar
    isso permite ler tudo que o sistema esta tocando (Zoom, Teams, browser).
    """
    try:
        import soundcard as sc
    except ImportError:
        return {"error": "soundcard nao instalado", "microphones": [], "loopbacks": []}

    devices = {"microphones": [], "loopbacks": []}

    # Mics fisicos
    try:
        for mic in sc.all_microphones(include_loopback=False):
            devices["microphones"].append({
                "id": mic.id,
                "name": mic.name,
                "channels": mic.channels,
            })
        default_mic = sc.default_microphone()
        devices["default_mic_id"] = default_mic.id if default_mic else None
    except Exception as exc:
        logger.warning("erro listando mics: %s", exc)
        devices["default_mic_id"] = None

    # Loopbacks (speakers expostos como mic) — Windows WASAPI loopback
    try:
        for spk in sc.all_microphones(include_loopback=True):
            # Filtra so os que sao loopback
            if "loopback" in spk.name.lower() or getattr(spk, "isloopback", False):
                devices["loopbacks"].append({
                    "id": spk.id,
                    "name": spk.name,
                    "channels": spk.channels,
                })
        default_spk = sc.default_speaker()
        devices["default_speaker_id"] = default_spk.id if default_spk else None
    except Exception as exc:
        logger.warning("erro listando loopbacks: %s", exc)
        devices["default_speaker_id"] = None

    return devices


def _resolve_device(source_spec):
    """Resolve uma source spec ('loopback:<id>' ou 'mic:<id>') no objeto do soundcard.

    Aceita tambem 'loopback' / 'mic' (sem id) — usa o default.
    """
    import soundcard as sc

    if source_spec in ("loopback", "system"):
        return sc.get_microphone(
            sc.default_speaker().id, include_loopback=True
        )
    if source_spec == "mic":
        return sc.default_microphone()
    if source_spec.startswith("loopback:"):
        dev_id = source_spec[len("loopback:"):]
        return sc.get_microphone(dev_id, include_loopback=True)
    if source_spec.startswith("mic:"):
        dev_id = source_spec[len("mic:"):]
        return sc.get_microphone(dev_id, include_loopback=False)
    raise ValueError(f"source desconhecido: {source_spec}")


def _downsample_to_16k_mono(samples, src_rate):
    """Recebe (frames, channels) float32 [-1,1] e devolve int16 mono 16kHz.

    Versao SEM estado (compat) — usa interpolacao linear simples. Pode gerar
    aliasing com audio de musica/aplausos. Prefira _StreamResampler no streaming.
    """
    if samples.ndim == 2 and samples.shape[1] > 1:
        # Mix down pra mono
        samples = samples.mean(axis=1)
    elif samples.ndim == 2:
        samples = samples[:, 0]

    if src_rate != TARGET_RATE:
        # Resample linear simples — boa qualidade pra ASR
        # Tamanho novo = round(N * 16000 / src_rate)
        new_len = int(round(len(samples) * TARGET_RATE / src_rate))
        if new_len > 0:
            indices = np.linspace(0, len(samples) - 1, new_len)
            samples = np.interp(indices, np.arange(len(samples)), samples)

    # Float32 [-1,1] -> int16
    pcm = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
    return pcm.tobytes()


class _StreamResampler:
    """Downsample streaming float32 (frames, ch) -> int16 mono 16kHz, COM
    low-pass FIR anti-aliasing e estado contínuo entre chunks.

    Por que: o downsample linear simples (np.interp) sem filtro deixa energia
    acima de 8kHz "dobrar" (aliasing) pra dentro da banda de voz — vira chiado.
    Com áudio de YouTube/culto (música, aplausos, instrumentos), isso suja o
    sinal que vai pro Whisper. O FIR low-pass (~7.2kHz) antes da decimação
    elimina isso. Implementado em numpy puro (sem scipy) pra não pesar nos PCs
    da igreja. O estado (_tail) mantém continuidade do filtro entre os chunks
    de 30ms, sem cliques nas bordas.
    """

    def __init__(self, target_rate=TARGET_RATE, numtaps=31):
        self.target_rate = target_rate
        self.numtaps = numtaps
        self._src_rate = None
        self._taps = None
        self._tail = None  # últimas (numtaps-1) amostras do chunk anterior

    def _ensure_filter(self, src_rate):
        if self._src_rate == src_rate and self._taps is not None:
            return
        self._src_rate = src_rate
        # Cutoff em ~90% do Nyquist do alvo (8kHz) → 7.2kHz
        cutoff = min(self.target_rate * 0.45, src_rate * 0.45)
        n = np.arange(self.numtaps) - (self.numtaps - 1) / 2.0
        fc = cutoff / src_rate
        h = np.sinc(2 * fc * n) * np.hamming(self.numtaps)
        h = (h / h.sum()).astype(np.float32)
        self._taps = h
        self._tail = np.zeros(self.numtaps - 1, dtype=np.float32)

    def process(self, samples, src_rate):
        # Mixdown pra mono
        if samples.ndim == 2 and samples.shape[1] > 1:
            samples = samples.mean(axis=1)
        elif samples.ndim == 2:
            samples = samples[:, 0]
        samples = np.ascontiguousarray(samples, dtype=np.float32)

        if src_rate != self.target_rate:
            self._ensure_filter(src_rate)
            # FIR low-pass com estado contínuo (overlap-save simplificado):
            # prepende o tail do chunk anterior, convolve em 'valid' (saída
            # alinhada ao tamanho do chunk), guarda o novo tail.
            x = np.concatenate([self._tail, samples])
            filtered = np.convolve(x, self._taps, mode="valid")
            tail_len = self.numtaps - 1
            if len(samples) >= tail_len:
                self._tail = samples[-tail_len:].copy()
            else:
                self._tail = np.concatenate([self._tail, samples])[-tail_len:]
            # Decima filtered -> target_rate
            new_len = int(round(len(filtered) * self.target_rate / src_rate))
            if new_len <= 0:
                return b""
            idx = np.linspace(0, len(filtered) - 1, new_len)
            samples = np.interp(idx, np.arange(len(filtered)), filtered)

        pcm = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
        return pcm.tobytes()


class LocalAudioCapture:
    """Roda thread que captura audio local e chama callback com bytes PCM int16 16kHz mono."""

    def __init__(self, source_spec: str, on_pcm: Callable[[bytes], None],
                 on_error: Optional[Callable[[str], None]] = None):
        self.source_spec = source_spec
        self.on_pcm = on_pcm
        # on_error(msg) é chamado se a captura morrer (device sumiu, exclusive
        # mode, etc) — pra UI avisar o operador em vez de só parar em silêncio.
        self.on_error = on_error
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._device = None
        self._resampler = _StreamResampler()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        try:
            self._device = _resolve_device(self.source_spec)
        except Exception as exc:
            logger.error("falha resolver device %s: %s", self.source_spec, exc)
            raise
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("capture local INICIADA (source=%s, device=%s)",
                    self.source_spec, getattr(self._device, "name", "?"))

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("capture local PARADA")

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        # Tenta abrir em 48kHz (Windows default). Se rolar erro, deixa o
        # device escolher. O _StreamResampler aplica anti-aliasing e converte
        # pra 16kHz mono mantendo estado entre chunks.
        try:
            with self._device.recorder(samplerate=48000, channels=None,
                                       blocksize=int(48000 * CHUNK_MS / 1000)) as rec:
                src_rate = 48000
                silent_streak = 0.0  # segundos consecutivos de silêncio no início
                got_audio = False
                while not self._stop_event.is_set():
                    # Captura 30ms
                    frames = rec.record(numframes=int(src_rate * CHUNK_MS / 1000))
                    if frames is None or len(frames) == 0:
                        continue
                    # Detecta silêncio inicial (bug soundcard #166: se abrir antes
                    # do áudio tocar, pode travar). Só loga aviso, não quebra.
                    if not got_audio:
                        try:
                            peak = float(np.abs(frames).max())
                        except Exception:
                            peak = 1.0
                        if peak < 1e-4:
                            silent_streak += CHUNK_MS / 1000.0
                            if silent_streak >= 5.0 and self.on_error:
                                # Avisa UMA vez que está mudo (ex: live sem play)
                                self.on_error(
                                    "Captura ativa mas sem áudio há 5s — "
                                    "dê play na fonte (YouTube/live) ou confira o volume."
                                )
                                silent_streak = -1e9  # não repete o aviso
                        else:
                            got_audio = True
                    pcm = self._resampler.process(frames, src_rate)
                    try:
                        self.on_pcm(pcm)
                    except Exception as exc:
                        logger.warning("on_pcm raised: %s", exc)
        except Exception as exc:
            logger.exception("capture loop fatal: %s", exc)
            # Propaga pra UI saber (device sumiu, exclusive mode, etc) — em vez
            # de parar em silêncio sem ninguém perceber durante o culto.
            if self.on_error and not self._stop_event.is_set():
                try:
                    self.on_error(f"Captura de áudio parou: {exc}")
                except Exception:
                    pass


if __name__ == "__main__":
    import json
    print(json.dumps(list_devices(), indent=2, ensure_ascii=False))
