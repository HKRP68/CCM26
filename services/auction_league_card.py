"""Auction League season summary card — a PNG for the end of a season.

League and season, the champions, how your franchise finished, the Orange
and Purple Caps and the MVP, the Team of the Tournament, your top buy and
(from the second season) your career record. Pure: takes a finished state,
returns PNG bytes. Fonts come from the match summary card so the two look
like one family. Pillow is CPU-bound — callers render it off the event loop
(``asyncio.to_thread``).
"""

import io

from PIL import Image, ImageDraw

from services import auction_league_service as AL
from services.match_summary_card import _font, s

W, H = 540, 760          # reference size; the canvas is SCALE× larger

BG_TOP = (14, 22, 48)
BG_BOTTOM = (40, 16, 64)
GOLD = (245, 196, 66)
WHITE = (244, 244, 250)
MUTED = (170, 176, 200)
PANEL = (255, 255, 255, 22)
ACCENT = (98, 196, 255)

FINISH_TEXT = {"champion": "CHAMPIONS", "runner_up": "RUNNERS-UP",
               "playoffs": "PLAYOFFS", "league": "LEAGUE STAGE"}


def _gradient(img):
    draw = ImageDraw.Draw(img)
    height = img.size[1]
    for y in range(height):
        t = y / max(1, height - 1)
        col = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (img.size[0], y)], fill=col)


def _fit(draw, text, size, family, max_w):
    """The text at the largest size ≤ ``size`` that fits ``max_w`` (reference px)."""
    text = str(text or "")
    while size > 8:
        font = _font(size, family)
        if draw.textlength(text, font=font) <= s(max_w):
            return text, font
        size -= 1
    font = _font(8, family)
    while text and draw.textlength(text + "…", font=font) > s(max_w):
        text = text[:-1]
    return text + "…", font


def _text(draw, xy, text, size, family="body", fill=WHITE, max_w=None, anchor="la"):
    if max_w:
        text, font = _fit(draw, text, size, family, max_w)
    else:
        font = _font(size, family)
    draw.text((s(xy[0]), s(xy[1])), str(text), font=font, fill=fill, anchor=anchor)


def _panel(img, box, radius=12):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([s(v) for v in box], radius=s(radius), fill=PANEL)
    img.alpha_composite(layer)


def _award(state, aw, key):
    if key not in aw:
        return "—", ""
    pid, head = aw[key]
    c = state["pool"].get(str(pid)) or {}
    owner = AL.owner_of(state, pid)
    short = state["teams"][owner]["short"] if owner in state["teams"] else ""
    return c.get("name", "?"), f"{head}{' · ' + short if short else ''}"


def render_season_card(state):
    """PNG bytes for a finished season (raises if it isn't finished)."""
    if state.get("phase") != AL.PHASE_COMPLETED:
        raise AL.AuctionLeagueError("The season isn't over yet.")
    summ = AL.season_summary(state)
    aw = AL.season_awards(state)
    img = Image.new("RGBA", (s(W), s(H)), BG_TOP + (255,))
    _gradient(img)
    draw = ImageDraw.Draw(img)

    league = state["league"]["name"].upper()
    _text(draw, (W / 2, 34), f"{league} AUCTION LEAGUE", 26, "headline", WHITE,
          max_w=W - 40, anchor="mm")
    _text(draw, (W / 2, 62), f"SEASON {summ['season']}", 18, "display", ACCENT, anchor="mm")

    # Champions
    _panel(img, (20, 84, W - 20, 170))
    _text(draw, (W / 2, 104), "CHAMPIONS", 16, "display", MUTED, anchor="mm")
    _text(draw, (W / 2, 138), summ["champion"] or "—", 34, "headline", GOLD,
          max_w=W - 60, anchor="mm")
    _text(draw, (W / 2, 160), f"Runners-up: {summ['runner_up'] or '—'}", 12, "body", MUTED,
          max_w=W - 60, anchor="mm")

    # Your franchise
    _panel(img, (20, 182, W - 20, 262))
    _text(draw, (36, 198), "YOUR FRANCHISE", 13, "display", MUTED)
    _text(draw, (36, 216), summ["team"], 24, "headline", WHITE, max_w=300)
    pos = f"#{summ['position']} in the table · " if summ.get("position") else ""
    _text(draw, (36, 242), f"{pos}Won {summ['w']} of {summ['p']}", 13, "body", MUTED,
          max_w=300)
    _text(draw, (W - 36, 226), FINISH_TEXT.get(summ["finish"], ""), 22, "display",
          GOLD if summ["finish"] in ("champion", "runner_up") else ACCENT, anchor="rm")

    # Awards
    y = 274
    _panel(img, (20, y, W - 20, y + 120))
    for i, (key, label, col) in enumerate((("orange", "ORANGE CAP", (255, 152, 56)),
                                           ("purple", "PURPLE CAP", (186, 120, 255)),
                                           ("mvp", "MVP", GOLD))):
        cx = 20 + (W - 40) * (2 * i + 1) / 6
        name, head = _award(state, aw, key)
        _text(draw, (cx, y + 22), label, 14, "display", col, anchor="mm")
        _text(draw, (cx, y + 56), name, 17, "headline", WHITE, max_w=(W - 40) / 3 - 14,
              anchor="mm")
        _text(draw, (cx, y + 86), head, 11, "body", MUTED, max_w=(W - 40) / 3 - 10,
              anchor="mm")

    # Team of the Tournament
    y = 406
    xi = aw.get("xi") or []
    _panel(img, (20, y, W - 20, y + 200))
    _text(draw, (W / 2, y + 18), "TEAM OF THE TOURNAMENT", 15, "display", ACCENT, anchor="mm")
    for i, c in enumerate(xi[:11]):
        col_x = 36 if i < 6 else W / 2 + 8
        row_y = y + 40 + (i if i < 6 else i - 6) * 26
        role = AL.ROLE_SHORT.get(c.get("category"), "")
        _text(draw, (col_x, row_y), f"{i + 1}. {c.get('name', '?')}", 13, "body", WHITE,
              max_w=W / 2 - 80)
        _text(draw, (col_x + W / 2 - 52, row_y + 2), role, 11, "display", MUTED)

    # Top buy and career
    y = 618
    _panel(img, (20, y, W - 20, y + 110))
    top = summ.get("top_buy")
    _text(draw, (36, y + 16), "YOUR TOP BUY", 13, "display", MUTED)
    if top:
        _text(draw, (36, y + 34), top["name"], 20, "headline", WHITE, max_w=260)
        _text(draw, (36, y + 64), AL.money(top["price"]).replace("₹", "Rs "), 14,
              "body", GOLD)
    else:
        _text(draw, (36, y + 34), "—", 20, "headline", WHITE)
    rec = AL.career_record(state)
    _text(draw, (W - 36, y + 16), "CAREER", 13, "display", MUTED, anchor="ra")
    _text(draw, (W - 36, y + 34), f"{rec['seasons']} season{'s' if rec['seasons'] != 1 else ''}",
          18, "headline", WHITE, anchor="ra")
    _text(draw, (W - 36, y + 62), f"Titles {rec['titles']} · Finals {rec['finals']}", 12,
          "body", MUTED, anchor="ra")
    _text(draw, (W - 36, y + 80), f"Won {rec['w']} of {rec['p']}", 12, "body", MUTED,
          anchor="ra")

    _text(draw, (W / 2, H - 16), "Auction League · /rcpl", 11, "body", MUTED,
          anchor="mm")
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
