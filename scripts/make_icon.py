"""Maintainer-only: draw the app icon to draftwatch/assets/icon.png.

The PNG is committed, so end users never need Pillow; rerun this only to change
the design (pip install pillow && python scripts/make_icon.py). The "D" is built
from plain shapes rather than a font glyph, so there are no font-licensing
questions. Inside its counter sit two short strokes in the diff panel's add
(green) and delete (red) colors: a draft being reviewed.
"""
import os

from PIL import Image, ImageDraw

S = 1024            # final size (macOS Dock icons are drawn from a 1024 master)
K = 4               # supersampling factor for smooth edges
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "draftwatch", "assets", "icon.png")

TOP = (61, 130, 199)        # steel-blue gradient, around the app's --accent #2f6fb0
BOTTOM = (33, 88, 146)
PAPER = (245, 249, 254)     # --accent-fg: the light ink on the accent
ADD = (126, 208, 138)       # diff add, brightened to read on blue
DEL = (240, 142, 127)       # diff delete, likewise


def box(*v):
    return [int(round(x * K)) for x in v]


def main():
    w = S * K
    # Apple's icon grid: an 824px rounded square centered in the 1024 canvas,
    # leaving room for the Dock's drop shadow.
    tile = Image.new("L", (w, w), 0)
    ImageDraw.Draw(tile).rounded_rectangle(box(100, 100, 924, 924),
                                           radius=185 * K, fill=255)
    grad = Image.new("RGB", (w, w))
    gd = ImageDraw.Draw(grad)
    for y in range(w):
        t = y / (w - 1)
        gd.line([(0, y), (w, y)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    icon = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), tile)

    # The D: a rectangle joined to a right half-circle (the outer shape), minus
    # the same construction smaller (the counter). Both halves share cx.
    d = Image.new("L", (w, w), 0)
    dd = ImageDraw.Draw(d)

    def half_disc(left, top, right, bottom, fill):
        r = (bottom - top) / 2
        cx = right - r
        dd.rectangle(box(left, top, cx, bottom), fill=fill)
        dd.pieslice(box(cx - r, top, right, bottom), -90, 90, fill=fill)

    half_disc(322, 262, 738, 762, 255)    # outer
    half_disc(428, 368, 632, 656, 0)      # counter
    icon.paste(PAPER, (0, 0), d)

    # Two "lines of text" in the counter: one added, one deleted.
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(box(470, 452, 590, 494), radius=21 * K, fill=ADD)
    draw.rounded_rectangle(box(470, 530, 562, 572), radius=21 * K, fill=DEL)

    icon = icon.resize((S, S), Image.LANCZOS)
    icon.save(OUT, optimize=True)
    print("wrote", os.path.normpath(OUT), os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
