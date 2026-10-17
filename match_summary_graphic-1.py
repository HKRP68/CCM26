"""
Match Summary Graphic Generator

This Python file recreates the uploaded match-summary HTML layout as a PNG image.
It uses Pillow only, so it can run without a browser.

Install dependency:
    pip install pillow

Run:
    python match_summary_graphic.py

Optional:
    python match_summary_graphic.py --logo logo.png --output match-summary.png
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError as exc:
    raise SystemExit("Pillow is required. Install it with: pip install pillow") from exc


W, H = 1600, 900
SCALE = 2  # Draw at 2x then downsample for smoother edges.


@dataclass
class Innings:
    team: str
    overs: str
    score: str
    batting_theme: str
    bowling_theme: str
    batters: list[tuple[str, str, str]]
    bowlers: list[tuple[str, str, str, bool]]


MATCH = {
    "left_title": "SUMMARY",
    "right_title": "MATCH #118722",
    "stadium": "WANDERERS STADIUM",
    "result": "DADDYALEC WON BY 6 WICKETS",
    "potm": "BALLA STEYN",
    "potm_performance": "4/25 (4 OVERS)",
}

INNINGS = [
    Innings(
        team="VERY TOUCHABLES",
        overs="OVERS 20.0/20.0",
        score="184/8",
        batting_theme="blue",
        bowling_theme="red",
        batters=[
            ("KUSAL MENDIS", "68", "38"),
            ("GABY LEWIS", "27*", "18"),
            ("HARMANPREET KAUR", "19", "15"),
            ("SHUBMAN GILL", "18", "7"),
        ],
        bowlers=[
            ("BALLA STEYN", "4-25", "4", True),
            ("JOSH HAZLEWOOD", "2-30", "4", False),
            ("MITCHELL STARC", "1-42", "4", False),
            ("AMELIA KERR", "1-44", "4", False),
        ],
    ),
    Innings(
        team="DADDYALEC",
        overs="OVERS 16.5/20.0",
        score="185/4",
        batting_theme="red",
        bowling_theme="blue",
        batters=[
            ("VIRAT KOHLI", "79", "41"),
            ("SHREYAS IYER", "27", "15"),
            ("SAI SUDHARSAN", "25", "21"),
            ("SACHIN TENDULKAR", "24*", "10"),
        ],
        bowlers=[
            ("SHANE WARNE", "1-31", "4", False),
            ("GLENN MCGRATH", "1-34", "2.5", False),
            ("AMELIA KERR", "1-38", "3", False),
            ("JAY DIJKSTRA", "1-42", "4", False),
        ],
    ),
]

COLORS = {
    "bg_dark": (5, 7, 12, 255),
    "card": (7, 11, 18, 255),
    "panel": (8, 14, 23, 242),
    "white": (245, 247, 250, 255),
    "soft": (220, 226, 236, 255),
    "gold": (255, 208, 79, 255),
    "gold_dark": (255, 158, 27, 255),
    "red": (255, 70, 50, 255),
    "blue": (0, 165, 255, 255),
    "line": (255, 255, 255, 35),
    "line_strong": (255, 255, 255, 65),
}


def scaled(v: int | float) -> int:
    return int(round(v * SCALE))


def rgba(color: tuple[int, int, int, int], alpha: Optional[int] = None):
    if alpha is None:
        return color
    return color[:3] + (alpha,)


_ROOT_DIR = Path(__file__).resolve().parent
_FONT_DIR = _ROOT_DIR / "assets" / "fonts"

_BEBAS_FONT_CANDIDATES = (
    _FONT_DIR / "BebasNeue-Regular.ttf",
)
_BRICOLAGE_FONT_CANDIDATES = (
    _FONT_DIR / "BricolageGrotesque-SemiBold.ttf",
    _FONT_DIR / "BricolageGrotesque-Bold.ttf",
    _FONT_DIR / "BricolageGrotesque-ExtraBold.ttf",
    _FONT_DIR / "BricolageGrotesque.ttf",
)


def font(size: int, condensed: bool = False) -> ImageFont.FreeTypeFont:
    """Load bundled scorecard fonts before falling back to system fonts."""
    candidates = []
    if condensed:
        candidates.extend(_BEBAS_FONT_CANDIDATES)
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ])
    else:
        candidates.extend(_BRICOLAGE_FONT_CANDIDATES)
    candidates.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ])
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(str(path), scaled(size))
    return ImageFont.load_default()


TITLE_FONT = font(62, condensed=True)
TITLE_FONT_SMALL = font(44, condensed=True)
TEXT_FONT = font(28)
TEXT_FONT_SMALL = font(22)
TEXT_FONT_TINY = font(16)
ROW_FONT = font(27)
ROW_FONT_SMALL = font(22)
SCORE_FONT = font(58, condensed=True)
POTM_FONT = font(48, condensed=True)


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int = 14, condensed: bool = False):
    for size in range(start_size, min_size - 1, -1):
        fnt = font(size, condensed=condensed)
        if text_size(draw, text, fnt)[0] <= max_width:
            return fnt
    return font(min_size, condensed=condensed)


def draw_text_center(draw, box, text, fnt, fill=COLORS["white"], shadow=True):
    x1, y1, x2, y2 = [scaled(v) for v in box]
    tw, th = text_size(draw, text, fnt)
    x = x1 + (x2 - x1 - tw) // 2
    y = y1 + (y2 - y1 - th) // 2 - scaled(2)
    if shadow:
        draw.text((x, y + scaled(4)), text, font=fnt, fill=(0, 0, 0, 155))
        draw.text((x, y), text, font=fnt, fill=fill)
        draw.text((x, y - scaled(1)), text, font=fnt, fill=rgba(fill, 220))
    else:
        draw.text((x, y), text, font=fnt, fill=fill)


def draw_text_left(draw, xy, text, fnt, fill=COLORS["white"], shadow=True):
    x, y = scaled(xy[0]), scaled(xy[1])
    if shadow:
        draw.text((x, y + scaled(3)), text, font=fnt, fill=(0, 0, 0, 135))
    draw.text((x, y), text, font=fnt, fill=fill)


def rounded_rect(draw, box, radius, fill, outline=None, width=1):
    box_s = tuple(scaled(v) for v in box)
    draw.rounded_rectangle(box_s, radius=scaled(radius), fill=fill, outline=outline, width=scaled(width))


def overlay_rounded(img: Image.Image, box, radius, fill, outline=None, width=1):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    rounded_rect(d, box, radius, fill, outline, width)
    img.alpha_composite(ov)


def draw_linear_gradient(img: Image.Image, box, left, middle, right):
    x1, y1, x2, y2 = [scaled(v) for v in box]
    width = max(1, x2 - x1)
    grad = Image.new("RGBA", (width, y2 - y1), (0, 0, 0, 0))
    pix = grad.load()
    for x in range(width):
        t = x / max(1, width - 1)
        if t < 0.5:
            p = t / 0.5
            c1, c2 = left, middle
        else:
            p = (t - 0.5) / 0.5
            c1, c2 = middle, right
        color = tuple(int(c1[i] * (1 - p) + c2[i] * p) for i in range(4))
        for y in range(y2 - y1):
            pix[x, y] = color
    img.alpha_composite(grad, (x1, y1))


def make_background() -> Image.Image:
    img = Image.new("RGBA", (scaled(W), scaled(H)), COLORS["bg_dark"])
    # Main diagonal-like dark gradient.
    pix = img.load()
    for y in range(img.height):
        for x in range(img.width):
            tx = x / img.width
            ty = y / img.height
            mix = (tx * 0.45 + ty * 0.55)
            r = int(4 + mix * 7)
            g = int(7 + mix * 11)
            b = int(11 + mix * 18)
            pix[x, y] = (r, g, b, 255)

    # Soft red and blue corner glows.
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((scaled(-120), scaled(-160), scaled(620), scaled(420)), fill=(255, 60, 40, 48))
    gd.ellipse((scaled(980), scaled(-170), scaled(1720), scaled(420)), fill=(0, 170, 255, 48))
    glow = glow.filter(ImageFilter.GaussianBlur(scaled(70)))
    img.alpha_composite(glow)
    return img


def draw_card_shell(img: Image.Image):
    d = ImageDraw.Draw(img)
    overlay_rounded(img, (10, 10, 1590, 890), 24, (7, 11, 18, 245), COLORS["line_strong"], 1)
    overlay_rounded(img, (18, 18, 1582, 882), 18, (255, 255, 255, 5), COLORS["line"], 1)

    # Subtle side color wash.
    wash = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_linear_gradient(wash, (10, 10, 1590, 890), (255, 40, 40, 22), (255, 255, 255, 4), (0, 170, 255, 22))
    img.alpha_composite(wash)

    # A few thin stadium-like rays.
    for i in range(-160, 1700, 135):
        d.line((scaled(i), scaled(890), scaled(i + 280), scaled(10)), fill=(255, 255, 255, 8), width=scaled(2))


def draw_header(img: Image.Image, logo_path: Optional[str] = None):
    d = ImageDraw.Draw(img)
    x, y, w, h = 22, 22, 1556, 112
    overlay_rounded(img, (x, y, x + w, y + h), 18, (8, 12, 18, 242), COLORS["line_strong"], 1)
    draw_linear_gradient(img, (x, y, x + w, y + h), (255, 40, 40, 25), (255, 255, 255, 7), (0, 170, 255, 26))

    # Center logo panel.
    logo_x1, logo_x2 = 710, 890
    d.line((scaled(logo_x1), scaled(y), scaled(logo_x1), scaled(y + h)), fill=COLORS["line"], width=scaled(1))
    d.line((scaled(logo_x2), scaled(y), scaled(logo_x2), scaled(y + h)), fill=COLORS["line"], width=scaled(1))
    d.rectangle((scaled(logo_x1), scaled(y), scaled(logo_x2), scaled(y + h)), fill=(255, 255, 255, 7))

    # Accent corner blocks.
    d.polygon([(scaled(x), scaled(y + h)), (scaled(x + 145), scaled(y + h)), (scaled(x + 92), scaled(y + 43))], fill=(255, 70, 50, 170))
    d.polygon([(scaled(x + w), scaled(y + h)), (scaled(x + w - 145), scaled(y + h)), (scaled(x + w - 92), scaled(y + 43))], fill=(0, 170, 255, 170))

    # Titles.
    left_font = fit_font(d, MATCH["left_title"], scaled(570), 62, 36, condensed=True)
    right_font = fit_font(d, MATCH["right_title"], scaled(620), 62, 34, condensed=True)
    draw_text_center(d, (95, y, 635, y + h), MATCH["left_title"], left_font)
    draw_text_center(d, (965, y, 1515, y + h), MATCH["right_title"], right_font)

    # Logo image or placeholder.
    logo_box = (732, 34, 868, 122)
    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert("RGBA")
            max_w = scaled(136)
            max_h = scaled(88)
            logo.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            lx = scaled((logo_box[0] + logo_box[2]) / 2) - logo.width // 2
            ly = scaled((logo_box[1] + logo_box[3]) / 2) - logo.height // 2
            shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            shadow.alpha_composite(logo, (lx, ly))
            shadow = shadow.filter(ImageFilter.GaussianBlur(scaled(5)))
            img.alpha_composite(shadow)
            img.alpha_composite(logo, (lx, ly))
            return
        except Exception:
            pass

    overlay_rounded(img, (735, 39, 865, 117), 16, (255, 255, 255, 12), (255, 255, 255, 55), 1)
    draw_text_center(d, (735, 45, 865, 112), "LOGO", font(30, condensed=True), COLORS["soft"])


def draw_stadium_bar(img: Image.Image, y: int):
    d = ImageDraw.Draw(img)
    overlay_rounded(img, (22, y, 1578, y + 38), 16, (10, 14, 20, 235), (255, 190, 80, 55), 1)
    draw_linear_gradient(img, (22, y, 1578, y + 38), (255, 120, 40, 35), (255, 255, 255, 7), (255, 190, 70, 35))
    d.line((scaled(605), scaled(y + 2), scaled(670), scaled(y + 36)), fill=(255, 190, 80, 90), width=scaled(4))
    d.line((scaled(995), scaled(y + 2), scaled(930), scaled(y + 36)), fill=(255, 190, 80, 90), width=scaled(4))
    fnt = fit_font(d, MATCH["stadium"], scaled(760), 24, 14, condensed=True)
    draw_text_center(d, (400, y, 1200, y + 38), MATCH["stadium"], fnt, COLORS["soft"])


def theme_rgba(theme: str, alpha: int) -> tuple[int, int, int, int]:
    return rgba(COLORS["red"] if theme == "red" else COLORS["blue"], alpha)


def draw_row(img: Image.Image, box, theme: str, columns: tuple[str, str, str], potm: bool = False, right_accent=False):
    d = ImageDraw.Draw(img)
    x1, y1, x2, y2 = box
    col = COLORS["red"] if theme == "red" else COLORS["blue"]
    # Background
    draw_linear_gradient(img, box, rgba(col, 38), rgba(col, 10), rgba(col, 5))
    d.rectangle((scaled(x1), scaled(y1), scaled(x2), scaled(y2)), outline=(255, 255, 255, 22), width=scaled(1))
    if right_accent:
        d.rectangle((scaled(x2 - 4), scaled(y1), scaled(x2), scaled(y2)), fill=rgba(col, 230))
    else:
        d.rectangle((scaled(x1), scaled(y1), scaled(x1 + 4), scaled(y2)), fill=rgba(col, 230))

    name, v1, v2 = columns
    name_area = (x1 + 18, y1, x2 - 205, y2)
    fnt = fit_font(d, name, scaled((x2 - 205) - (x1 + 18)), 27, 16)
    _, th = text_size(d, name, fnt)
    ny = scaled(y1 + (y2 - y1) / 2) - th // 2 - scaled(1)
    nx = scaled(x1 + 18)
    d.text((nx, ny + scaled(3)), name, font=fnt, fill=(0, 0, 0, 130))
    d.text((nx, ny), name, font=fnt, fill=COLORS["white"])

    # POTM mini chip
    if potm:
        tw, _ = text_size(d, name, fnt)
        chip_x = min(nx + tw + scaled(12), scaled(x2 - 315))
        chip_y = scaled(y1 + 13)
        d.rounded_rectangle((chip_x, chip_y, chip_x + scaled(72), chip_y + scaled(25)), radius=scaled(6), fill=COLORS["gold"], outline=(255, 230, 140, 200), width=scaled(1))
        d.text((chip_x + scaled(9), chip_y + scaled(4)), "POTM", font=TEXT_FONT_TINY, fill=(30, 18, 0, 255))

    for cx1, cx2, text in [(x2 - 205, x2 - 95, v1), (x2 - 95, x2 - 10, v2)]:
        f = fit_font(d, text, scaled(cx2 - cx1 - 10), 26, 15, condensed=True)
        draw_text_center(d, (cx1, y1, cx2, y2), text, f, COLORS["white"])


def draw_innings(img: Image.Image, y: int, innings: Innings):
    d = ImageDraw.Draw(img)
    x, w = 22, 1556
    top_h = 62
    row_h = 47
    total_h = top_h + row_h * 4
    overlay_rounded(img, (x, y, x + w, y + total_h), 18, (6, 12, 20, 239), COLORS["line_strong"], 1)

    # Top strip.
    d.rounded_rectangle((scaled(x), scaled(y), scaled(x + w), scaled(y + total_h)), radius=scaled(18), outline=COLORS["line_strong"], width=scaled(1))
    d.rectangle((scaled(x + 1), scaled(y + 1), scaled(x + w - 1), scaled(y + top_h)), fill=(255, 255, 255, 8))
    d.line((scaled(x), scaled(y + top_h), scaled(x + w), scaled(y + top_h)), fill=COLORS["line"], width=scaled(1))

    team_font = fit_font(d, innings.team, scaled(760), 48, 25, condensed=True)
    meta_font = fit_font(d, innings.overs, scaled(325), 27, 15, condensed=True)
    score_font = fit_font(d, innings.score, scaled(240), 54, 25, condensed=True)
    draw_text_left(d, (x + 18, y + 6), innings.team, team_font, COLORS["white"])
    draw_text_center(d, (1005, y, 1325, y + top_h), innings.overs, meta_font, COLORS["soft"])
    draw_text_center(d, (1325, y, x + w - 20, y + top_h), innings.score, score_font, COLORS["white"])

    # Grid divider.
    mid_x = x + w // 2
    d.line((scaled(mid_x), scaled(y + top_h), scaled(mid_x), scaled(y + total_h)), fill=COLORS["line"], width=scaled(1))

    table_y = y + top_h
    # Batting rows.
    for i, row in enumerate(innings.batters):
        by1 = table_y + i * row_h
        draw_row(img, (x, by1, mid_x, by1 + row_h), innings.batting_theme, row, False, right_accent=False)
    # Bowling rows.
    for i, row in enumerate(innings.bowlers):
        by1 = table_y + i * row_h
        name, fig, overs, potm = row
        draw_row(img, (mid_x, by1, x + w, by1 + row_h), innings.bowling_theme, (name, fig, overs), potm, right_accent=True)

    return total_h


def draw_result_bar(img: Image.Image, y: int):
    d = ImageDraw.Draw(img)
    overlay_rounded(img, (22, y, 1578, y + 60), 18, (8, 12, 18, 240), (255, 190, 80, 55), 1)
    draw_linear_gradient(img, (22, y, 1578, y + 60), (255, 60, 40, 38), (255, 255, 255, 7), (0, 170, 255, 38))
    d.polygon([(scaled(22), scaled(y + 60)), (scaled(130), scaled(y + 60)), (scaled(78), scaled(y + 17))], fill=(255, 70, 50, 150))
    d.polygon([(scaled(1578), scaled(y + 60)), (scaled(1470), scaled(y + 60)), (scaled(1522), scaled(y + 17))], fill=(0, 170, 255, 150))
    text = MATCH["result"] + "  |  TROPHY"
    fnt = fit_font(d, text, scaled(1170), 42, 22, condensed=True)
    draw_text_center(d, (185, y + 3, 1415, y + 58), text, fnt, COLORS["white"])


def draw_potm_bar(img: Image.Image, y: int):
    d = ImageDraw.Draw(img)
    h = 76
    x, w = 22, 1556
    overlay_rounded(img, (x, y, x + w, y + h), 18, (10, 10, 10, 242), (255, 180, 50, 72), 1)
    draw_linear_gradient(img, (x, y, x + w, y + h), (255, 160, 30, 42), (255, 255, 255, 7), (255, 180, 50, 42))

    c1, c2, c3 = x + 170, x + 700, x + 1370
    for xx in [c1, c2, c3]:
        d.line((scaled(xx), scaled(y), scaled(xx), scaled(y + h)), fill=COLORS["line"], width=scaled(1))

    d.rectangle((scaled(x), scaled(y), scaled(c1), scaled(y + h)), fill=(255, 120, 20, 38))
    draw_text_center(d, (x, y, c1, y + h), "POTM", font(38, condensed=True), (255, 242, 197, 255))

    name_f = fit_font(d, MATCH["potm"], scaled(c2 - c1 - 40), 44, 22, condensed=True)
    draw_text_center(d, (c1 + 16, y, c2 - 16, y + h), MATCH["potm"], name_f, COLORS["gold"])

    perf_text = "PERFORMANCE: " + MATCH["potm_performance"]
    perf_f = fit_font(d, perf_text, scaled(c3 - c2 - 40), 34, 17, condensed=True)
    draw_text_center(d, (c2 + 20, y, c3 - 20, y + h), perf_text, perf_f, COLORS["white"])

    draw_text_center(d, (c3, y, x + w, y + h), "STAR", font(30, condensed=True), COLORS["gold"])


def generate(output: str, logo: Optional[str] = None):
    img = make_background()
    draw_card_shell(img)
    draw_header(img, logo)
    y = 140
    draw_stadium_bar(img, y)
    y += 38 + 12
    for inn in INNINGS:
        y += draw_innings(img, y, inn) + 12
    y -= 2
    draw_result_bar(img, y)
    y += 60 + 10
    draw_potm_bar(img, y)

    # Composite first so semi-transparent strokes blend correctly, then downsample for clean final image.
    base = Image.new("RGBA", img.size, (4, 7, 12, 255))
    flat = Image.alpha_composite(base, img)
    final = flat.resize((W, H), Image.Resampling.LANCZOS).convert("RGB")
    final.save(output, quality=96)
    return output


def main():
    parser = argparse.ArgumentParser(description="Generate a match summary PNG graphic.")
    parser.add_argument("--output", default="match-summary.png", help="Output PNG filename")
    parser.add_argument("--logo", default="logo.png", help="Optional logo path. If missing, a LOGO placeholder is used.")
    args = parser.parse_args()

    logo = args.logo if args.logo and os.path.exists(args.logo) else None
    out = generate(args.output, logo)
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
