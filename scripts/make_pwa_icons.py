#!/usr/bin/env python3
"""Gera os ícones do PWA da audiência (tela /v2) em static/.

Desenho: balão de fala branco (a Palavra falada) com uma cruz azul (igreja)
e um ponto âmbar no canto (sinal de "ao vivo"). Fundo gradiente azul.
Cores casam com o app: azul = PT/origem, âmbar = ES/ao vivo.

Saídas (em scripts/static/):
  icon-192.png            (any)        — home screen Android
  icon-512.png            (any)        — splash / lojas
  icon-maskable-512.png   (maskable)   — adaptive icon Android (sangria total)
  apple-touch-icon.png    (180, opaco) — iOS "Añadir a inicio"
  favicon.png             (32)         — aba do navegador
"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "static"
OUT.mkdir(parents=True, exist_ok=True)

M = 2048  # master grande -> downscale com LANCZOS = bordas suaves

TOP = (28, 62, 112)     # azul topo
BOT = (9, 20, 40)       # azul base
WHITE = (250, 250, 252)
CROSS = (31, 58, 120)   # azul profundo
AMBER = (245, 176, 38)  # âmbar "ao vivo"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient_bg(size, top, bot):
    img = Image.new("RGB", (size, size), top)
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=lerp(top, bot, y / (size - 1)))
    return img


def rrect(d, box, r, fill):
    d.rounded_rectangle(box, radius=int(r), fill=fill)


def draw_content(img):
    d = ImageDraw.Draw(img)

    # ---- Balão de fala (branco) ----
    bx0, by0, bx1, by1 = 0.24 * M, 0.20 * M, 0.76 * M, 0.60 * M
    # cauda do balão (triângulo apontando pra baixo-esquerda)
    d.polygon([(0.34 * M, 0.575 * M), (0.47 * M, 0.575 * M), (0.31 * M, 0.74 * M)],
              fill=WHITE)
    rrect(d, (bx0, by0, bx1, by1), 0.14 * M, WHITE)

    # ---- Cruz (azul) dentro do balão ----
    cx, cy = 0.50 * M, 0.40 * M
    bar = 0.024 * M                      # meia-espessura
    v_h = 0.135 * M                      # meia-altura vertical
    h_w = 0.082 * M                      # meia-largura horizontal
    cross_y = 0.345 * M                  # travessa um pouco acima (cruz latina)
    rrect(d, (cx - bar, cy - v_h, cx + bar, cy + v_h * 0.92), bar, CROSS)   # vertical
    rrect(d, (cx - h_w, cross_y - bar, cx + h_w, cross_y + bar), bar, CROSS)  # horizontal

    # ---- Ponto âmbar "ao vivo" (canto sup. direito do balão) ----
    dr = 0.058 * M
    dcx, dcy = 0.725 * M, 0.255 * M
    d.ellipse((dcx - dr - 0.018 * M, dcy - dr - 0.018 * M,
               dcx + dr + 0.018 * M, dcy + dr + 0.018 * M), fill=WHITE)  # anel branco
    d.ellipse((dcx - dr, dcy - dr, dcx + dr, dcy + dr), fill=AMBER)


def make_master(rounded):
    img = gradient_bg(M, TOP, BOT).convert("RGBA")
    draw_content(img)
    if rounded:
        mask = Image.new("L", (M, M), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, M, M), radius=int(0.22 * M), fill=255)
        img.putalpha(mask)
    return img


def main():
    rounded = make_master(rounded=True)
    bleed = make_master(rounded=False)

    def save(img, name, size, opaque=False):
        out = img.resize((size, size), Image.LANCZOS)
        if opaque:
            bg = Image.new("RGB", (size, size), BOT)
            bg.paste(out, (0, 0), out)
            out = bg
        out.save(OUT / name)
        print(f"  {name}  ({size}x{size})")

    print("Gerando ícones do PWA em", OUT)
    save(rounded, "icon-192.png", 192)
    save(rounded, "icon-512.png", 512)
    save(bleed, "icon-maskable-512.png", 512)
    save(bleed, "apple-touch-icon.png", 180, opaque=True)
    save(rounded, "favicon.png", 32)
    print("OK")


if __name__ == "__main__":
    main()
