"""Prizes and honours when a tournament's final is decided.

:func:`finalize` runs once per tournament (guarded by
``Tournament.awards_given_at``), from the same spot that writes the champion
news story: the recording of a decided final, whichever path recorded it.

Awards: 🏆 Champion and 🥈 Runner-up (the final's two teams), 🟠 Orange Cap
(most runs), 🟣 Purple Cap (most wickets) and ⭐ MVP (``tournament_mvp``).
Each award is written to ``tournament_honours`` — the Hall of Fame's
tournament section — and, when the admin set a prize for it
(``Tournament.prizes_json``), paid in coins and gems. Team awards go to the
team's owner (a Lets Play team *is* its user); player awards to the user who
owns the player. Prizes are created by the bot; nothing is deducted from
anyone.

The ceremony card itself is posted by the tournament watch.
"""

import json
import logging
from datetime import datetime
from html import escape

logger = logging.getLogger(__name__)

AWARDS = ("champion", "runner_up", "orange_cap", "purple_cap", "mvp")
AWARD_LABEL = {
    "champion": "🏆 Champion", "runner_up": "🥈 Runner-up",
    "orange_cap": "🟠 Orange Cap", "purple_cap": "🟣 Purple Cap", "mvp": "⭐ MVP",
}
AWARD_ALIASES = {
    "champion": "champion", "champions": "champion", "winner": "champion",
    "runnerup": "runner_up", "runner_up": "runner_up", "runner": "runner_up",
    "orange": "orange_cap", "orangecap": "orange_cap", "orange_cap": "orange_cap",
    "purple": "purple_cap", "purplecap": "purple_cap", "purple_cap": "purple_cap",
    "mvp": "mvp",
}
MAX_PRIZE_COINS = 10_000_000
MAX_PRIZE_GEMS = 100_000


# ── Prize settings ─────────────────────────────────────────────────────

def prizes(tour):
    """``{award: {"coins": int, "gems": int}}`` — only the awards with a prize."""
    try:
        raw = json.loads(getattr(tour, "prizes_json", None) or "{}")
    except (TypeError, ValueError):
        return {}
    out = {}
    for award in AWARDS:
        p = raw.get(award) or {}
        coins, gems = int(p.get("coins") or 0), int(p.get("gems") or 0)
        if coins or gems:
            out[award] = {"coins": coins, "gems": gems}
    return out


def set_prize(tour, award, coins, gems):
    """Set (0/0 clears) one award's prize. Raises ValueError for bad input."""
    key = AWARD_ALIASES.get(str(award or "").strip().lower().replace("-", "").replace(" ", ""))
    if key is None:
        raise ValueError("Award must be one of: champion, runnerup, orange, purple, mvp.")
    coins, gems = int(coins or 0), int(gems or 0)
    if coins < 0 or gems < 0:
        raise ValueError("Prizes can't be negative.")
    if coins > MAX_PRIZE_COINS or gems > MAX_PRIZE_GEMS:
        raise ValueError("That prize is larger than the allowed maximum.")
    current = prizes(tour)
    if coins or gems:
        current[key] = {"coins": coins, "gems": gems}
    else:
        current.pop(key, None)
    tour.prizes_json = json.dumps(current) if current else None
    return key


def prize_text(p):
    if not p:
        return ""
    bits = []
    if p.get("coins"):
        bits.append(f"{p['coins']:,} coins")
    if p.get("gems"):
        bits.append(f"{p['gems']:,} gems")
    return " + ".join(bits)


# ── Working out the winners ────────────────────────────────────────────

def _user_for_team(session, team):
    from models import User
    tg = getattr(team, "user_tg_id", None) or getattr(team, "owner_tg_id", None)
    if not tg:
        return None
    return session.query(User).filter(User.telegram_id == int(tg)).first()


def _user_for_player(session, tid, row):
    """The user owning a stat/MVP row's player (by player id, then name)."""
    uid = getattr(row, "user_id", None)
    if uid:
        return uid
    from models import TournamentPlayerStats
    q = session.query(TournamentPlayerStats).filter_by(tournament_id=int(tid))
    pid = getattr(row, "player_id", None)
    hit = None
    if pid:
        hit = q.filter(TournamentPlayerStats.player_id == pid).first()
    if hit is None and getattr(row, "name", None):
        hit = q.filter(TournamentPlayerStats.name == row.name,
                       TournamentPlayerStats.team_name == getattr(row, "team_name", None)
                       ).first()
    return hit.user_id if hit is not None else None


def winners(session, tour):
    """``[(award, team_name, player_name, user_id, value)]`` for a finished final."""
    from models import TournamentMatch, TournamentTeam
    from services import tournament_mvp, tournament_service
    out = []
    final = (session.query(TournamentMatch)
             .filter_by(tournament_id=tour.id, stage="final", status="completed")
             .filter(TournamentMatch.winner_team_id.isnot(None))
             .order_by(TournamentMatch.id.desc()).first())
    if final is None:
        return out
    champ = session.get(TournamentTeam, final.winner_team_id)
    loser_id = final.team2_id if final.winner_team_id == final.team1_id else final.team1_id
    runner = session.get(TournamentTeam, loser_id) if loser_id else None
    for award, team in (("champion", champ), ("runner_up", runner)):
        if team is not None:
            user = _user_for_team(session, team)
            out.append((award, team.name, None, user.id if user else None, None))
    lead = tournament_service.stat_leaders(session, tour.id, limit=1)
    runs = lead.get("most_runs") or []
    if runs and (runs[0].bat_runs or 0) > 0:
        r = runs[0]
        out.append(("orange_cap", r.team_name, r.name, r.user_id, f"{r.bat_runs} runs"))
    wkts = lead.get("most_wickets") or []
    if wkts and (wkts[0].bowl_wickets or 0) > 0:
        r = wkts[0]
        out.append(("purple_cap", r.team_name, r.name, r.user_id,
                    f"{r.bowl_wickets} wkts"))
    mvp = tournament_mvp.mvp_table(session, tour.id, limit=1)
    if mvp:
        r = mvp[0]
        out.append(("mvp", r.team_name, r.name, _user_for_player(session, tour.id, r),
                    f"{r.points:g} pts"))
    return out


# ── Paying out ────────────────────────────────────────────────────────

def finalize(session, tour):
    """Write honours and pay prizes, once. Returns the honour rows (or [])."""
    from models import TournamentHonour, User
    if tour is None or getattr(tour, "awards_given_at", None):
        return []
    rows = winners(session, tour)
    if not rows:
        return []
    table = prizes(tour)
    honours = []
    for award, team_name, player_name, user_id, value in rows:
        p = table.get(award) or {}
        coins, gems = int(p.get("coins") or 0), int(p.get("gems") or 0)
        paid_to = session.get(User, int(user_id)) if user_id else None
        if paid_to is not None and (coins or gems):
            paid_to.total_coins = (paid_to.total_coins or 0) + coins
            paid_to.total_gems = (paid_to.total_gems or 0) + gems
            try:
                from services.activity_service import log_activity
                log_activity(session, paid_to.id, "tournament_prize",
                             f"{AWARD_LABEL[award]} — {tour.name}: "
                             f"+{coins} coins, +{gems} gems")
            except Exception:
                logger.exception("Could not log a tournament prize")
        elif coins or gems:
            # Nobody to pay (an ownerless team). The honour still stands.
            coins = gems = 0
        honour = TournamentHonour(
            tournament_id=tour.id, tournament_name=tour.name or "Tournament",
            award=award, team_name=team_name, player_name=player_name,
            user_id=paid_to.id if paid_to else (int(user_id) if user_id else None),
            value=value, prize_coins=coins, prize_gems=gems)
        session.add(honour)
        honours.append(honour)
    tour.awards_given_at = datetime.utcnow()
    session.flush()
    logger.info("Tournament %s: %s honours written", tour.id, len(honours))
    return honours


def honours_for(session, tid):
    from models import TournamentHonour
    order = {a: i for i, a in enumerate(AWARDS)}
    rows = session.query(TournamentHonour).filter_by(tournament_id=int(tid)).all()
    return sorted(rows, key=lambda h: order.get(h.award, 99))


def render_ceremony(session, tour):
    """The awards ceremony card (HTML)."""
    from models import User
    out = [f"🎉 <b>{escape(tour.name or 'Tournament')} — Awards Ceremony</b>", ""]
    for h in honours_for(session, tour.id):
        who = escape(h.player_name or h.team_name or "—")
        if h.player_name and h.team_name:
            who += f" <i>({escape(h.team_name)})</i>"
        line = f"{AWARD_LABEL.get(h.award, h.award)}: <b>{who}</b>"
        if h.value:
            line += f" — {escape(h.value)}"
        prize = prize_text({"coins": h.prize_coins, "gems": h.prize_gems})
        if prize:
            user = session.get(User, h.user_id) if h.user_id else None
            to = ""
            if user is not None and (user.username or user.first_name):
                to = f" → {escape('@' + user.username if user.username else user.first_name)}"
            line += f"\n   💰 {prize}{to}"
        out.append(line)
    out += ["", "Congratulations to every team and player! 🏏",
            "<i>All past winners: /halloffame</i>"]
    return "\n".join(out)


def recent_honours(session, limit=8):
    """Latest champions with their tournament, for the Hall of Fame."""
    from models import TournamentHonour
    return (session.query(TournamentHonour)
            .order_by(TournamentHonour.awarded_at.desc(), TournamentHonour.id.desc())
            .limit(int(limit) * len(AWARDS)).all())
