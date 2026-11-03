"""Deterministic festival artwork shared by the scorecard renderers.

Artwork is drawn locally, without downloads or font-dependent emoji. The
renderers take an explicit theme so previews and concurrent renders cannot
change each other's palette.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

NORMAL = "normal"
DIWALI = "diwali"
GOLD = (246, 193, 86)
PALE_GOLD = (255, 225, 151)
SUMMARY_FOOTER_H = 92


def normalize_theme(value):
    return DIWALI if str(value or "").strip().lower() == DIWALI else NORMAL


class _Art:
    """Draw in reference pixels on a transparent, supersampled layer."""

    def __init__(self, image, scale):
        self.image = image
        self.scale = scale
        self.layer = Image.new("RGBA", image.size)
        self.draw = ImageDraw.Draw(self.layer)

    def box(self, x0, y0, x1, y1):
        return tuple(round(v * self.scale) for v in (x0, y0, x1, y1))

    def line(self, points, color, width=1):
        self.draw.line([(round(x * self.scale), round(y * self.scale))
                        for x, y in points], fill=color,
                       width=max(1, round(width * self.scale)))

    def polygon(self, points, color):
        self.draw.polygon([(round(x * self.scale), round(y * self.scale))
                           for x, y in points], fill=color)

    def ellipse(self, cx, cy, rx, ry, color, outline=None, width=1):
        self.draw.ellipse(self.box(cx - rx, cy - ry, cx + rx, cy + ry),
                          fill=color, outline=outline,
                          width=max(1, round(width * self.scale)))

    def glow(self, cx, cy, rx, ry, color):
        from PIL import ImageFilter
        w, h = max(4, round(rx * self.scale * 4)), max(4, round(ry * self.scale * 4))
        glow = Image.new("RGBA", (w, h))
        ImageDraw.Draw(glow).ellipse((w // 4, h // 4, w * 3 // 4, h * 3 // 4), fill=color)
        glow = glow.filter(ImageFilter.GaussianBlur(max(1, min(w, h) * 0.16)))
        self.layer.alpha_composite(glow, (round(cx * self.scale - w / 2),
                                          round(cy * self.scale - h / 2)))

    def finish(self):
        self.image.alpha_composite(self.layer)


def _firework(art, cx, cy, radius, color):
    for i in range(28):
        angle = i * math.tau / 28
        r = radius * (0.72 + (i % 4) * 0.09)
        dx, dy = math.cos(angle), math.sin(angle)
        art.line([(cx + dx * r * 0.2, cy + dy * r * 0.2),
                  (cx + dx * r * 0.82, cy + dy * r * 0.82)], (*color, 160), 1)
        art.ellipse(cx + dx * r, cy + dy * r, 1.1, 1.1, (*PALE_GOLD, 220))
    art.ellipse(cx, cy, 2, 2, (*PALE_GOLD, 255))


def _lights(art, x0, x1, drop=22):
    points = [(x0 + (x1 - x0) * i / 80,
               3 + drop * math.sin(math.pi * i / 80)) for i in range(81)]
    art.line(points, (197, 135, 55, 255), 1.2)
    for i in range(23):
        t = i / 22
        cx, cy = x0 + (x1 - x0) * t, 3 + drop * math.sin(math.pi * t)
        art.glow(cx, cy + 3, 7, 7, (255, 144, 32, 160))
        art.ellipse(cx, cy + 3, 3, 3, (255, 184, 64, 255))
        art.ellipse(cx, cy + 2.5, 1.4, 1.6, (255, 251, 220, 255))


def _lantern(art, cx, top, size):
    w, h = size * 0.31, size
    art.line([(cx, 0), (cx, top)], (*GOLD, 200))
    art.glow(cx, top + h * 0.55, w * 2, h * 0.8, (255, 113, 13, 75))
    art.polygon([(cx - w, top + h * 0.16), (cx, top),
                 (cx + w, top + h * 0.16)], (176, 67, 26, 255))
    art.polygon([(cx - w, top + h * 0.16), (cx + w, top + h * 0.16),
                 (cx + w * 0.82, top + h * 0.79),
                 (cx, top + h * 0.92), (cx - w * 0.82, top + h * 0.79)],
                (234, 110, 23, 255))
    art.ellipse(cx, top + h * 0.46, w * 0.58, h * 0.29, (255, 184, 51, 255))
    art.ellipse(cx, top + h * 0.46, w * 0.22, h * 0.22, (255, 226, 134, 255))
    art.line([(cx - w, top + h * 0.16), (cx + w, top + h * 0.16)], (*GOLD, 255), 2)
    art.line([(cx - w * 0.82, top + h * 0.79),
              (cx, top + h * 0.92), (cx + w * 0.82, top + h * 0.79)], (*GOLD, 255), 2)
    for dx in (-0.22, 0, 0.22):
        art.line([(cx + w * dx, top + h * 0.92),
                  (cx + w * dx, top + h * 1.2)], (224, 142, 43, 255), 1)


def _diya(art, cx, cy, size):
    """An oil lamp; cy is the rim, with the flame above it."""
    art.glow(cx, cy - size * 0.17, size * 1.4, size * 0.85,
             (255, 135, 20, 140))
    art.glow(cx, cy + size * 0.48, size * 0.86, size * 0.15,
             (255, 177, 50, 130))
    art.polygon([(cx - size, cy), (cx + size, cy),
                 (cx + size * 0.66, cy + size * 0.4),
                 (cx, cy + size * 0.55), (cx - size * 0.66, cy + size * 0.4)],
                (155, 42, 27, 255))
    art.ellipse(cx, cy, size, size * 0.24, (253, 170, 41, 255),
                (255, 221, 122, 255), 1.5)
    art.ellipse(cx, cy + size * 0.02, size * 0.84, size * 0.13,
                (109, 31, 29, 255))
    for i in range(7):
        dx = (i - 3) * size * 0.21
        art.ellipse(cx + dx, cy + size * (0.26 + 0.09 * (1 - abs(i - 3) / 3)),
                    size * 0.025, size * 0.025, (*GOLD, 255))
    art.polygon([(cx, cy - size * 1.05), (cx + size * 0.14, cy - size * 0.5),
                 (cx + size * 0.12, cy - size * 0.12), (cx, cy),
                 (cx - size * 0.14, cy - size * 0.18),
                 (cx - size * 0.11, cy - size * 0.52)], (255, 185, 58, 255))
    art.ellipse(cx, cy - size * 0.29, size * 0.06, size * 0.24,
                (255, 250, 208, 255))


def _rangoli(art, cx, cy, radius):
    colors = ((223, 70, 117, 230), (244, 142, 42, 240),
              (113, 80, 187, 240), (247, 197, 79, 255))
    # Flattened circles and petals read as a rangoli lying on the ground.
    for ring in range(4):
        outer = radius * (1 - ring * 0.19)
        inner = outer * 0.78
        color = colors[ring]
        for i in range(20):
            a = i * math.tau / 20 + (ring % 2) * math.pi / 20
            points = []
            for step in range(21):
                t = step / 20
                r = inner + (outer - inner) * math.sin(math.pi * t)
                angle = a + (t - 0.5) * 0.27
                points.append((cx + math.cos(angle) * r,
                               cy + math.sin(angle) * r * 0.29))
            art.polygon(points, color)
        art.ellipse(cx, cy, inner, inner * 0.29, None, (*GOLD, 220), 1)


def draw_diwali_background(image, *, scale=1, footer_height=SUMMARY_FOOTER_H):
    """A navy festival backdrop; all decorations stay behind scorecard data."""
    width, height = image.width / scale, image.height / scale
    gradient = Image.new("RGB", (192, 128))
    pixels = gradient.load()
    for y in range(128):
        for x in range(192):
            nx, ny = x / 191, y / 127
            warm = (math.exp(-((nx - 0.02) ** 2 / 0.045 + (ny - 0.86) ** 2 / 0.1))
                    + math.exp(-((nx - 0.98) ** 2 / 0.045 + (ny - 0.86) ** 2 / 0.1)))
            top = math.exp(-((nx - 0.93) ** 2 / 0.07 + ny ** 2 / 0.04))
            pixels[x, y] = (int(14 + 61 * warm + 33 * top),
                            int(15 + 12 * warm + 9 * top),
                            int(39 + 7 * ny + 3 * top))
    image.paste(gradient.resize(image.size, Image.Resampling.BICUBIC).convert("RGBA"))

    art = _Art(image, scale)
    _lights(art, 0, width * 0.27)
    _lights(art, width * 0.73, width)
    _firework(art, width * 0.262, 59, 39, (235, 88, 148))
    _firework(art, width * 0.738, 43, 31, (245, 174, 69))
    _firework(art, 23, height * 0.48, 42, (199, 127, 68))
    _firework(art, width - 19, height * 0.62, 48, (199, 127, 68))
    for dx, top, size in ((190, 28, 68), (111, 12, 49), (48, 30, 42)):
        _lantern(art, width - dx, top, size)

    # Symmetric sparks along the margins never cross into the white tables.
    for side in (-1, 1):
        x = 20 if side < 0 else width - 20
        for y in range(210, int(height - footer_height), 84):
            art.polygon([(x, y - 5), (x + 3, y), (x, y + 5), (x - 3, y)],
                        (218, 164, 68, 140))
    art.finish()


def draw_diwali_footer(image, *, scale=1, height=SUMMARY_FOOTER_H):
    width, bottom = image.width / scale, image.height / scale
    art = _Art(image, scale)
    art.line([(36, bottom - height + 6), (width - 36, bottom - height + 6)],
             (192, 137, 53, 100))
    _rangoli(art, width / 2, bottom - 12, min(220, width * 0.16))
    _diya(art, width / 2, bottom - 37, 33)
    for x, size in ((width * 0.1, 24), (width * 0.18, 18),
                    (width * 0.82, 18), (width * 0.9, 24)):
        _diya(art, x, bottom - 27, size)
    for i in range(57):
        x = 45 + (width - 90) * i / 56
        y = bottom - 14 - (i % 5) * 4
        art.ellipse(x, y, 1.2, 1.2, (*PALE_GOLD, 110 + (i % 3) * 55))
    art.finish()


def frame_innings_card(image):
    """Add festive space around an innings card without covering its content."""
    scale = 2
    pad, top, footer = 36, 80, 100
    width, height = image.width + 2 * pad, image.height + top + footer
    framed = Image.new("RGBA", (width * scale, height * scale))
    draw_diwali_background(framed, scale=scale, footer_height=footer)
    framed.alpha_composite(image.convert("RGBA").resize(
        (image.width * scale, image.height * scale), Image.Resampling.LANCZOS),
        (pad * scale, top * scale))
    art = _Art(framed, scale)
    art.draw.rounded_rectangle(art.box(pad - 4, top - 4, width - pad + 4,
                                     top + image.height + 4),
                               radius=24 * scale, outline=(*GOLD, 255), width=2 * scale)
    art.finish()
    # Reuse the bundled script font rather than a festival emoji glyph.
    path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Caveat-Bold.ttf"
    try:
        font = ImageFont.truetype(str(path), 42 * scale)
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(framed)
    draw.text((width * scale // 2, 42 * scale), "Happy Diwali", anchor="mm",
              font=font, fill=(*PALE_GOLD, 255))
    draw_diwali_footer(framed, scale=scale, height=footer)
    return framed.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
