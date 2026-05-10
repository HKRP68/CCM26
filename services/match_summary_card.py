"""Match summary card — sent after the match ends.

Compact, single-image visual recap with:
  - Final scoreline (both innings)
  - Winner badge with margin
  - Player of the Match with impact + key stats
  - Top scorer (across both teams)
  - Top wicket-taker (across both teams)
  - Match meta (overs, format, date)

Color scheme matches the innings cards: Lava Red / Lagoon Teal / Midnight Slate.
"""

import io
import os
import logging
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Color palette
PRIMARY    = (255, 42, 42)      # Lava Red
SECONDARY  = (0, 201, 167)      # Lagoon Teal
GOLD       = (251, 191, 36)     # Trophy gold
BG         = (15, 23, 42)       # Midnight Slate
BG_DARK    = (6, 11, 24)
CARD_BG    = (24, 33, 54)
TEXT       = (241, 245, 249)
DIM        = (130, 140, 160)
MUTED      = (95, 105, 125)
SEP        = (40, 50, 70)

_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets", "logo.png")


def _font(size, bold=False, italic=False):
    if bold and italic:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"
    elif bold:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    elif italic:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"
    else:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _gradient(img, top, bottom):
    w, h = img.size
    pixels = img.load()
    for y in range(h):
        t = y / h
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(w):
            pixels[x, y] = (r, g, b)


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _load_logo(target=110):
    try:
        if not os.path.exists(_LOGO_PATH):
            return None
        img = Image.open(_LOGO_PATH).convert("RGBA")
        img.thumbnail((target, target), Image.LANCZOS)
        return img
    except Exception:
        return None


def _draw_card(draw, x, y, w, h, *, accent=None):
    """Filled rounded card with optional accent bar on the left."""
    draw.rounded_rectangle([x, y, x + w, y + h], radius=14,
                           fill=CARD_BG, outline=SEP, width=1)
    if accent:
        # accent stripe on the left edge
        draw.rounded_rectangle([x, y, x + 6, y + h], radius=3, fill=accent)


def _format_player_line(rating, name, stat_a, stat_b):
    """Returns the components for a 'top performer' row."""
    return (rating or "—"), name, stat_a, stat_b


def generate_match_summary(*,
    inn1_team, inn1_runs, inn1_wickets, inn1_overs,
    inn2_team, inn2_runs, inn2_wickets, inn2_overs,
    winner_name, win_margin_text,  # e.g. "by 24 runs" or "by 5 wickets"
    overs_total,                   # total overs format (e.g. 20)
    potm_name=None, potm_rating=None, potm_team=None,
    potm_stats=None, potm_impact=None,
    top_scorer=None,    # dict: name, rating, team, runs, balls, fours, sixes
    top_wicket=None,    # dict: name, rating, team, wickets, runs, overs
    match_date=None,
    is_spectator=False,
) -> bytes | None:
    """Render the match summary card. Returns PNG bytes or None on failure."""
    try:
        W = 1400
        H = 1100
        img = Image.new("RGB", (W, H), BG)
        _gradient(img, BG, BG_DARK)
        draw = ImageDraw.Draw(img, "RGBA")

        # Fonts
        f_title = _font(40, bold=True, italic=True)
        f_brand = _font(15, bold=True, italic=True)
        f_meta = _font(18, bold=True)
        f_meta_dim = _font(15, bold=True)
        f_team = _font(38, bold=True, italic=True)
        f_score = _font(72, bold=True)
        f_score_lbl = _font(18, bold=True)
        f_winner_label = _font(24, bold=True)
        f_winner_name = _font(46, bold=True, italic=True)
        f_margin = _font(22, bold=True)
        f_section_lbl = _font(18, bold=True)
        f_player_name = _font(28, bold=True)
        f_player_team = _font(15, italic=True)
        f_stat_big = _font(30, bold=True)
        f_stat_small = _font(16, bold=True)
        f_rating_pill = _font(15, bold=True)

        # ── Header ────────────────────────────────────
        # Logo
        logo = _load_logo(target=100)
        title_x = 50
        if logo:
            img.paste(logo, (40, 30), logo)
            title_x = 40 + logo.size[0] + 20
        # Title
        draw.line([(title_x, 50), (title_x + 50, 50)], fill=GOLD, width=4)
        draw.text((title_x + 64, 38), "MATCH SUMMARY",
                  fill=GOLD, font=f_title)
        # Date / overs in top right
        meta_lines = []
        if match_date:
            meta_lines.append(match_date.strftime("%d %b %Y"))
        meta_lines.append(f"T{overs_total} · CRICMASTERULTRA")
        meta_y = 38
        for line in meta_lines:
            tw = _tw(draw, line, f_meta_dim)
            draw.text((W - 50 - tw, meta_y), line, fill=DIM, font=f_meta_dim)
            meta_y += 22

        # ── Scoreline cards (innings 1 + innings 2) ────
        score_y = 160
        score_h = 200
        gap = 24
        card_w = (W - 100 - gap) // 2

        # Innings 1 card (Lava Red)
        _draw_card(draw, 50, score_y, card_w, score_h, accent=PRIMARY)
        draw.text((76, score_y + 22), "INNINGS 1", fill=PRIMARY, font=f_section_lbl)
        # Team name (auto-shrink)
        team1 = (inn1_team or "—").upper()
        team1_size = 38
        while team1_size > 22:
            tf = _font(team1_size, bold=True, italic=True)
            if _tw(draw, team1, tf) <= card_w - 50:
                break
            team1_size -= 2
        f_team1 = _font(team1_size, bold=True, italic=True)
        draw.text((76, score_y + 50), team1, fill=TEXT, font=f_team1)
        # Score
        score1_str = f"{inn1_runs}/{inn1_wickets}"
        draw.text((76, score_y + 100), score1_str, fill=TEXT, font=f_score)
        # Overs
        overs1_str = f"({inn1_overs} ov)"
        s1w = _tw(draw, score1_str, f_score)
        draw.text((76 + s1w + 18, score_y + 142),
                  overs1_str, fill=DIM, font=f_meta)

        # Innings 2 card (Lagoon Teal)
        x2 = 50 + card_w + gap
        _draw_card(draw, x2, score_y, card_w, score_h, accent=SECONDARY)
        draw.text((x2 + 26, score_y + 22), "INNINGS 2", fill=SECONDARY, font=f_section_lbl)
        team2 = (inn2_team or "—").upper()
        team2_size = 38
        while team2_size > 22:
            tf = _font(team2_size, bold=True, italic=True)
            if _tw(draw, team2, tf) <= card_w - 50:
                break
            team2_size -= 2
        f_team2 = _font(team2_size, bold=True, italic=True)
        draw.text((x2 + 26, score_y + 50), team2, fill=TEXT, font=f_team2)
        score2_str = f"{inn2_runs}/{inn2_wickets}"
        draw.text((x2 + 26, score_y + 100), score2_str, fill=TEXT, font=f_score)
        s2w = _tw(draw, score2_str, f_score)
        draw.text((x2 + 26 + s2w + 18, score_y + 142),
                  f"({inn2_overs} ov)", fill=DIM, font=f_meta)

        # ── Winner banner ────────────────────────────
        win_y = score_y + score_h + 30
        win_h = 130
        # Trophy band — gold gradient strip with team name
        draw.rounded_rectangle([50, win_y, W - 50, win_y + win_h],
                               radius=14, fill=(36, 28, 12), outline=GOLD, width=2)
        # Trophy: drawn as a gold star+circle composite (no emoji font dependency)
        cx, cy = 110, win_y + 65
        # Big circle background
        draw.ellipse([cx - 32, cy - 32, cx + 32, cy + 32], fill=GOLD, outline=None)
        # 'W' inside
        f_W = _font(36, bold=True)
        ww = _tw(draw, "W", f_W)
        draw.text((cx - ww // 2, cy - 22), "W", fill=BG_DARK, font=f_W)
        draw.text((175, win_y + 24), "WINNER", fill=GOLD, font=f_winner_label)
        # Winner name
        wname_upper = (winner_name or "—").upper()
        wname_size = 46
        while wname_size > 28:
            tf = _font(wname_size, bold=True, italic=True)
            if _tw(draw, wname_upper, tf) <= W - 350:
                break
            wname_size -= 3
        f_wname = _font(wname_size, bold=True, italic=True)
        draw.text((175, win_y + 50), wname_upper, fill=TEXT, font=f_wname)
        # Margin badge on the right
        margin_text = (win_margin_text or "").upper()
        if margin_text:
            mw = _tw(draw, margin_text, f_margin)
            draw.text((W - 80 - mw, win_y + 56), margin_text,
                      fill=GOLD, font=f_margin)

        # ── POTM card (full width) ─────────────────────
        potm_y = win_y + win_h + 30
        potm_h = 180
        _draw_card(draw, 50, potm_y, W - 100, potm_h, accent=GOLD)
        draw.text((76, potm_y + 22), "PLAYER OF THE MATCH",
                  fill=GOLD, font=f_section_lbl)
        if potm_name:
            # Rating pill
            pill_x = 76
            pill_y = potm_y + 56
            pill_text = str(potm_rating or "—")
            pill_w = _tw(draw, pill_text, f_rating_pill) + 22
            draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + 30],
                                   radius=6, fill=GOLD)
            draw.text((pill_x + 11, pill_y + 6), pill_text,
                      fill=BG_DARK, font=f_rating_pill)
            # Name
            name_x = pill_x + pill_w + 16
            draw.text((name_x, potm_y + 50),
                      potm_name.upper(), fill=TEXT, font=f_player_name)
            # Team
            if potm_team:
                draw.text((name_x, potm_y + 90),
                          f"({potm_team})", fill=DIM, font=f_player_team)
            # Stats line below — strip emoji and use plain text labels
            if potm_stats:
                clean_stats = (potm_stats
                               .replace("🏏", "BAT:")
                               .replace("🎳", "BOWL:")
                               .replace(" | ", "    "))
                draw.text((76, potm_y + 122),
                          clean_stats, fill=TEXT, font=f_meta)
            # Impact in top-right — make sure it fits inside the card
            if potm_impact is not None:
                imp_str = f"{int(potm_impact)} IMPACT"
                iw = _tw(draw, imp_str, f_stat_big)
                # Pin to inside-right with safe padding
                imp_x = max(name_x + 200, W - 80 - iw)
                draw.text((imp_x, potm_y + 60),
                          imp_str, fill=GOLD, font=f_stat_big)
        else:
            draw.text((76, potm_y + 60), "(none)", fill=DIM, font=f_player_name)

        # ── Top performers row ──────────────────────
        perf_y = potm_y + potm_h + 28
        perf_h = 180
        perf_card_w = (W - 100 - gap) // 2

        # Top scorer (left)
        _draw_card(draw, 50, perf_y, perf_card_w, perf_h, accent=PRIMARY)
        draw.text((76, perf_y + 22), "TOP SCORER",
                  fill=PRIMARY, font=f_section_lbl)
        if top_scorer:
            # Rating pill
            pill_text = str(top_scorer.get("rating", "—"))
            pill_w = _tw(draw, pill_text, f_rating_pill) + 20
            draw.rounded_rectangle([76, perf_y + 56, 76 + pill_w, perf_y + 86],
                                   radius=6, fill=PRIMARY)
            draw.text((86, perf_y + 62), pill_text,
                      fill=TEXT, font=f_rating_pill)
            # Name + team
            draw.text((76 + pill_w + 12, perf_y + 50),
                      (top_scorer["name"] or "—").upper(),
                      fill=TEXT, font=f_player_name)
            if top_scorer.get("team"):
                draw.text((76 + pill_w + 12, perf_y + 88),
                          f"({top_scorer['team']})", fill=DIM, font=f_player_team)
            # Stats: runs (big) + balls/4s/6s
            runs = top_scorer.get("runs", 0)
            balls = top_scorer.get("balls", 0)
            fours = top_scorer.get("fours", 0)
            sixes = top_scorer.get("sixes", 0)
            draw.text((76, perf_y + 120),
                      f"{runs}", fill=TEXT, font=f_stat_big)
            rw = _tw(draw, str(runs), f_stat_big)
            draw.text((76 + rw + 8, perf_y + 130),
                      f"({balls}b)", fill=DIM, font=f_meta_dim)
            sub = f"4s: {fours}  ·  6s: {sixes}"
            if balls:
                sr = (runs / balls) * 100
                sub += f"  ·  SR {sr:.0f}"
            draw.text((76, perf_y + 158), sub, fill=DIM, font=f_meta_dim)
        else:
            draw.text((76, perf_y + 60), "(none)", fill=DIM, font=f_player_name)

        # Top wicket-taker (right)
        x3 = 50 + perf_card_w + gap
        _draw_card(draw, x3, perf_y, perf_card_w, perf_h, accent=SECONDARY)
        draw.text((x3 + 26, perf_y + 22), "TOP WICKET-TAKER",
                  fill=SECONDARY, font=f_section_lbl)
        if top_wicket:
            pill_text = str(top_wicket.get("rating", "—"))
            pill_w = _tw(draw, pill_text, f_rating_pill) + 20
            draw.rounded_rectangle([x3 + 26, perf_y + 56,
                                    x3 + 26 + pill_w, perf_y + 86],
                                   radius=6, fill=SECONDARY)
            draw.text((x3 + 36, perf_y + 62), pill_text,
                      fill=BG_DARK, font=f_rating_pill)
            draw.text((x3 + 26 + pill_w + 12, perf_y + 50),
                      (top_wicket["name"] or "—").upper(),
                      fill=TEXT, font=f_player_name)
            if top_wicket.get("team"):
                draw.text((x3 + 26 + pill_w + 12, perf_y + 88),
                          f"({top_wicket['team']})", fill=DIM, font=f_player_team)
            wk = top_wicket.get("wickets", 0)
            ru = top_wicket.get("runs", 0)
            ov = top_wicket.get("overs", 0)
            stat_line = f"{wk}/{ru}"
            draw.text((x3 + 26, perf_y + 120), stat_line,
                      fill=TEXT, font=f_stat_big)
            sw = _tw(draw, stat_line, f_stat_big)
            draw.text((x3 + 26 + sw + 8, perf_y + 130),
                      f"({ov} ov)", fill=DIM, font=f_meta_dim)
            # Economy: handle string/int overs
            try:
                ov_float = float(ov) if not isinstance(ov, (int, float)) else float(ov)
            except (ValueError, TypeError):
                ov_float = 0
            if ov_float > 0:
                econ = ru / ov_float
                draw.text((x3 + 26, perf_y + 158),
                          f"Economy: {econ:.2f}", fill=DIM, font=f_meta_dim)
        else:
            draw.text((x3 + 26, perf_y + 60), "(none)", fill=DIM, font=f_player_name)

        # ── Footer ──────────────────────────────────
        if is_spectator:
            footer = "🎬 SPECTATOR MATCH — no rewards distributed"
            fw = _tw(draw, footer, f_meta_dim)
            draw.text(((W - fw) // 2, H - 38), footer, fill=DIM, font=f_meta_dim)
        else:
            footer = "CRICMASTERULTRA · cricket simulation"
            fw = _tw(draw, footer, f_brand)
            draw.text(((W - fw) // 2, H - 30), footer, fill=MUTED, font=f_brand)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("generate_match_summary failed")
        return None
