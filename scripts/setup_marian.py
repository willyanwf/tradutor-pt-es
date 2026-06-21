#!/usr/bin/env python3
"""Baixa e converte o modelo Marian biblico pra ctranslate2.

Modelo: Helsinki-NLP/opus-mt-tc-bible-big-deu_eng_fra_por_spa-roa
  - 1 modelo serve PT->ES e ES->PT (multi-target via prefixo)
  - Treinado em corpus biblico — qualidade religiosa nativa
  - ~600MB download, ~300MB em float16 CT2

Saida: <skill>/data/marian/bible-ct2/

Uso:
    python setup_marian.py                  # converte com float16
    python setup_marian.py --quantization int8_float16  # menor footprint
    python setup_marian.py --force          # reconverte se ja existir
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_ROOT = SCRIPT_DIR.parent / "data" / "marian"

HF_MODEL = "facebook/nllb-200-distilled-600M"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quantization", default="float16",
                        choices=["float16", "int8_float16", "int8", "float32"],
                        help="Default float16 — bom pra GPU. "
                             "int8_float16 economiza VRAM com pouca perda.")
    parser.add_argument("--force", action="store_true",
                        help="Reconverter mesmo se ja existir")
    args = parser.parse_args()

    out_dir = DATA_ROOT / "bible-ct2"

    if out_dir.exists() and not args.force:
        print(f"[skip] Modelo ja existe em {out_dir}")
        print("       Use --force pra reconverter.")
        return 0

    if args.force and out_dir.exists():
        print(f"Removendo {out_dir}...")
        shutil.rmtree(out_dir)

    out_dir.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "ctranslate2.converters.transformers",
        "--model", HF_MODEL,
        "--output_dir", str(out_dir),
        "--quantization", args.quantization,
    ]
    print(f"\n=== Baixando + convertendo {HF_MODEL} ===")
    print(f"Quantization: {args.quantization}")
    print(f"Output: {out_dir}")
    print(">>>", " ".join(cmd))
    print()
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        print(f"\n[ERRO] Conversao falhou: {exc}")
        return 1

    print(f"\n=== [ok] Modelo convertido em {out_dir} ===")
    print(f"Pra usar:")
    print(f"  python server.py --backend marian")
    return 0


if __name__ == "__main__":
    sys.exit(main())
