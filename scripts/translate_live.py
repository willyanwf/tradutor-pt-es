#!/usr/bin/env python3
"""Traducao simultanea PT-BR <-> ES via microfone (CLI).

Pipeline em pipeline.py. Esse script so faz captura via sounddevice
e imprime os resultados no terminal.
"""

import argparse
import queue
import signal
import sys
import threading
from datetime import datetime

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sys.exit("Falta 'sounddevice'. Rode: python setup.py")

try:
    import webrtcvad
except ImportError:
    sys.exit("Falta 'webrtcvad'. Rode: python setup.py")

try:
    from faster_whisper import WhisperModel
except ImportError:
    sys.exit("Falta 'faster-whisper'. Rode: python setup.py")

from pipeline import (
    FRAME_SIZE,
    SAMPLE_RATE,
    PhraseDetector,
    get_translator,
    transcribe_and_translate,
)


class AudioCapture:
    def __init__(self, device=None):
        self.q = queue.Queue()
        self.device = device

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        pcm = (indata[:, 0] * 32768).clip(-32768, 32767).astype(np.int16)
        self.q.put(pcm.tobytes())

    def stream(self):
        return sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SIZE,
            device=self.device,
            callback=self._callback,
        )


def main():
    parser = argparse.ArgumentParser(description="Traducao simultanea PT <-> ES via mic")
    parser.add_argument("--model", default="small",
                        choices=["tiny", "base", "small", "medium", "large-v3"])
    parser.add_argument("--device", type=int, default=None)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--backend", default="google", choices=["google", "argos"])
    parser.add_argument("--src", default=None)
    parser.add_argument("--tgt", default=None)
    parser.add_argument("--vad", type=int, default=2, choices=[0, 1, 2, 3])
    parser.add_argument("--compute-type", default="int8",
                        choices=["int8", "int8_float16", "float16", "float32"])
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    print(f"[setup] Carregando Whisper '{args.model}' ({args.compute_type})...",
          file=sys.stderr)
    model = WhisperModel(args.model, device="cpu", compute_type=args.compute_type)
    print("[setup] Whisper pronto.", file=sys.stderr)

    translator = get_translator(args.backend)
    vad = webrtcvad.Vad(args.vad)
    detector = PhraseDetector()
    capture = AudioCapture(device=args.device)
    stop_event = threading.Event()

    def handle_sigint(signum, frame):
        print("\n[stop] Encerrando...", file=sys.stderr)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sigint)

    print("=" * 60, file=sys.stderr)
    print("Traducao ao vivo iniciada. Fale no microfone. Ctrl+C para sair.",
          file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    with capture.stream():
        while not stop_event.is_set():
            try:
                frame = capture.q.get(timeout=0.5)
            except queue.Empty:
                continue

            phrase = detector.feed(frame, vad)
            if phrase is None:
                continue

            result = transcribe_and_translate(
                model, translator, phrase,
                forced_src=args.src, forced_tgt=args.tgt,
            )
            if result is None:
                continue

            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"\n[{ts}] ({result['src_lang']}->{result['tgt_lang']} | "
                f"audio {result['duration_s']:.1f}s | "
                f"stt {result['stt_s']:.1f}s | mt {result['mt_s']:.2f}s)"
            )
            print(f"  {result['src_lang'].upper()}: {result['src_text']}")
            print(f"  {result['tgt_lang'].upper()}: {result['tgt_text']}")
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
