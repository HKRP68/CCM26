"""Playing XI composite image.

Builds ONE PNG showing a user's 11 cards in a 4-4-3 grid on a dark-navy
frame, with a "PLAYING XI" header, a Team OVR badge, position numbers, and a
gold highlight on the captain.

The player cards themselves come straight from card_generator.generate_card(),
so custom uploads, website-template cards, and procedural tier cards all render
exactly as they do everywhere else in the bot. Only the surrounding frame is
new artwork.
"""

import io
import logging
import os

logger = logging.getLogger(__name__)

# Dark-navy palette, matching the bot's existing card / market aesthetic.
_BG_TOP = (17, 20, 32)
_BG_BOTTOM = (8, 9, 16)
_HEADER_BG = (24, 28, 44)
_ACCENT = (147, 197, 253)      # soft blue used elsewhere in the bot
_GOLD = (250, 204, 21)         # captain highlight (same gold as basic card)
_WHITE = (255, 255, 255)
_MUTED = (180, 180, 200)

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "assets", "fonts")

_DEFAULT_LAYOUT = (4, 4, 3)


def _font(size, *, display=False):
    """Load a bot font with the standard DejaVu fallback used by other cards."""
    from PIL import ImageFont
    candidates = []
    if display:
        candidates += [
            os.path.join(_FONT_DIR, "BebasNeue-Regular.ttf"),
            os.path.join(_FONT_DIR, "RussoOne-Regular.ttf"),
        ]
    candidates += [
        os.path.join(_FONT_DIR, "BricolageGrotesque-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _text_w(draw, text, font):
    """Return the rendered pixel width of ``text`` in ``font``."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _gradient_bg(width, height):
    """Vertical dark-navy gradient (per-row lerp, like generate_card)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), _BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(_BG_TOP[0] * (1 - t) + _BG_BOTTOM[0] * t)
        g = int(_BG_TOP[1] * (1 - t) + _BG_BOTTOM[1] * t)
        b = int(_BG_TOP[2] * (1 - t) + _BG_BOTTOM[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    return img


def build_xi_image(xi_pairs, *, team_name, captain_roster_id=None,
                   columns_layout=_DEFAULT_LAYOUT):
    """Render the Playing XI as a single PNG.

    Args:
      xi_pairs: list of up to 11 (UserRoster, Player) in display order.
      team_name: header subtitle (handle / team name / first name).
      captain_roster_id: User.captain_roster_id — the captain's UserRoster id.
      columns_layout: cards per row, default (4, 4, 3).

    Returns: PNG bytes, or None on failure.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("PIL not available")
        return None

    if not xi_pairs:
        return None

    from services.card_generator import generate_card

    # ── Fetch + open each card (defensive, like market_composite) ──
    cards = []  # (entry, player, PIL.Image)
    for entry, player in xi_pairs[:sum(columns_layout)]:
        try:
            b = generate_card(player)
            if not b:
                # Bail out rather than render a short XI with renumbered cards
                # and a Team OVR computed from incomplete data — the caller
                # falls back to the text XI when this returns None.
                logger.warning("Missing card bytes for player %s",
                               getattr(player, "id", "?"))
                return None
            img = Image.open(io.BytesIO(b)).convert("RGB")
            cards.append((entry, player, img))
        except Exception:
            logger.exception("Failed to render card for player %s",
                             getattr(player, "id", "?"))
            return None

    if not cards:
        return None

    # ── Card cell sizing ──
    # Fixed cell shape based on the standard generated card (700x420). Each card
    # is scaled to FIT inside its cell while keeping its own aspect ratio, then
    # centered — template (1536x1024), procedural (700x420), and admin custom
    # images can all differ, so we never stretch the card art.
    card_w = 260
    card_h = int(card_w * 420 / 700)
    resized = []  # (entry, player, fitted_img, off_x, off_y)
    for e, p, im in cards:
        scale = min(card_w / im.size[0], card_h / im.size[1])
        nw, nh = max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))
        fitted = im.resize((nw, nh), Image.LANCZOS)
        resized.append((e, p, fitted, (card_w - nw) // 2, (card_h - nh) // 2))

    # ── Layout geometry ──
    pad = 22                      # gap between cards
    side = 30                     # canvas side margin
    header_h = 130
    max_cols = max(columns_layout)
    grid_w = max_cols * card_w + (max_cols - 1) * pad
    width = grid_w + side * 2

    rows = list(columns_layout)
    n_rows = len(rows)
    height = header_h + n_rows * card_h + (n_rows - 1) * pad + side

    canvas = _gradient_bg(width, height)
    draw = ImageDraw.Draw(canvas)

    # ── Header band ──
    draw.rectangle([0, 0, width, header_h], fill=_HEADER_BG)
    draw.line([(0, header_h), (width, header_h)], fill=_ACCENT, width=2)

    title_font = _font(64, display=True)
    sub_font = _font(26)
    title = "PLAYING XI"
    draw.text((side, 24), title, fill=_WHITE, font=title_font)
    sub = f'"{team_name}"'
    draw.text((side + 4, 92), sub[:48], fill=_ACCENT, font=sub_font)

    # ── Team OVR badge (top-right) ──
    ratings = [getattr(p, "rating", 0) or 0 for _, p, *_ in resized]
    team_ovr = round(sum(ratings) / len(ratings)) if ratings else 0
    badge_r = 46
    bx, by = width - side - badge_r, header_h // 2
    draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r],
                 outline=_ACCENT, width=3, fill=(30, 36, 56))
    ovr_font = _font(40, display=True)
    label_font = _font(15)
    ot = str(team_ovr)
    draw.text((bx - _text_w(draw, ot, ovr_font) / 2, by - 30), ot,
              fill=_WHITE, font=ovr_font)
    lt = "TEAM OVR"
    draw.text((bx - _text_w(draw, lt, label_font) / 2, by + 18), lt,
              fill=_ACCENT, font=label_font)

    # ── Place cards row by row, each row horizontally centered ──
    num_font = _font(22, display=True)
    cap_font = _font(18, display=True)
    idx = 0
    y = header_h + pad
    for row_count in rows:
        row_cards = resized[idx:idx + row_count]
        if not row_cards:
            break
        row_px = len(row_cards) * card_w + (len(row_cards) - 1) * pad
        x = (width - row_px) // 2
        for entry, player, im, off_x, off_y in row_cards:
            # Center the (aspect-preserved) card art inside its fixed cell.
            canvas.paste(im, (x + off_x, y + off_y))

            is_captain = (captain_roster_id is not None
                          and entry.id == captain_roster_id)
            if is_captain:
                # Gold border + "CAPTAIN" pill above the card.
                draw.rectangle([x - 2, y - 2, x + card_w + 1, y + card_h + 1],
                               outline=_GOLD, width=3)
                pill_w, pill_h = 96, 24
                px = x + (card_w - pill_w) // 2
                py = y - pill_h - 4
                draw.rounded_rectangle([px, py, px + pill_w, py + pill_h],
                                       radius=6, fill=_GOLD)
                ct = "CAPTAIN"
                draw.text((px + (pill_w - _text_w(draw, ct, cap_font)) / 2,
                           py + 2), ct, fill=(0, 0, 0), font=cap_font)

            # Position number badge — top-left of each card.
            n = str(idx + 1)
            nb_r = 16
            nbx, nby = x + nb_r + 6, y + nb_r + 6
            draw.ellipse([nbx - nb_r, nby - nb_r, nbx + nb_r, nby + nb_r],
                         fill=(0, 0, 0),
                         outline=_GOLD if is_captain else _ACCENT, width=2)
            draw.text((nbx - _text_w(draw, n, num_font) / 2, nby - 14), n,
                      fill=_WHITE, font=num_font)

            x += card_w + pad
            idx += 1
        y += card_h + pad

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()
