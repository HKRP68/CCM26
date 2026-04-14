"""Generate batsman stats card image — dark green theme with neon accents."""

import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

W, H = 654, 242

BG1 = (10, 15, 10)       # #0a0f0a
BG2 = (26, 46, 26)       # #1a2e1a
ACCENT = (0, 212, 170)   # #00d4aa
WHITE = (255, 255, 255)
DIM = (120, 150, 120)
DIVIDER = (35, 65, 35)
BADGE_BG = (20, 45, 20)


def _font(size, bold=False):
    p = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold \
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(p, size)
    except (OSError, IOError):
        return ImageFont.load_default()


def generate_batsman_card(name, rating, bat_rating, stats) -> bytes | None:
    """Generate batsman stats card.
    stats dict: bat_inns, runs, fifties, hundreds, fours, sixes,
                bat_avg, bat_sr, ducks, hs_str
    """
    try:
        img = Image.new("RGB", (W, H), BG1)
        draw = ImageDraw.Draw(img)

        # Gradient background
        for y in range(H):
            t = y / H
            r = int(BG1[0] * (1 - t) + BG2[0] * t)
            g = int(BG1[1] * (1 - t) + BG2[1] * t)
            b = int(BG1[2] * (1 - t) + BG2[2] * t)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

        # Border with neon glow effect
        draw.rounded_rectangle([1, 1, W - 2, H - 2], radius=14, outline=(0, 100, 80), width=2)
        draw.rounded_rectangle([3, 3, W - 4, H - 4], radius=12, outline=ACCENT, width=1)

        fn = _font(24, True)
        fr = _font(18, True)
        fl = _font(12)
        fv = _font(17, True)
        fs = _font(11)

        # ── Top bar: icon + name + rating badges ──────────────
        # Bat icon area
        draw.rounded_rectangle([14, 10, 42, 38], radius=6, fill=BADGE_BG)
        draw.text((17, 11), "🏏", font=_font(16), fill=WHITE)

        # Player name
        draw.text((52, 10), name.upper(), font=fn, fill=ACCENT)

        # OVR badge
        bx = W - 200
        draw.rounded_rectangle([bx, 10, bx + 80, 38], radius=6, fill=BADGE_BG)
        draw.text((bx + 6, 13), str(rating), font=fr, fill=ACCENT)
        draw.text((bx + 42, 17), "OVR", font=fs, fill=DIM)

        # BAT badge
        bx2 = W - 105
        draw.rounded_rectangle([bx2, 10, bx2 + 90, 38], radius=6, fill=BADGE_BG)
        draw.text((bx2 + 6, 13), str(bat_rating), font=fr, fill=ACCENT)
        draw.text((bx2 + 42, 17), "BAT", font=fs, fill=DIM)

        # Divider
        draw.line([(14, 48), (W - 14, 48)], fill=DIVIDER, width=1)

        # ── Stats section — 2 rows ───────────────────────────
        stat_items = [
            [("Inns", str(stats.get("bat_inns", 0))),
             ("Runs", str(stats.get("runs", 0))),
             ("50s", str(stats.get("fifties", 0))),
             ("100s", str(stats.get("hundreds", 0))),
             ("4/6", f"{stats.get('fours', 0)}/{stats.get('sixes', 0)}")],
            [("Avg", str(stats.get("bat_avg", "-"))),
             ("SR", str(stats.get("bat_sr", "-"))),
             ("Ducks", str(stats.get("ducks", 0))),
             ("HS", str(stats.get("hs_str", "-"))),
             ("", "")],
        ]

        col_w = (W - 30) // 5
        y_start = 60

        for ri, row in enumerate(stat_items):
            y = y_start + ri * 56
            for ci, (lbl, val) in enumerate(row):
                if not lbl:
                    continue
                x = 16 + ci * col_w

                # Stat value — large, white
                draw.text((x, y), val, font=fv, fill=WHITE)
                # Stat label — small, dim
                draw.text((x, y + 22), lbl.upper(), font=fs, fill=DIM)

        # Second row divider
        draw.line([(14, y_start + 52), (W - 14, y_start + 52)], fill=DIVIDER, width=1)

        # ── Bottom accent bar ─────────────────────────────────
        draw.line([(14, H - 14), (W - 14, H - 14)], fill=(0, 80, 65), width=1)

        # Export
        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()

    except Exception:
        logger.exception("Batsman card failed")
        return None
