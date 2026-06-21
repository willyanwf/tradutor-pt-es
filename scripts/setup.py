#!/usr/bin/env python3
"""Setup da skill tradutor: instala deps Python e (opcional) modelos offline.

Uso:
    python setup.py              # instala deps padrao (Google translate online)
    python setup.py --offline    # tambem instala argostranslate + modelos PT<->ES
    python setup.py --check      # so verifica o que esta instalado, sem mexer
"""

import importlib.util
import subprocess
import sys

BASE_PKGS = [
    "numpy",
    "sounddevice",
    "webrtcvad-wheels",        # build pre-compilado pra Windows
    "faster-whisper",
    "deep-translator",
    "fastapi",
    "uvicorn[standard]",
]

OFFLINE_PKGS = [
    "argostranslate",
]

REQUIRED_MODULES = {
    "numpy": "numpy",
    "sounddevice": "sounddevice",
    "webrtcvad": "webrtcvad-wheels",
    "faster_whisper": "faster-whisper",
    "deep_translator": "deep-translator",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
}

OFFLINE_MODULES = {
    "argostranslate": "argostranslate",
}


def have(module_name):
    return importlib.util.find_spec(module_name) is not None


def pip_install(packages):
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd)


def check(include_offline=False):
    print("\n=== Verificando dependencias ===")
    missing = []
    modules = dict(REQUIRED_MODULES)
    if include_offline:
        modules.update(OFFLINE_MODULES)
    for mod, pkg in modules.items():
        ok = have(mod)
        mark = "[ok]" if ok else "[--]"
        print(f"  {mark} {mod}  (pip: {pkg})")
        if not ok:
            missing.append(pkg)
    return missing


def install_argos_models():
    import argostranslate.package as ap

    print("\n=== Atualizando indice de modelos argos ===")
    ap.update_package_index()
    available = ap.get_available_packages()

    pairs = [("pt", "es"), ("es", "pt")]
    for src, tgt in pairs:
        pkg = next(
            (p for p in available if p.from_code == src and p.to_code == tgt),
            None,
        )
        if pkg is None:
            print(f"  [!] Nenhum pacote argos direto pra {src}->{tgt}")
            continue
        print(f"  baixando {src}->{tgt}...")
        path = pkg.download()
        ap.install_from_path(path)
        print(f"  [ok] argos {src}->{tgt} instalado")


def main():
    offline = "--offline" in sys.argv
    check_only = "--check" in sys.argv

    missing = check(include_offline=offline)

    if check_only:
        if missing:
            print(f"\n[!] Faltam: {missing}")
            return 1
        print("\n[ok] Tudo instalado.")
        return 0

    if missing:
        print(f"\n=== Instalando: {missing} ===")
        pip_install(missing)
    else:
        print("\n[ok] Deps Python ja instaladas.")

    if offline:
        try:
            install_argos_models()
        except Exception as exc:
            print(f"\n[!] Falha ao instalar modelos argos: {exc}")
            return 2

    print("\n" + "=" * 60)
    print("[ok] Setup completo.")
    print("Proximos passos:")
    print("  CLI:        python translate_live.py --list-devices")
    print("              python translate_live.py --device <N>")
    print("  Servidor:   python server.py")
    print("              -> abra http://localhost:8765 no navegador")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
