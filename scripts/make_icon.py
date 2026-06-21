#!/usr/bin/env python3
"""Gera o icone tradutor.ico (256x256 + tamanhos menores) usando PIL.

Design: circulo com gradient verde (PT) -> laranja (ES), texto "PT/ES" branco.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SCRIPT_DIR = Path(__file__).parent.resolve()
OUT_PATH = SCRIPT_DIR / "tradutor.ico"
PNG_PREVIEW = SCRIPT_DIR / "tradutor_icon_preview.png"


COLOR_PT = (95, 207, 128, 255)    # verde do operador (--pt)
COLOR_ES = (255, 180, 85, 255)    # laranja (--es)
COLOR_BG = (15, 17, 21, 255)      # quase preto (--bg)
COLOR_TEXT = (255, 255, 255, 255)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(4))


def make_icon(size):
    """Gera 1 PNG quadrado size x size com o design."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circulo de fundo com leve gradient (linhas horizontais)
    # PIL nao tem gradient nativo, fazemos manualmente
    for y in range(size):
        t = y / max(1, size - 1)
        # gradient: PT (verde) no topo -> ES (laranja) embaixo
        color = lerp(COLOR_PT, COLOR_ES, t)
        draw.line([(0, y), (size, y)], fill=color)

    # Mascara circular
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((0, 0, size - 1, size - 1), fill=255)
    img.putalpha(mask)

    # Texto "PT" no topo, "ES" embaixo (separados por linha)
    if size >= 48:
        try:
            # Tenta fontes do Windows
            font_big = ImageFont.truetype("segoeuib.ttf", int(size * 0.32))  # bold
            font_small = ImageFont.truetype("segoeui.ttf", int(size * 0.13))
        except Exception:
            font_big = ImageFont.load_default()
            font_small = font_big

        # Layout vertical: PT no topo, seta no meio, ES embaixo
        try:
            font_lang = ImageFont.truetype("segoeuib.ttf", int(size * 0.28))
            font_arrow = ImageFont.truetype("segoeuib.ttf", int(size * 0.22))
        except Exception:
            font_lang = ImageFont.load_default()
            font_arrow = font_lang

        def centered_text(text, font, y_offset):
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = (size - tw) // 2
            y = (size - th) // 2 + y_offset
            # Sombra leve
            draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 110))
            draw.text((x, y), text, font=font, fill=COLOR_TEXT)
            return th

        # PT em cima
        centered_text("PT", font_lang, -int(size * 0.22))
        # Seta no meio
        centered_text("↓", font_arrow, -int(size * 0.02))
        # ES embaixo
        centered_text("ES", font_lang, int(size * 0.22))
    else:
        # Tamanhos pequenos: so o texto curto
        try:
            font = ImageFont.truetype("segoeuib.ttf", int(size * 0.45))
        except Exception:
            font = ImageFont.load_default()
        text = "T"
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (size - tw) // 2
        y = (size - th) // 2
        draw.text((x, y), text, font=font, fill=COLOR_TEXT)

    return img


def main():
    # Gera tamanhos pro .ico
    sizes = [256, 128, 64, 48, 32, 16]
    images = [make_icon(s) for s in sizes]

    # Salva o maior como preview PNG
    images[0].save(PNG_PREVIEW, format="PNG")

    # Salva como .ico com todos os tamanhos
    images[0].save(
        OUT_PATH,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )

    print(f"[ok] Gerado: {OUT_PATH}")
    print(f"[ok] Preview: {PNG_PREVIEW}")


if __name__ == "__main__":
    main()
