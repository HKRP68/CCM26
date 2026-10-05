"""The playoff bracket as a picture.

One column per knockout round (``round_no`` restarts at 1 for the bracket),
one box per match: both teams — or the slot label ("Winner Q1") while it's
still TBD — with their scores, the winner in gold. A champion box closes the
right-hand side once the final is decided. Works for every bracket the
knockout service builds: Top-4 semis, IPL playoffs (Q1 + Eliminator, Q2,
Final), quarter-finals, and a straight knockout of any size.

Drawn with PIL in the bot's dark-navy card palette (``services.xi_image``).
"""

import io
import logging

logger = logging.getLogger(__name__)

STAGE_LABEL = {
    "round_of_32": "Round of 32", "round_of_16": "Round of 16",
    "quarterfinal": "Quarter-final", "semifinal": "Semi-final",
    "qualifier1": "Qualifier 1", "eliminator": "Eliminator",
    "qualifier2": "Qualifier 2", "final": "Final",
}

BOX_W, BOX_H = 330, 104
COL_GAP, ROW_GAP = 70, 34
SIDE, HEADER_H = 34, 110


def knockout_fixtures(session, tid):
    from models import TournamentMatch
    from services.knockout_service import KNOCKOUT_STAGES
    return (session.query(TournamentMatch)
            .filter(TournamentMatch.tournament_id == int(tid),
                    TournamentMatch.stage.in_(KNOCKOUT_STAGES))
            .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                      TournamentMatch.id).all())


def state_key(session, tid):
    """Changes whenever the bracket's picture would: slots filled, results in."""
    parts = []
    for fx in knockout_fixtures(session, tid):
        parts.append(f"{fx.id}:{fx.team1_id or 0}:{fx.team2_id or 0}:"
                     f"{fx.winner_team_id or 0}:{1 if fx.status == 'completed' else 0}")
    if not parts:
        return None
    import hashlib
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def _score(runs, wkts):
    return "" if runs is None else f"{runs}/{wkts}"


def render(session, tour):
    """PNG bytes of ``tour``'s bracket, or None when it has none."""
    try:
        from PIL import ImageDraw
    except ImportError:
        logger.error("PIL not available")
        return None
    from models import TournamentTeam
    from services.xi_image import (_ACCENT, _GOLD, _HEADER_BG, _MUTED, _WHITE,
                                   _font, _gradient_bg, _text_w)
    fixtures = knockout_fixtures(session, tour.id)
    if not fixtures:
        return None
    names = {t.id: t.name or "—" for t in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
    columns = {}
    for fx in fixtures:
        columns.setdefault(int(fx.round_no or 1), []).append(fx)
    rounds = [columns[k] for k in sorted(columns)]
    final = next((fx for fx in reversed(fixtures) if fx.stage == "final"), None)
    champion = (names.get(final.winner_team_id)
                if final is not None and final.status == "completed"
                and final.winner_team_id else None)

    n_cols = len(rounds) + (1 if champion else 0)
    tallest = max(len(c) for c in rounds)
    label_h = 30
    col_h = tallest * (BOX_H + label_h) + (tallest - 1) * ROW_GAP
    width = SIDE * 2 + n_cols * BOX_W + (n_cols - 1) * COL_GAP
    height = HEADER_H + col_h + SIDE * 2
    canvas = _gradient_bg(width, height)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle([0, 0, width, HEADER_H], fill=_HEADER_BG)
    draw.line([(0, HEADER_H), (width, HEADER_H)], fill=_ACCENT, width=2)
    draw.text((SIDE, 18), "PLAYOFFS", fill=_WHITE, font=_font(56, display=True))
    draw.text((SIDE + 4, 78), (tour.name or "")[:60], fill=_ACCENT, font=_font(22))

    stage_font = _font(20, display=True)
    team_font = _font(22)
    score_font = _font(22, display=True)
    # Pass 1: where every box goes.
    boxes, col_of = {}, {}
    for ci, col in enumerate(rounds):
        x = SIDE + ci * (BOX_W + COL_GAP)
        block = len(col) * (BOX_H + label_h) + (len(col) - 1) * ROW_GAP
        y = HEADER_H + SIDE + (col_h - block) // 2
        for fx in col:
            boxes[fx.id] = (x, y + label_h)
            col_of[fx.id] = ci
            y += BOX_H + label_h + ROW_GAP
    half = BOX_H // 2

    # Pass 2: connectors, drawn first so the boxes sit on top of them. Each
    # feed goes into the slot its team fills (or, while TBD, the next free
    # slot); a feed that skips a column (IPL Qualifier 1 → Final) is routed
    # above the boxes rather than through the column in between.
    incoming = {}
    top_lane = HEADER_H + 12
    for fx in fixtures:
        feeds = ((fx.feeds_winner_to_id, fx.winner_team_id, False),
                 (fx.feeds_loser_to_id, None, True))
        for target, team, loser in feeds:
            if not target or target not in boxes or fx.id not in boxes:
                continue
            if loser and fx.winner_team_id:
                team = fx.team2_id if fx.winner_team_id == fx.team1_id else fx.team1_id
            tfx = next(t for t in fixtures if t.id == target)
            if team and team == tfx.team1_id:
                slot = 0
            elif team and team == tfx.team2_id:
                slot = 1
            else:
                # Still TBD: the feed fills one of the slots nobody holds yet.
                empty = [i for i, t in enumerate((tfx.team1_id, tfx.team2_id))
                         if t is None] or [0, 1]
                slot = empty[incoming.get(target, 0) % len(empty)]
            incoming[target] = incoming.get(target, 0) + 1
            sx, sy = boxes[fx.id]
            tx, ty = boxes[target]
            x1, y1 = sx + BOX_W, sy + half
            y2 = ty + half // 2 + slot * half
            colour = (90, 100, 130) if loser else _ACCENT
            gap1 = x1 + COL_GAP // 2
            if col_of[target] - col_of[fx.id] > 1:
                gap2 = tx - COL_GAP // 2
                pts = [(x1, y1), (gap1, y1), (gap1, top_lane), (gap2, top_lane),
                       (gap2, y2), (tx, y2)]
            else:
                pts = [(x1, y1), (gap1, y1), (gap1, y2), (tx, y2)]
            draw.line(pts, fill=colour, width=2)

    # Pass 3: the boxes.
    centres = {}
    for fx in fixtures:
        if fx.id not in boxes:
            continue
        x, by = boxes[fx.id]
        label = STAGE_LABEL.get(fx.stage, (fx.stage or "").title())
        if fx.match_no:
            label += f" · M{fx.match_no}"
        draw.text((x + 4, by - label_h), label.upper(), fill=_ACCENT, font=stage_font)
        done = fx.status == "completed"
        draw.rounded_rectangle([x, by, x + BOX_W, by + BOX_H], radius=12,
                               fill=(28, 33, 52),
                               outline=_GOLD if done else (70, 80, 110), width=2)
        draw.line([(x + 10, by + half), (x + BOX_W - 10, by + half)],
                  fill=(60, 68, 96), width=1)
        for slot, (tid, lab, runs, wk) in enumerate((
                (fx.team1_id, fx.slot1_label, fx.inn1_runs, fx.inn1_wickets),
                (fx.team2_id, fx.slot2_label, fx.inn2_runs, fx.inn2_wickets))):
            ty = by + slot * half + (half - 26) // 2
            name = names.get(tid) if tid else (lab or "TBD")
            won = done and tid and fx.winner_team_id == tid
            colour = _GOLD if won else (_WHITE if tid else _MUTED)
            draw.text((x + 16, ty), str(name)[:22], fill=colour, font=team_font)
            sc = _score(runs, wk) if done else ""
            if sc:
                draw.text((x + BOX_W - 16 - _text_w(draw, sc, score_font), ty),
                          sc, fill=colour, font=score_font)
        centres[fx.id] = (x + BOX_W, by + half)
    if champion:
        x = SIDE + len(rounds) * (BOX_W + COL_GAP)
        y = HEADER_H + SIDE + col_h // 2 - BOX_H // 2
        draw.rounded_rectangle([x, y, x + BOX_W, y + BOX_H], radius=14,
                               fill=(48, 40, 10), outline=_GOLD, width=3)
        draw.text((x + 16, y + 10), "CHAMPIONS", fill=_GOLD, font=stage_font)
        draw.text((x + 16, y + 44), champion[:22], fill=_WHITE, font=_font(30, display=True))
        if final is not None and final.id in centres:
            x1, y1 = centres[final.id]
            draw.line([(x1, y1), (x, y + BOX_H // 2)], fill=_GOLD, width=3)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
