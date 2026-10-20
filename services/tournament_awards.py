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

The ceremony is presented on demand by /awardceremony.
"""

import json
import logging
from datetime import datetime
from html import escape

logger = logging.getLogger(__name__)

CEREMONY_AWARDS = ("orange_cap", "purple_cap", "most_sixes", "most_fours", "mvp",
                   "player_of_final", "emerging_player", "highest_sr", "best_catch",
                   "most_dots", "winning_captain")
# Preserve team honours/prizes in history, while presentations use only the
# requested eleven categories.
AWARDS = ("champion", "runner_up") + CEREMONY_AWARDS
AWARD_LABEL = {
    "champion": "🏆 Champion", "runner_up": "🥈 Runner-up",
    "orange_cap": "🟠 Orange Cap", "purple_cap": "🟣 Purple Cap",
    "most_sixes": "💥 Most Sixes", "most_fours": "🏏 Most Fours",
    "mvp": "⭐ Most Valuable Player", "player_of_final": "🏅 Player of the Final",
    "emerging_player": "🌟 Emerging Player", "highest_sr": "⚡ Highest Strike Rate",
    "best_catch": "👐 Best Catch", "most_dots": "🎯 Most Dot Balls",
    "winning_captain": "👑 Winning Captain",
}
AWARD_ALIASES = {key: key for key in AWARDS}
AWARD_ALIASES.update({"champions": "champion", "winner": "champion",
    "runnerup": "runner_up", "runner": "runner_up", "orange": "orange_cap",
    "orangecap": "orange_cap", "purple": "purple_cap", "purplecap": "purple_cap",
    "sixes": "most_sixes", "mostsixes": "most_sixes", "fours": "most_fours", "mostfours": "most_fours",
    "playeroffinal": "player_of_final", "final": "player_of_final",
    "emerging": "emerging_player", "emergingplayer": "emerging_player",
    "sr": "highest_sr", "higheststrikerate": "highest_sr", "catch": "best_catch", "bestcatch": "best_catch",
    "dots": "most_dots", "mostdotballs": "most_dots", "captain": "winning_captain", "winningcaptain": "winning_captain"})
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
        raise ValueError("Choose an award from /tprize; examples: orange, purple, sixes, fours, mvp, final, emerging, sr, catch, dots, captain.")
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
    emerging = emerging_player(session, tour)
    if emerging:
        row, rating = emerging
        out.append(("emerging_player", row.team_name, row.name,
                    _user_for_player(session, tour.id, row), f"{row.points:g} pts · rated {rating}"))
    for key, board, field, unit in (("most_sixes", "most_sixes", "bat_sixes", "sixes"),
                                    ("most_fours", "most_fours", "bat_fours", "fours")):
        rows = lead.get(board) or []
        if rows and (getattr(rows[0], field, 0) or 0) > 0:
            row = rows[0]
            out.append((key, row.team_name, row.name, row.user_id, f"{getattr(row, field)} {unit}"))
    rates = lead.get('best_strike_rate') or []
    if rates:
        row, rate = rates[0]
        out.append(('highest_sr', row.team_name, row.name, row.user_id, f'SR {rate:.2f} · {row.bat_runs} runs'))
    lines = next((ls for match, ls in tournament_service.match_scorecards(session, tour.id) if match.id == final.id), [])
    potm = final_player(session, final, lines)
    if potm:
        row = potm
        out.append(('player_of_final', row.get('team_name'), row.get('name'), row.get('user_id'),
                    f"{row.get('bat_runs', 0)} runs · {row.get('bowl_wickets', 0)} wkts"))
    dots = dot_leaders(session, tour.id)
    if dots:
        row = dots[0]
        out.append(('most_dots', row['team_name'], row['name'], row.get('user_id'), f"{row['dots']} dot balls"))
    selected = selected_catch(session, tour)
    if selected:
        out.append(('best_catch', selected['team_name'], selected['name'], selected.get('user_id'),
                    f"Match #{selected['match_no']} · {selected['batter']}"[:60]))
    if champ is not None:
        from models import User
        captain_id = final.host_user_id if final.winner_team_id == final.team1_id else final.target_user_id
        captain = session.get(User, captain_id) if captain_id else _user_for_team(session, champ)
        captain_name = (captain.first_name or captain.username or str(captain.telegram_id)) if captain else champ.owner_name
        out.append(('winning_captain', champ.name, captain_name or 'Winning captain', captain.id if captain else None, 'Tournament-winning captain'))
    present = {row[0] for row in out}
    for key in CEREMONY_AWARDS:
        if key not in present:
            value = 'Selection pending' if key == 'best_catch' else 'No qualifying performance recorded'
            out.append((key, None, None, None, value))
    return out


def final_player(session, final, lines):
    """Use saved match POTM evidence, falling back to the live card's rule."""
    saved = next((l.get('match_potm') for l in lines if l.get('match_potm')), None)
    if saved:
        hit = next((l for l in lines if l.get('name') == saved.get('name') and l.get('team_name') == saved.get('team')), None)
        if hit:
            return hit
    from services import tournament_mvp as M
    from models import TournamentTeam
    team = session.get(TournamentTeam, final.winner_team_id) if final.winner_team_id else None
    ranked = []
    for line in lines:
        if not (line.get('batted') or line.get('bowled')):
            continue
        points = sum(M.line_impact(line)) + (15 if team and line.get('team_name') == team.name else 0)
        ranked.append((points, line))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked:
        return None
    if team and len(ranked) >= 2 and ranked[0][0] - ranked[1][0] <= 50:
        winning = [r for r in ranked[:2] if r[1].get('team_name') == team.name]
        if winning:
            return winning[0][1]
    return ranked[0][1]


def _verified_cards(session, tid):
    from services import tournament_service as TS, record_telemetry as T
    for match, lines in TS.match_scorecards(session, tid):
        if match.status != 'completed':
            continue
        detail = next((l.get('record_detail') for l in lines if l.get('record_detail')), None)
        evidence = T.analyse(detail, lines=lines) if detail else {}
        if evidence.get('invalid_reason'):
            evidence = {}
        yield match, lines, evidence


def dot_leaders(session, tid):
    totals = {}
    for match, lines, evidence in _verified_cards(session, tid):
        for line in lines:
            if not line.get('bowled'):
                continue
            if 'bowl_dots' in line:
                count = int(line['bowl_dots'] or 0)
            else:
                count = sum(1 for e in evidence.get('deliveries', []) if e.get('bowler') == line.get('name')
                            and e.get('opponent') == line.get('team_name') and e.get('legal') and e.get('team_runs') == 0)
            key = (line.get('user_id'), line.get('player_id') or line.get('roster_id'), line.get('name'), line.get('team_name'))
            row = totals.setdefault(key, dict(line, dots=0))
            row['dots'] += count
    return sorted((r for r in totals.values() if r['dots'] > 0), key=lambda r: (-r['dots'], r.get('name') or ''))


def catch_choices(session, tid):
    choices = []
    for match, lines, evidence in _verified_cards(session, tid):
        for e in evidence.get('deliveries', []):
            if not e.get('wicket') or not e.get('fielder') or not ('caught' in (e.get('how') or '') or e.get('how') in ('c&b', 'c & b')):
                continue
            line = next((l for l in lines if l.get('name') == e['fielder'] and l.get('team_name') == e.get('opponent')), {})
            choices.append(dict(key=f"{match.id}:{e['seq']}", name=e['fielder'], team_name=e.get('opponent'),
                user_id=line.get('user_id'), player_id=line.get('player_id'), batter=e.get('batter') or 'Batter',
                match_no=match.match_no or match.match_id or match.id))
    return choices


def selected_catch(session, tour):
    try:
        key = json.loads(tour.best_catch_json or '{}').get('key')
    except (ValueError, TypeError):
        return None
    return next((c for c in catch_choices(session, tour.id) if c['key'] == key), None)


def set_best_catch(session, tour, key):
    if tour.awards_announced_at:
        raise ValueError('The ceremony has already finished.')
    if key and not any(c['key'] == key for c in catch_choices(session, tour.id)):
        raise ValueError('Choose a recorded catch from this tournament.')
    tour.best_catch_json = json.dumps({'key': key}) if key else None
    session.flush()


def player_for_stats(session, tour, row):
    """Resolve only within this tournament's roster, never another player's name."""
    from models import Player, ChallengePlayer, TournamentTeam
    if row is None:
        return None
    if row.player_id:
        return session.get(Player, row.player_id)
    team = session.query(TournamentTeam).filter_by(tournament_id=tour.id, name=row.team_name).first()
    if team and team.challenge_team_id:
        cp = session.query(ChallengePlayer).filter_by(team_id=team.challenge_team_id, name=row.name).first()
        if cp and cp.source_player_id:
            return session.get(Player, cp.source_player_id)
    return None


def emerging_player(session, tour):
    """Best MVP impact among players rated <=83; lower rating breaks equal impact.

    Require at least half the most appearances (rounded up). Unknown ratings
    cannot win, and prizes use the same persisted honours as every other award.
    """
    from models import ChallengePlayer, TournamentTeam
    from services.tournament_mvp import mvp_rows
    rows = mvp_rows(session, tour.id)
    minimum = max(1, (max((r.matches for r in rows), default=0) + 1) // 2)
    candidates = []
    for row in rows:
        player = player_for_stats(session, tour, row)
        rating = player.rating if player else None
        if rating is None:
            team = session.query(TournamentTeam).filter_by(tournament_id=tour.id, name=row.team_name).first()
            cp = session.query(ChallengePlayer).filter_by(team_id=team.challenge_team_id, name=row.name).first() if team and team.challenge_team_id else None
            if cp:
                try:
                    rating = int(json.loads(cp.details_json or '{}').get('rating'))
                except (ValueError, TypeError):
                    pass
        if rating is not None and 1 <= int(rating) <= 83 and row.matches >= minimum and row.points > 0:
            candidates.append((row, int(rating)))
    return max(candidates, key=lambda pair: (pair[0].points, -pair[1], pair[0].name or '')) if candidates else None


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


def ensure_ceremony_honours(session, tour):
    """Backfill newly introduced awards once, including already-finished finals."""
    from models import Tournament, TournamentHonour, User
    # Serialize upgrades and prize payments with the tournament row on Postgres.
    session.query(Tournament).filter_by(id=tour.id).with_for_update().one()
    existing = {h.award: h for h in honours_for(session, tour.id)}
    for award, team, player, uid, value in winners(session, tour):
        if award not in CEREMONY_AWARDS:
            continue
        h = existing.get(award)
        if h and (h.player_name or h.team_name or h.announced_at):
            continue
        if h is None:
            h = TournamentHonour(tournament_id=tour.id, tournament_name=tour.name, award=award)
            session.add(h)
        h.team_name, h.player_name, h.user_id, h.value = team, player, uid, value
        if uid and (team or player) and not (h.prize_coins or h.prize_gems):
            prize = prizes(tour).get(award) or {}
            user = session.get(User, uid)
            if user:
                h.prize_coins, h.prize_gems = prize.get('coins', 0), prize.get('gems', 0)
                user.total_coins = (user.total_coins or 0) + h.prize_coins
                user.total_gems = (user.total_gems or 0) + h.prize_gems
    session.flush()


def ceremony_honours(session, tid):
    rows = {h.award: h for h in honours_for(session, tid)}
    return [rows[key] for key in CEREMONY_AWARDS if key in rows]


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
