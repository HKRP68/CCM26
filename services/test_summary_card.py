"""Test-match summary card — the light poster, stretched to five days.

The two-innings poster (services.match_summary_card) has no room for a Test:
up to four innings, a follow-on, a declaration, a draw. This card reuses every
piece of that poster — header, conditions strip, innings blocks, result bar,
Player of the Match strip — and adds a day-by-day timeline of close-of-play
scores between the last innings and the result.

Layout, top to bottom (reference px, drawn at 2× like the poster):

    header · conditions chips · innings 1..n · DAY 1 ─ DAY 2 ─ … ─ RESULT · result bar · POTM

The height follows the innings count, so an innings win (three innings) is a
shorter card than a match that went the distance.
"""

import io
import logging
from datetime import datetime

from PIL import Image, ImageDraw

from services import match_summary_card as msc
from services.match_summary_card import (
    CANVAS_W, CONTENT_R, GOLD, GOLD_BRIGHT, GOLD_PALE, HEADER_H, INK, INN_H, LINE,
    PAD_L, POTM_H, RESULT_ACCENT, RESULT_H, STRIP_GAP, STRIP_H, TEAM_A, TEAM_B, WHITE,
    COL_HEAD, NAME_INK, s, sbox,
)

logger = logging.getLogger(__name__)

BLOCK_GAP = 12           # between innings blocks
TIMELINE_H = 104
TIMELINE_LABEL_W = 150   # the navy "DAY BY DAY" tab on the left
SECTION_GAP = 14


def _layout(n_innings, with_strip):
    top = HEADER_H + (STRIP_GAP * 2 + STRIP_H if with_strip else 0) + 6
    blocks = [top + i * (INN_H + BLOCK_GAP) for i in range(n_innings)]
    blocks_end = (blocks[-1] + INN_H) if blocks else top
    timeline_y = blocks_end + SECTION_GAP
    result_y = timeline_y + TIMELINE_H + SECTION_GAP
    potm_y = result_y + RESULT_H + 6
    height = potm_y + POTM_H + 20
    return blocks, timeline_y, result_y, potm_y, height


def _draw_rain(draw, cx, cy, r=9):
    """A small cloud with three drops — rain stopped play that day."""
    cloud = (*INK, 255)
    for bx, by, br in ((-0.5, 0.1, 0.5), (0.05, -0.15, 0.65), (0.6, 0.15, 0.45)):
        draw.ellipse(sbox(cx + (bx - br) * r, cy + (by - br) * r,
                          cx + (bx + br) * r, cy + (by + br) * r), fill=cloud)
    draw.rectangle(sbox(cx - r, cy + 0.05 * r, cx + r * 1.05, cy + 0.6 * r), fill=cloud)
    for dx in (-0.55, 0.05, 0.65):
        draw.line([(s(cx + dx * r), s(cy + r * 0.85)), (s(cx + dx * r - 2), s(cy + r * 1.45))],
                  fill=(66, 150, 220, 255), width=s(2))


def _draw_timeline(img, draw, y, days):
    """The day-by-day rail.

    ``days`` is ``[{"label": "DAY 1", "line": "ALP 338/5", "rain": bool,
    "final": bool}, ...]`` — one node per day played, the last being the
    result day.
    """
    y0, y1 = y, y + TIMELINE_H
    draw.rounded_rectangle(sbox(PAD_L, y0, CONTENT_R, y1), radius=s(16),
                           fill=(255, 255, 255, 255), outline=(*LINE, 190), width=s(1))
    # The navy tab, with the same angled cut as an innings crest panel.
    tab_r = PAD_L + TIMELINE_LABEL_W
    draw.rounded_rectangle(sbox(PAD_L, y0, tab_r, y1), radius=s(16), fill=(*INK, 255))
    draw.polygon([(s(tab_r - 18), s(y0)), (s(tab_r + 22), s(y0)),
                  (s(tab_r - 6), s(y1)), (s(tab_r - 18), s(y1))], fill=(*INK, 255))
    f_tab = msc._font(20, family="display")
    msc._draw_text(draw, (PAD_L + TIMELINE_LABEL_W / 2 + 2, (y0 + y1) / 2 - 12), "DAY BY",
                   f_tab, GOLD_PALE, tracking=2.2, anchor="mm")
    msc._draw_text(draw, (PAD_L + TIMELINE_LABEL_W / 2 + 2, (y0 + y1) / 2 + 14), "DAY",
                   f_tab, WHITE, tracking=2.2, anchor="mm")

    days = list(days or [])[:6]
    if not days:
        return
    rail_x0, rail_x1 = tab_r + 130, CONTENT_R - 100
    rail_y = (y0 + y1) / 2 + 2
    draw.rectangle(sbox(rail_x0, rail_y - 2, rail_x1, rail_y + 2), fill=(*GOLD, 230))
    step = (rail_x1 - rail_x0) / max(1, len(days) - 1) if len(days) > 1 else 0
    slot_w = step + 20 if len(days) > 1 else 400
    f_day = msc._font(17, family="display")
    f_line = msc._font(17, family="body")
    for i, d in enumerate(days):
        cx = rail_x0 + i * step if len(days) > 1 else (rail_x0 + rail_x1) / 2
        final = bool(d.get("final"))
        r = 13 if final else 9
        if final:
            draw.ellipse(sbox(cx - r - 5, rail_y - r - 5, cx + r + 5, rail_y + r + 5),
                         fill=(*GOLD_BRIGHT, 70))
        draw.ellipse(sbox(cx - r, rail_y - r, cx + r, rail_y + r),
                     fill=(*(GOLD_BRIGHT if final else GOLD), 255),
                     outline=(*INK, 255), width=s(2.5 if final else 2))
        label = str(d.get("label", "")).upper()
        msc._draw_text(draw, (cx, rail_y - 28), label, f_day,
                       INK if final else COL_HEAD, tracking=1.6, anchor="mm")
        line = msc._fit(draw, str(d.get("line", "")).upper(), f_line, slot_w - 16, 4)
        msc._draw_text(draw, (cx, rail_y + 30), line, f_line, NAME_INK, anchor="mm",
                       weight=0.35)
        if d.get("rain"):
            lw = msc._tw(draw, label, f_day)
            _draw_rain(draw, cx + lw / 2 + 18, rail_y - 30)


def generate_test_summary(*, innings, winner_name, win_margin_text, result_headline=None,
                          days=None, conditions=None, side_a=None,
                          potm_name=None, potm_team=None, potm_stats=None,
                          potm_runs=None, potm_balls=None, potm_fours=None, potm_sixes=None,
                          potm_wickets=None, potm_conceded=None, potm_overs=None,
                          potm_photo_png=None, potm_card_png=None,
                          inn1_color=None, inn2_color=None,
                          inn1_logo_png=None, inn2_logo_png=None,
                          stadium=None, match_date=None, text_settings=None,
                          match_no=None, **_ignored) -> bytes | None:
    """Render a Test summary card. Returns PNG bytes or ``None`` on failure.

    ``innings`` is a list (1-4) of dicts: ``team``, ``runs``, ``wickets``,
    ``overs``, ``score_text`` ("498", "190-2 D"), ``meta_text`` ("1ST INNINGS
    · 157.1 OVERS"), ``batters`` and ``bowlers`` (the poster's top-four rows).
    ``side_a`` names the team that batted first; it gets ``inn1_color`` and
    ``inn1_logo_png`` in every innings it bats, the other side the inn2 pair.
    """
    try:
        ts = text_settings
        innings = list(innings or [])[:4]
        blocks, timeline_y, result_y, potm_y, height = _layout(len(innings), bool(conditions))
        img = Image.new("RGBA", (s(CANVAS_W), s(height)), (255, 255, 255, 255))
        msc._draw_paper(img)
        draw = ImageDraw.Draw(img, "RGBA")

        colour_a = msc._hex_to_rgb(inn1_color, TEAM_A)
        colour_b = msc._hex_to_rgb(inn2_color, TEAM_B)
        side_a = side_a or (innings[0]["team"] if innings else None)

        venue = stadium or (match_date.strftime("%d %b %Y")
                            if isinstance(match_date, datetime) else "")
        msc._draw_header(img, draw, ts, match_no=match_no, stadium=venue,
                         header_left="TEST", header_right="SUMMARY",
                         tagline="FIVE DAYS|ONE RESULT|TEST CRICKET",)
        if conditions:
            msc._draw_conditions_strip(img, draw, ts, HEADER_H + STRIP_GAP, conditions)

        for y, inn in zip(blocks, innings):
            is_a = inn.get("team") == side_a
            msc._draw_innings(
                img, draw, ts, y,
                team=inn.get("team"), runs=inn.get("runs", 0), wickets=inn.get("wickets", 0),
                overs=inn.get("overs", ""), overs_total=None, is_hundred=False,
                batters=inn.get("batters", []), bowlers=inn.get("bowlers", []),
                color=colour_a if is_a else colour_b,
                crest_png=inn1_logo_png if is_a else inn2_logo_png,
                potm_name=potm_name,
                score_text=inn.get("score_text"), meta_text=inn.get("meta_text"))

        _draw_timeline(img, draw, timeline_y, days)

        msc._draw_result(img, draw, ts, winner_name, win_margin_text, RESULT_ACCENT,
                         y=result_y, headline=result_headline)

        metrics = msc._potm_metrics(potm_stats, potm_runs, potm_balls, potm_fours,
                                    potm_sixes, None, wickets=potm_wickets,
                                    conceded=potm_conceded, overs=potm_overs)
        msc._draw_potm(img, draw, ts, name=potm_name, team=potm_team,
                       photo_png=potm_photo_png, card_png=potm_card_png, metrics=metrics,
                       flourish=msc._flourish(potm_stats, potm_runs, potm_wickets),
                       y=potm_y)

        out = Image.new("RGB", img.size, (255, 255, 255))
        out.paste(img, mask=img.split()[-1])
        out = out.resize((CANVAS_W, height), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render Test summary card")
        return None
