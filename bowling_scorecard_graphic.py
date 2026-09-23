"""
Cricket Bowling Scorecard Graphic Generator
Converted from the uploaded Bowling Scorecard HTML.

Run:
    python bowling_scorecard_graphic.py

Output:
    bowling-scorecard.png

Optional:
    Put a logo image named logo.png in the same folder as this script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter


# -----------------------------
# EDIT SCORECARD DATA HERE
# -----------------------------
WIDTH, HEIGHT = 1600, 900
OUTPUT_FILE = "bowling-scorecard.png"

LEFT_TEAM = "DADDYALEC"
RIGHT_TEAM = "VERY TOUCHABLES"
BOWLING_TEAM = LEFT_TEAM

BOWLING_ROWS = [
    ("GLENN MCGRATH", "2.5", "1", "34", "1", "12.0"),
    ("MITCHELL STARC", "3.0", "5", "37", "0", "12.33"),
    ("JAY DIJKSTRA", "4.0", "5", "42", "1", "10.5"),
    ("AMELIA KERR", "3.0", "6", "38", "1", "12.67"),
    ("SHANE WARNE", "4.0", "5", "31", "1", "7.75"),
]

FALL_OF_WICKETS = ["9", "85", "125", "137", "", "", "", "", "", ""]

BOTTOM_STATS = [
    ("11.0", "RUN-RATE"),
    ("3", "EXTRAS"),
    ("16.5", "OVERS"),
    ("185/4", ""),
]


# -----------------------------
# COLORS
# -----------------------------
WHITE = (244, 246, 251, 255)
TEXT = (242, 244, 248, 255)
MUTED_TEXT = (231, 234, 240, 255)
GOLD = (255, 197, 91, 255)
GREEN = (54, 219, 120, 255)
GREEN_DARK = (7, 20, 13, 255)
RED = (255, 35, 45, 255)
BLUE = (0, 170, 255, 255)
CARD_BG = (5, 12, 20, 235)
PANEL_BG = (6, 15, 25, 220)


# -----------------------------
# FONT HELPERS
# -----------------------------
_ROOT_DIR = Path(__file__).resolve().parent
_FONT_DIR = _ROOT_DIR / "assets" / "fonts"

_BRICOLAGE_FONT_CANDIDATES = (
    _FONT_DIR / "BricolageGrotesque-SemiBold.ttf",
    _FONT_DIR / "BricolageGrotesque-Bold.ttf",
    _FONT_DIR / "BricolageGrotesque-ExtraBold.ttf",
    _FONT_DIR / "BricolageGrotesque.ttf",
)


def first_existing(paths: Sequence[str | Path]) -> str | None:
    for p in paths:
        if Path(p).exists():
            return str(p)
    return None


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a body font. Falls back safely if a custom font is unavailable."""
    candidates = []
    if bold:
        candidates.extend(_BRICOLAGE_FONT_CANDIDATES)
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            ]
        )
    candidates.extend(
        [
            *_BRICOLAGE_FONT_CANDIDATES,
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        ]
    )
    found = first_existing(candidates)
    if found:
        return ImageFont.truetype(found, size=size)
    return ImageFont.load_default()


def fit_font(text: str, max_width: int, start_size: int, min_size: int = 12) -> ImageFont.ImageFont:
    size = start_size
    while size >= min_size:
        f = font(size)
        box = ImageDraw.Draw(Image.new("RGBA", (10, 10))).textbbox((0, 0), text, font=f)
        if box[2] - box[0] <= max_width:
            return f
        size -= 2
    return font(min_size)


# -----------------------------
# DRAWING HELPERS
# -----------------------------
def clamp(value: int) -> int:
    return max(0, min(255, int(value)))


def mix(c1: Tuple[int, int, int, int], c2: Tuple[int, int, int, int], t: float) -> Tuple[int, int, int, int]:
    return tuple(clamp(c1[i] + (c2[i] - c1[i]) * t) for i in range(4))  # type: ignore[return-value]


def rounded_gradient(
    base: Image.Image,
    box: Tuple[int, int, int, int],
    radius: int,
    left_color: Tuple[int, int, int, int],
    mid_color: Tuple[int, int, int, int],
    right_color: Tuple[int, int, int, int],
    border: Tuple[int, int, int, int] | None = None,
    border_width: int = 1,
) -> None:
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    grad = Image.new("RGBA", (w, h))
    pix = grad.load()
    for x in range(w):
        p = x / max(1, w - 1)
        if p < 0.5:
            c = mix(left_color, mid_color, p * 2)
        else:
            c = mix(mid_color, right_color, (p - 0.5) * 2)
        for y in range(h):
            pix[x, y] = c
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(grad, (x1, y1), mask)
    base.alpha_composite(layer)
    if border:
        d = ImageDraw.Draw(base)
        d.rounded_rectangle((x1, y1, x2, y2), radius=radius, outline=border, width=border_width)


def draw_shadow_text(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    fnt: ImageFont.ImageFont,
    fill: Tuple[int, int, int, int] = WHITE,
    anchor: str = "mm",
    shadow_offset: Tuple[int, int] = (0, 5),
    shadow_fill: Tuple[int, int, int, int] = (0, 0, 0, 130),
    stroke_width: int = 0,
    stroke_fill: Tuple[int, int, int, int] = (0, 0, 0, 160),
) -> None:
    x, y = xy
    draw.text((x + shadow_offset[0], y + shadow_offset[1]), text, font=fnt, fill=shadow_fill, anchor=anchor)
    draw.text((x, y), text, font=fnt, fill=fill, anchor=anchor, stroke_width=stroke_width, stroke_fill=stroke_fill)


def draw_glow_rect(
    image: Image.Image,
    box: Tuple[int, int, int, int],
    radius: int,
    color: Tuple[int, int, int, int],
    blur: int = 18,
) -> None:
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(box, radius=radius, fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    image.alpha_composite(glow)


def draw_corner_stripes(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    side: str,
    color: Tuple[int, int, int, int],
) -> None:
    """Draw angular broadcast-style corner blocks."""
    x1, y1, x2, y2 = box
    if side == "left":
        poly = [(x1, y2), (x1 + 120, y2), (x1 + 55, y1 + 15), (x1, y1 + 15)]
        draw.polygon(poly, fill=(color[0], color[1], color[2], 220))
        for i in range(-20, 135, 14):
            draw.line((x1 + i, y2, x1 + i + 70, y1 + 15), fill=(color[0], color[1], color[2], 75), width=2)
    else:
        poly = [(x2, y2), (x2 - 140, y2), (x2 - 62, y1 + 15), (x2, y1 + 15)]
        draw.polygon(poly, fill=(color[0], color[1], color[2], 220))
        for i in range(-20, 155, 14):
            draw.line((x2 - i, y2, x2 - i - 70, y1 + 15), fill=(color[0], color[1], color[2], 75), width=2)


def draw_logo(image: Image.Image, box: Tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    d = ImageDraw.Draw(image)
    script_dir = Path(__file__).resolve().parent
    logo_path = script_dir / "logo.png"
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((x2 - x1 - 16, y2 - y1 - 16), Image.Resampling.LANCZOS)
            lx = x1 + (x2 - x1 - logo.width) // 2
            ly = y1 + (y2 - y1 - logo.height) // 2
            shadow = Image.new("RGBA", logo.size, (0, 0, 0, 120)).filter(ImageFilter.GaussianBlur(8))
            image.paste(shadow, (lx + 3, ly + 5), logo.split()[-1])
            image.paste(logo, (lx, ly), logo.split()[-1])
            return
        except Exception:
            pass

    # Fallback logo placeholder
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    r = min(x2 - x1, y2 - y1) // 2 - 18
    draw_glow_rect(image, (cx - r, cy - r, cx + r, cy + r), r, (255, 255, 255, 28), blur=12)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(14, 25, 42, 255), outline=(255, 255, 255, 75), width=2)
    d.arc((cx - r + 16, cy - r + 16, cx + r - 16, cy + r - 16), 295, 65, fill=(255, 55, 65, 240), width=5)
    d.arc((cx - r + 16, cy - r + 16, cx + r - 16, cy + r - 16), 115, 245, fill=(0, 170, 255, 240), width=5)
    draw_shadow_text(d, (cx, cy - 4), "LOGO", font(28), fill=(240, 242, 246, 255), anchor="mm")


def create_background() -> Image.Image:
    img = Image.new("RGBA", (WIDTH, HEIGHT), (2, 4, 7, 255))
    pix = img.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            t = (x + y) / (WIDTH + HEIGHT)
            base = mix((2, 4, 7, 255), (6, 17, 29, 255), t)
            pix[x, y] = base

    # red and blue radial glows
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    opix = overlay.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            d1 = ((x - 0) ** 2 + (y - 0) ** 2) ** 0.5
            d2 = ((x - WIDTH) ** 2 + (y - 0) ** 2) ** 0.5
            a1 = max(0, 1 - d1 / 460)
            a2 = max(0, 1 - d2 / 500)
            r, g, b, a = 0, 0, 0, 0
            if a1 > 0:
                r += int(255 * a1)
                g += int(30 * a1)
                b += int(45 * a1)
                a += int(90 * a1)
            if a2 > 0:
                r += int(0 * a2)
                g += int(150 * a2)
                b += int(255 * a2)
                a += int(90 * a2)
            if a:
                opix[x, y] = (clamp(r), clamp(g), clamp(b), clamp(a))
    img.alpha_composite(overlay)
    return img


def draw_team_highlight(
    image: Image.Image,
    box: Tuple[int, int, int, int],
    name: str,
    active: bool = False,
    align: str = "left",
) -> None:
    x1, y1, x2, y2 = box
    d = ImageDraw.Draw(image)
    if active:
        draw_glow_rect(image, box, 22, (62, 255, 149, 35), blur=20)
        rounded_gradient(
            image,
            box,
            22,
            (46, 210, 113, 62),
            (255, 255, 255, 12),
            (18, 115, 62, 46),
            border=(95, 255, 165, 110),
            border_width=1,
        )
        tag_box = (x1 + 20, y1 - 12, x1 + 132, y1 + 18)
        d.rounded_rectangle(tag_box, radius=15, fill=(54, 219, 120, 255))
        draw_shadow_text(d, ((tag_box[0] + tag_box[2]) // 2, (tag_box[1] + tag_box[3]) // 2 + 1), "BOWLING", font(14), fill=GREEN_DARK, anchor="mm", shadow_offset=(0, 0), shadow_fill=(0, 0, 0, 0))
    else:
        rounded_gradient(
            image,
            box,
            22,
            (255, 255, 255, 8),
            (255, 255, 255, 5),
            (255, 255, 255, 8),
            border=(255, 255, 255, 32),
        )

    team_font = fit_font(name, x2 - x1 - 44, 66, 38)
    draw_shadow_text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 6), name, team_font, fill=(241, 242, 245, 255), anchor="mm")


def draw_header(image: Image.Image) -> None:
    d = ImageDraw.Draw(image)
    x, y, w, h = 14, 14, 1572, 130
    rounded_gradient(
        image,
        (x, y, x + w, y + h),
        18,
        (255, 35, 45, 45),
        (8, 16, 27, 220),
        (0, 145, 255, 50),
        border=(255, 255, 255, 38),
    )
    left_w, logo_w = 696, 180
    left_box = (x, y, x + left_w, y + h)
    logo_box = (x + left_w, y, x + left_w + logo_w, y + h)
    right_box = (x + left_w + logo_w, y, x + w, y + h)

    # separators
    d.line((logo_box[0], y, logo_box[0], y + h), fill=(255, 255, 255, 30), width=1)
    d.line((logo_box[2], y, logo_box[2], y + h), fill=(255, 255, 255, 30), width=1)
    rounded_gradient(image, logo_box, 0, (0, 0, 0, 25), (255, 255, 255, 8), (0, 0, 0, 25), border=None)

    draw_corner_stripes(d, (x, y + 60, x + 140, y + h), "left", RED)
    draw_corner_stripes(d, (x + w - 140, y + 60, x + w, y + h), "right", BLUE)

    highlight_y = y + 19
    draw_team_highlight(image, (x + 40, highlight_y, x + 460, highlight_y + 92), LEFT_TEAM, active=(BOWLING_TEAM == LEFT_TEAM))
    draw_team_highlight(image, (x + w - 40 - 420, highlight_y, x + w - 40, highlight_y + 92), RIGHT_TEAM, active=(BOWLING_TEAM == RIGHT_TEAM), align="right")
    draw_logo(image, (logo_box[0] + 10, logo_box[1] + 8, logo_box[2] - 10, logo_box[3] - 8))


def draw_table(image: Image.Image) -> None:
    d = ImageDraw.Draw(image)
    x, y, w, h = 14, 152, 1572, 455
    rounded_gradient(
        image,
        (x, y, x + w, y + h),
        16,
        (255, 50, 60, 25),
        (6, 15, 25, 210),
        (0, 150, 255, 30),
        border=(255, 255, 255, 46),
    )
    headers = ["", "OVERS", "DOTS", "RUNS", "WICKETS", "ECONOMY"]
    col_w = [590, 190, 170, 170, 220, 232]
    header_h = 46
    row_h = 51

    # header row
    cur_x = x
    for i, cw in enumerate(col_w):
        d.rectangle((cur_x, y, cur_x + cw, y + header_h), fill=(255, 255, 255, 25), outline=(255, 255, 255, 35))
        if headers[i]:
            fnt = fit_font(headers[i], cw - 18, 25, 18)
            draw_shadow_text(d, (cur_x + cw // 2, y + header_h // 2 + 2), headers[i], fnt, fill=(241, 244, 250, 255), anchor="mm", shadow_offset=(0, 3))
        cur_x += cw

    all_rows = list(BOWLING_ROWS)[:5]
    while len(all_rows) < 8:
        all_rows.append(("-", "-", "-", "-", "-", "-"))

    table_font = font(30)
    name_font = font(31)
    for r, row in enumerate(all_rows):
        row_y = y + header_h + r * row_h
        data_row = r < len(BOWLING_ROWS)
        # subtle row background
        fill = (255, 255, 255, 10) if data_row else (255, 255, 255, 3)
        d.rectangle((x, row_y, x + w, row_y + row_h), fill=fill)
        if data_row:
            d.rectangle((x, row_y, x + 4, row_y + row_h), fill=(255, 70, 75, 230))
            d.rectangle((x + w - 4, row_y, x + w, row_y + row_h), fill=(0, 170, 255, 230))
        cur_x = x
        for c, cw in enumerate(col_w):
            d.rectangle((cur_x, row_y, cur_x + cw, row_y + row_h), outline=(255, 255, 255, 35), width=1)
            value = row[c]
            if data_row:
                if c == 0:
                    fnt = fit_font(value, cw - 70, 31, 18)
                    draw_shadow_text(d, (cur_x + 36, row_y + row_h // 2 + 2), value, fnt, fill=TEXT, anchor="lm", shadow_offset=(0, 3))
                else:
                    draw_shadow_text(d, (cur_x + cw // 2, row_y + row_h // 2 + 2), value, table_font, fill=TEXT, anchor="mm", shadow_offset=(0, 3))
            cur_x += cw


def draw_fall_of_wickets(image: Image.Image) -> None:
    d = ImageDraw.Draw(image)
    x, y, w, h = 14, 619, 1572, 155
    rounded_gradient(
        image,
        (x, y, x + w, y + h),
        16,
        (255, 255, 255, 18),
        (5, 14, 25, 210),
        (0, 150, 255, 18),
        border=(255, 255, 255, 46),
    )
    draw_shadow_text(d, (x + 12, y + 30), "FALL OF WICKETS", font(28), fill=(244, 246, 251, 255), anchor="lm", shadow_offset=(0, 3))

    grid_x, grid_y, grid_w, grid_h = x + 12, y + 55, w - 24, 88
    d.rounded_rectangle((grid_x, grid_y, grid_x + grid_w, grid_y + grid_h), radius=10, outline=(255, 255, 255, 42), width=1)
    cell_w = grid_w / 10
    ordinals = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th"]
    for i in range(10):
        cx1 = int(grid_x + i * cell_w)
        cx2 = int(grid_x + (i + 1) * cell_w)
        d.rectangle((cx1, grid_y, cx2, grid_y + grid_h), fill=(255, 255, 255, 11))
        if i != 9:
            d.line((cx2, grid_y, cx2, grid_y + grid_h), fill=(255, 255, 255, 48), width=1)
        d.line((cx1, grid_y + 38, cx2, grid_y + 38), fill=(255, 255, 255, 42), width=1)
        draw_shadow_text(d, ((cx1 + cx2) // 2, grid_y + 20), ordinals[i], font(22), fill=GOLD, anchor="mm", shadow_offset=(0, 2))
        score = FALL_OF_WICKETS[i] if i < len(FALL_OF_WICKETS) else ""
        if score:
            draw_shadow_text(d, ((cx1 + cx2) // 2, grid_y + 64), score, font(35), fill=TEXT, anchor="mm", shadow_offset=(0, 3))


def draw_bottom_bar(image: Image.Image) -> None:
    d = ImageDraw.Draw(image)
    x, y, w, h = 14, 786, 1572, 100
    rounded_gradient(
        image,
        (x, y, x + w, y + h),
        18,
        (255, 30, 45, 45),
        (5, 13, 23, 225),
        (0, 150, 255, 50),
        border=(255, 255, 255, 58),
    )
    draw_corner_stripes(d, (x, y + 30, x + 95, y + h), "left", RED)
    draw_corner_stripes(d, (x + w - 105, y + 30, x + w, y + h), "right", BLUE)

    col_w = [370, 370, 370, 462]
    cur_x = x
    for i, cw in enumerate(col_w):
        if i:
            d.line((cur_x, y, cur_x, y + h), fill=(255, 255, 255, 45), width=1)
        value, label = BOTTOM_STATS[i]
        value_size = 76 if i == 3 else 68
        value_font = fit_font(value, cw - 80 if label else cw - 40, value_size, 34)
        if label:
            total_text_w = 0
            vb = d.textbbox((0, 0), value, font=value_font)
            lb_font = fit_font(label, 170, 26, 16)
            lb = d.textbbox((0, 0), label, font=lb_font)
            total_text_w = (vb[2] - vb[0]) + 26 + (lb[2] - lb[0])
            start_x = cur_x + (cw - total_text_w) // 2
            draw_shadow_text(d, (start_x, y + h // 2 + 4), value, value_font, fill=(244, 245, 247, 255), anchor="lm", shadow_offset=(0, 5))
            draw_shadow_text(d, (start_x + (vb[2] - vb[0]) + 26, y + h // 2 + 5), label, lb_font, fill=MUTED_TEXT, anchor="lm", shadow_offset=(0, 3))
        else:
            draw_shadow_text(d, (cur_x + cw // 2, y + h // 2 + 6), value, value_font, fill=(244, 245, 247, 255), anchor="mm", shadow_offset=(0, 5))
        cur_x += cw


def draw_outer_frame(image: Image.Image) -> None:
    d = ImageDraw.Draw(image)
    # main card background over the body background
    rounded_gradient(
        image,
        (0, 0, WIDTH - 1, HEIGHT - 1),
        24,
        (255, 30, 45, 28),
        CARD_BG,
        (0, 145, 255, 32),
        border=(255, 255, 255, 56),
        border_width=1,
    )
    d.rounded_rectangle((8, 8, WIDTH - 9, HEIGHT - 9), radius=20, outline=(255, 255, 255, 34), width=1)


def create_scorecard() -> Image.Image:
    image = create_background()
    draw_outer_frame(image)
    draw_header(image)
    draw_table(image)
    draw_fall_of_wickets(image)
    draw_bottom_bar(image)
    # Flatten all translucent drawing onto a dark background for consistent PNG viewing.
    flattened = Image.new("RGBA", image.size, (2, 4, 7, 255))
    flattened.alpha_composite(image)
    return flattened.convert("RGB")


def main() -> None:
    image = create_scorecard()
    out_path = Path(__file__).resolve().parent / OUTPUT_FILE
    image.save(out_path, quality=95)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
