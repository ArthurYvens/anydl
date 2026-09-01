"""
Draw the anydl icon and write assets/anydl.ico plus assets/anydl.png.

The mark is a dragonfly in flat vector style: four wings in two tones over a
violet gradient, with the abdomen segmented into bands that thin and lighten
toward the tip.

Three things drove the geometry, all learned by rendering and looking:

* Thin wings vanish. At 16 px the first pass lost them entirely, so the wings
  are deliberately heavier than a naturalistic dragonfly would have.
* The membrane sits at high alpha rather than a delicate wash, for the same
  reason.
* Pillow cannot draw a rotated ellipse, so each wing is drawn horizontally on
  its own layer and the layer is rotated around the body pivot.

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

GRADIENT_TOP = (79, 70, 229)      # indigo
GRADIENT_BOTTOM = (139, 92, 246)  # violet

WING_EDGE = (196, 181, 253, 255)     # solid leading edge
WING_MEMBRANE = (233, 213, 255, 190)  # wider, softer panel behind it
THORAX = (255, 255, 255, 255)
EYE = (255, 255, 255, 255)
EYE_CENTRE = (139, 92, 246, 255)
ABDOMEN = [(255, 255, 255), (243, 232, 255), (233, 213, 255),
           (216, 180, 254), (196, 181, 253), (167, 139, 250)]

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
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
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    art.putalpha(mask)
    return art


def wing(size, pivot, inset, length, width, angle, colour):
    """One wing: an ellipse growing to the right of the pivot, then rotated."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px, py = pivot
    ImageDraw.Draw(layer).ellipse(
        [px + inset, py - width / 2, px + inset + length, py + width / 2],
        fill=colour,
    )
    return layer.rotate(angle, resample=Image.BICUBIC, center=pivot)


def draw_dragonfly(size):
    """Return a transparent layer holding the whole insect."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    cx = s * 0.5
    upper_pivot = (cx, s * 0.30)
    lower_pivot = (cx, s * 0.37)

    # Membranes first, then the solid edges on top of them. Angles mirror
    # across the body: 7 and 173 are the same wing on either side.
    panels = [
        (upper_pivot, 0.45, 0.135, (7, 173), WING_MEMBRANE),
        (lower_pivot, 0.44, 0.125, (-36, 216), WING_MEMBRANE),
        (upper_pivot, 0.44, 0.080, (9, 171), WING_EDGE),
        (lower_pivot, 0.43, 0.074, (-34, 214), WING_EDGE),
    ]
    for pivot, length, width, angles, colour in panels:
        for angle in angles:
            img = Image.alpha_composite(
                img, wing(s, pivot, s * 0.02, s * length, s * width, angle, colour))

    draw = ImageDraw.Draw(img)

    # Compound eyes: two overlapping discs with a darker seam between them.
    r = s * 0.085
    draw.ellipse([cx - r * 1.75, s * .085, cx + r * .25, s * .085 + r * 2], fill=EYE)
    draw.ellipse([cx - r * .25, s * .085, cx + r * 1.75, s * .085 + r * 2], fill=EYE)
    draw.ellipse([cx - r * .38, s * .095, cx + r * .38, s * .235], fill=EYE_CENTRE)

    draw.rounded_rectangle(
        [cx - s * .075, s * .225, cx + s * .075, s * .415],
        radius=s * .07, fill=THORAX)

    # Abdomen: stacked bands, each narrower and lighter than the one above.
    top, bottom = s * .40, s * .90
    half_top, half_tip = s * .058, s * .028
    count = len(ABDOMEN)
    for i, colour in enumerate(ABDOMEN):
        ta, tb = i / count, (i + 1) / count
        ya, yb = top + (bottom - top) * ta, top + (bottom - top) * tb
        wa = half_top + (half_tip - half_top) * ta
        wb = half_top + (half_tip - half_top) * tb
        draw.polygon([(cx - wa, ya), (cx + wa, ya), (cx + wb, yb), (cx - wb, yb)],
                     fill=colour + (255,))
    draw.ellipse([cx - half_tip, bottom - half_tip, cx + half_tip, bottom + half_tip],
                 fill=ABDOMEN[-1] + (255,))
    return img


def render(size):
    """Render one icon at the given size, supersampled for clean edges."""
    big = size * SUPERSAMPLE
    art = Image.alpha_composite(background(big), draw_dragonfly(big))
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
