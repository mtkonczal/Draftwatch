"""Maintainer-only: draw the app icon to draftwatch/assets/icon.png (the native
window's Dock icon) and favicon.png (the browser tab).

The PNGs are committed, so end users never need Pillow; rerun this only to change
the design (python3 -m pip install pillow && python3 scripts/make_icon.py). The "D" is built
from plain shapes rather than a font glyph, so there are no font-licensing
questions. The two equal-length strokes use the diff panel's delete (coral)
and add (green) colors: a draft being reviewed.
"""
import os

from PIL import Image, ImageDraw

S = 1024            # final size (macOS Dock icons are drawn from a 1024 master)
K = 4               # supersampling factor for smooth edges
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                      "draftwatch", "assets")
FAVICON = 64        # 32px tab icon at 2x

BLUE = (47, 111, 176)       # the default app accent, #2f6fb0
PAPER = (247, 251, 255)
ADD = (133, 210, 150)
DEL = (241, 141, 127)


def box(*v):
    return [int(round(x * K)) for x in v]


def main():
    w = S * K
    # Apple's icon grid: an 824px rounded square centered in the 1024 canvas,
    # leaving room for the Dock's drop shadow.
    tile = Image.new("L", (w, w), 0)
    ImageDraw.Draw(tile).rounded_rectangle(box(100, 100, 924, 924),
                                           radius=182 * K, fill=255)
    icon = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    icon.paste(Image.new("RGB", (w, w), BLUE), (0, 0), tile)

    # The D: a rectangle joined to a right half-circle (the outer shape), minus
    # the same construction smaller (the counter). Both halves share cx.
    d = Image.new("L", (w, w), 0)
    dd = ImageDraw.Draw(d)

    def half_disc(left, top, right, bottom, fill):
        r = (bottom - top) / 2
        cx = right - r
        dd.rectangle(box(left, top, cx, bottom), fill=fill)
        dd.pieslice(box(cx - r, top, right, bottom), -90, 90, fill=fill)

    half_disc(314, 251, 718, 765, 255)    # outer
    half_disc(425, 354, 615, 662, 0)      # counter
    icon.paste(PAPER, (0, 0), d)

    # Equal strokes keep addition and deletion visually balanced at tab size.
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(box(472, 433, 638, 481), radius=24 * K, fill=DEL)
    draw.rounded_rectangle(box(472, 536, 638, 584), radius=24 * K, fill=ADD)

    # Favicon: the tile cropped full-bleed (no Dock-shadow margin), since a
    # browser tab gives the icon only a tiny square.
    fav = icon.crop(tuple(box(100, 100, 924, 924))).resize((FAVICON, FAVICON),
                                                          Image.LANCZOS)
    icon = icon.resize((S, S), Image.LANCZOS)
    for name, im in (("icon.png", icon), ("favicon.png", fav)):
        out = os.path.join(ASSETS, name)
        im.save(out, optimize=True)
        print("wrote", os.path.normpath(out), os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
