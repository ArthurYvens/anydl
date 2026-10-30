"""
Draw the anydl icon and write assets/anydl.ico plus assets/anydl.png.

The mark is a lowercase d -- the last letter of the name, and the one that
reads as "download" -- whose bowl is a play button, white on the blue the
window's primary actions use.

What rendering it at every size taught:

* At 16 and 24 px the full drawing fails: the ring around the play button is
  one pixel and the triangle becomes a smudge, so the small sizes get their
  own proportions -- a heavier stroke and a triangle that fills the bowl.
* The stem stands a little taller than the bowl's top, or the letter reads as
  an o with a bar beside it.
* Pillow draws hard edges, so everything is drawn six times larger and
  scaled down.

Run this only when the artwork changes; the generated files are committed so
nobody needs Pillow just to use the app.

    python tools/make_icon.py
"""

import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("This script needs Pillow:  pip install pillow")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

GRADIENT_TOP = (59, 130, 246)     # blue 500
GRADIENT_BOTTOM = (29, 78, 216)   # blue 700, also the play button's hole
MARK = (255, 255, 255)

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
SMALL = 24          # this size and under use the simplified proportions
SUPERSAMPLE = 6


def background(size):
    """Rounded square filled with the vertical gradient."""
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        column.putpixel((0, y), tuple(
            round(GRADIENT_TOP[i] + (GRADIENT_BOTTOM[i] - GRADIENT_TOP[i]) * t)
            for i in range(3)
        ))

    art = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    art.paste(column.resize((size, size), Image.NEAREST), (0, 0))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.23), fill=255)
    art.putalpha(mask)
    return art


def draw_mark(size, small):
    """Return a transparent layer holding the d and its play button."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Bowl centre and radius, the ring's weight, and how much of the hole the
    # triangle fills. The small set trades the ring's elegance for pixels.
    cx, cy = s * (0.46 if small else 0.47), s * 0.56
    r = s * (0.26 if small else 0.24)
    stroke = s * (0.13 if small else 0.11)
    fill = 0.95 if small else 0.80

    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MARK)
    inner = r - stroke
    draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner],
                 fill=GRADIENT_BOTTOM + (255,))
    draw.rounded_rectangle([cx + r - stroke, s * 0.20, cx + r, cy + r],
                           radius=stroke / 2, fill=MARK)

    # The play triangle, its centroid nudged right of the bowl's centre so it
    # looks centred: a triangle's visual weight sits towards its flat side.
    tri = inner * fill
    draw.polygon([(cx - tri * 0.50, cy - tri * 0.70), (cx - tri * 0.50, cy + tri * 0.70),
                  (cx + tri * 0.75, cy)], fill=MARK)
    return img


def render(size):
    """Render one icon at the given size, supersampled for clean edges."""
    big = size * SUPERSAMPLE
    art = Image.alpha_composite(background(big), draw_mark(big, size <= SMALL))
    return art.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(ASSETS, exist_ok=True)

    # Each size is rendered on its own rather than downscaled from one master,
    # which is what keeps the 16 px version from turning to mush.
    images = [render(s) for s in ICO_SIZES]
    largest = images[-1]

    ico_path = os.path.join(ASSETS, "anydl.ico")
    largest.save(ico_path, format="ICO",
                 sizes=[(s, s) for s in ICO_SIZES],
                 append_images=images[:-1])

    png_path = os.path.join(ASSETS, "anydl.png")
    largest.save(png_path, format="PNG")

    for path in (ico_path, png_path):
        print("wrote %s (%d bytes)" % (os.path.relpath(path, ROOT),
                                       os.path.getsize(path)))


if __name__ == "__main__":
    main()
