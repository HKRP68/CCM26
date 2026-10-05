"""Team of the Tournament — the best XI a tournament produced.

Built from the same impact points as the MVP race (``services.tournament_mvp``)
so the two never disagree: one wicket-keeper, four batters, two all-rounders
and four bowlers, each the highest-scoring player of that role with enough
matches behind them. A role that is short is filled by the best player left,
so the XI is always eleven when eleven players have taken the field. The MVP
captains it.

Rendered as the usual XI card image (``services.xi_image``), with a text list
for when the cards can't be drawn.
"""

import logging
from html import escape
from types import SimpleNamespace

logger = logging.getLogger(__name__)

SHAPE = (("wk", 1), ("bat", 4), ("ar", 2), ("bowl", 4))
ROLE_LABEL = {"wk": "🧤", "bat": "🏏", "ar": "⚡", "bowl": "🎯"}
DISPLAY_ORDER = ("bat", "wk", "ar", "bowl")


def _role(player, row):
    cat = (getattr(player, "category", "") or "").strip().lower()
    if "keep" in cat or cat == "wk":
        return "wk"
    if "all" in cat:
        return "ar"
    if "bowl" in cat:
        return "bowl"
    if "bat" in cat:
        return "bat"
    # No card to ask: read the role off what they actually did.
    if row.bat_points and row.bowl_points and min(row.bat_points, row.bowl_points) * 2 >= max(row.bat_points, row.bowl_points):
        return "ar"
    return "bowl" if row.bowl_points > row.bat_points else "bat"


def _player_for(session, row):
    from models import ChallengePlayer, Player
    pid = getattr(row, "player_id", None)
    if pid:
        p = session.get(Player, int(pid))
        if p is not None:
            return p
    rid = getattr(row, "roster_id", None)
    if rid:
        cp = session.get(ChallengePlayer, int(rid))
        if cp is not None and cp.source_player_id:
            return session.get(Player, int(cp.source_player_id))
    return None


def pick_xi(session, tournament_id):
    """``[SimpleNamespace(row, player, role, captain)]`` — up to eleven."""
    from services import tournament_mvp
    rows = [r for r in tournament_mvp.mvp_rows(session, tournament_id)
            if r.points > 0]
    if not rows:
        return []
    most = max(r.matches for r in rows)
    floor = max(1, int(most * 0.4 + 0.5))
    pool = [r for r in rows if r.matches >= floor] or rows
    pool.sort(key=lambda r: (r.points, r.awards, r.wins), reverse=True)
    cands = [SimpleNamespace(row=r, player=_player_for(session, r), role=None,
                             captain=False) for r in pool]
    for c in cands:
        c.role = _role(c.player, c.row)
    picked, taken = [], set()
    for role, n in SHAPE:
        for c in cands:
            if len([p for p in picked if p.role == role]) >= n:
                break
            if c.role == role and id(c) not in taken:
                picked.append(c)
                taken.add(id(c))
    for c in cands:                       # short roles: best of the rest
        if len(picked) >= 11:
            break
        if id(c) not in taken:
            picked.append(c)
            taken.add(id(c))
    if picked:
        max(picked, key=lambda c: c.row.points).captain = True
    picked.sort(key=lambda c: (DISPLAY_ORDER.index(c.role), -c.row.points))
    return picked


def _line(c):
    r = c.row
    bits = []
    if r.bat_balls:
        bits.append(f"{r.bat_runs} runs")
    if r.bowl_balls:
        bits.append(f"{r.bowl_wickets} wkts")
    cap = " (C)" if c.captain else ""
    team = f" <i>({escape(r.team_name)})</i>" if r.team_name else ""
    return (f"{ROLE_LABEL.get(c.role, '•')} <b>{escape(r.name or '—')}</b>{cap}{team}"
            + (f" — {', '.join(bits)}" if bits else "") + f" · {r.points:g} pts")


def render_text(tour, xi):
    out = [f"🌟 <b>Team of the Tournament</b> — {escape(tour.name or '')}", ""]
    if not xi:
        out.append("<i>Not enough scorecards yet — check back after a few matches.</i>")
        return "\n".join(out)
    out += [_line(c) for c in xi]
    out += ["", "<i>Picked on MVP impact points: 1 keeper, 4 batters, "
                "2 all-rounders, 4 bowlers.</i>"]
    return "\n".join(out)


def render_image(tour, xi):
    """PNG bytes of the XI as cards, or None (any card missing → text only)."""
    if len(xi) < 11 or any(c.player is None for c in xi):
        return None
    try:
        from services.xi_image import build_xi_image
        pairs = [(SimpleNamespace(id=i), c.player) for i, c in enumerate(xi)]
        cap = next((i for i, c in enumerate(xi) if c.captain), None)
        return build_xi_image(pairs, team_name=tour.name or "Tournament",
                              captain_roster_id=cap, title="TEAM OF THE TOURNAMENT")
    except Exception:
        logger.exception("Team of the Tournament image failed for %s", tour.id)
        return None


def post_for(session, tour):
    """A watch Post (photo + caption) for the ceremony, or None."""
    from services.tournament_watch import Post
    xi = pick_xi(session, tour.id)
    if not xi or not tour.announce_chat_id:
        return None
    return Post(chat_id=tour.announce_chat_id, kind="team_of_tournament",
                text=render_text(tour, xi), photo=render_image(tour, xi))
