"""Hall of Fame — the all-time records of the game.

Two kinds of record live here:

* **Single-match records** (highest score, best figures, most sixes in an
  innings, fastest-scoring innings, highest team total, biggest win). These are
  harvested from the persisted final scorecards (``MatchScorecard``) into
  ``HallOfFameEntry`` rows — once per match, by :func:`scan` — so the boards
  are a cheap indexed read and every record can point back at its match.
  The scan starts from the oldest scorecard, so the history that was already
  played before this feature shipped is back-filled too.
* **Career records** (longest win streak, most wins, most runs / wickets / POTM
  awards for one card, highest ranked rating ever) read straight from the
  tables that already keep them.

Only matches between two human captains are harvested — a record set against
the AI, or in a match flagged for anti stat-farming, is not a record.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Single-match boards: key → (title, emoji). Order is display order.
MATCH_CATEGORIES = {
    "bat_score": ("Highest individual score", "🏏"),
    "strike_rate": ("Fastest innings (min 20 balls)", "⚡"),
    "inn_sixes": ("Most sixes in an innings", "💥"),
    "bowl_figures": ("Best bowling figures", "🎯"),
    "team_total": ("Highest team total", "🏟️"),
    "win_runs": ("Biggest win by runs", "🏆"),
}

# Minimums a performance must reach to be stored at all — the boards only show
# the top few, and this keeps the table to the performances that could matter.
MIN_BAT_SCORE = 30
MIN_SR_BALLS = 20
MIN_SR = 180.0
MIN_SIXES = 4
MIN_WICKETS = 3
MIN_TEAM_TOTAL = 120
MIN_WIN_RUNS = 25

SECTIONS = {
    "bat": ("🏏 Batting", ("bat_score", "strike_rate", "inn_sixes")),
    "bowl": ("🎳 Bowling", ("bowl_figures",)),
    "team": ("🏟️ Team", ("team_total", "win_runs")),
    "career": ("👤 Careers", ()),
}


# ── harvesting ───────────────────────────────────────────────────────

def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def entries_from_scorecard(scorecard, match=None, team_owner=None):
    """Record-worthy entries from one scorecard dict. Pure — no DB.

    ``team_owner`` maps a team name to the user id that fielded it. Returns a
    list of dicts shaped like ``HallOfFameEntry`` columns.
    """
    team_owner = team_owner or {}
    out = []
    innings = [i for i in (scorecard or {}).get("innings") or []
               if not i.get("super_over") and _int(i.get("number"), 1) <= 2]
    for inn in innings:
        bat_team = inn.get("bat_team") or ""
        bowl_team = inn.get("bowl_team") or ""
        bat_owner = team_owner.get(bat_team)
        bowl_owner = team_owner.get(bowl_team)
        for b in inn.get("batting") or []:
            runs, balls = _int(b.get("runs")), _int(b.get("balls"))
            sixes = _int(b.get("sixes"))
            star = "" if b.get("out") else "*"
            name = b.get("name") or ""
            if runs >= MIN_BAT_SCORE:
                out.append(dict(category="bat_score", value=runs, tiebreak=-balls,
                                label=f"{runs}{star} ({balls})", player_name=name,
                                team_name=bat_team, user_id=bat_owner))
            if balls >= MIN_SR_BALLS:
                sr = runs * 100.0 / balls
                if sr >= MIN_SR:
                    out.append(dict(category="strike_rate", value=int(round(sr * 10)),
                                    tiebreak=runs,
                                    label=f"SR {sr:.1f} · {runs}{star} ({balls})",
                                    player_name=name, team_name=bat_team,
                                    user_id=bat_owner))
            if sixes >= MIN_SIXES:
                out.append(dict(category="inn_sixes", value=sixes, tiebreak=runs,
                                label=f"{sixes} sixes · {runs}{star} ({balls})",
                                player_name=name, team_name=bat_team,
                                user_id=bat_owner))
        for w in inn.get("bowling") or []:
            wk, rc = _int(w.get("wickets")), _int(w.get("runs"))
            if wk >= MIN_WICKETS:
                out.append(dict(category="bowl_figures", value=wk, tiebreak=-rc,
                                label=f"{wk}/{rc} ({w.get('overs', '')} ov)",
                                player_name=w.get("name") or "",
                                team_name=bowl_team, user_id=bowl_owner))
        total, wkts = _int(inn.get("runs")), _int(inn.get("wickets"))
        if total >= MIN_TEAM_TOTAL:
            out.append(dict(category="team_total", value=total, tiebreak=-wkts,
                            label=f"{total}/{wkts} ({inn.get('overs', '')} ov)",
                            player_name=None, team_name=bat_team, user_id=bat_owner))
    if match is not None and (match.margin_type or "") == "runs":
        margin = _int(match.margin_value)
        if margin >= MIN_WIN_RUNS:
            winner_team = None
            for team, uid in team_owner.items():
                if uid == match.winner_id:
                    winner_team = team
            out.append(dict(category="win_runs", value=margin, tiebreak=0,
                            label=f"won by {margin} runs", player_name=None,
                            team_name=winner_team, user_id=match.winner_id))
    return out


def _team_owner(session, match, post_row):
    """``{team name: user id}`` for a match, from the hook payload or the toss."""
    owners = {}
    if post_row is not None and post_row.payload_json:
        try:
            owners = {k: v for k, v in (json.loads(post_row.payload_json)
                                        .get("team_owner") or {}).items() if v}
        except Exception:
            owners = {}
    return owners


def _eligible(session, match, post_row):
    from models import User
    from services.match_outcome import END_COMPLETED
    from services.match_rewards import is_ai_user
    if match is None or (match.status or "") != "completed":
        return False
    if match.end_reason not in (END_COMPLETED, None):
        return False
    if post_row is not None and not post_row.counted:
        return False
    for uid in (match.user1_id, match.user2_id):
        if is_ai_user(session.get(User, uid)):
            return False
    return True


def harvest_match(session, match, scorecard_row, post_row=None):
    """Store the Hall of Fame entries for one match. Returns how many."""
    from models import HallOfFameEntry
    if not _eligible(session, match, post_row):
        return 0
    try:
        sc = json.loads(scorecard_row.scorecard_json or "{}")
    except Exception:
        return 0
    owners = _team_owner(session, match, post_row)
    # Old matches have no hook payload; the scorecard's first innings batted
    # first, so the toss columns (where a mode records them) name its owner.
    innings = [i for i in sc.get("innings") or [] if not i.get("super_over")]
    if not owners and match.batting_first_id and innings:
        first = innings[0].get("bat_team")
        other = (match.user2_id if match.batting_first_id == match.user1_id
                 else match.user1_id)
        if first:
            owners[first] = match.batting_first_id
        if len(innings) > 1 and innings[1].get("bat_team"):
            owners[innings[1]["bat_team"]] = other
    rows = entries_from_scorecard(sc, match=match, team_owner=owners)
    when = match.completed_at or scorecard_row.created_at
    for r in rows:
        session.add(HallOfFameEntry(match_id=match.id, achieved_at=when,
                                    **{k: (v[:120] if isinstance(v, str) else v)
                                       for k, v in r.items()}))
    return len(rows)


def scan(session, limit=200):
    """Harvest up to ``limit`` scorecards not yet read. Returns matches scanned.

    Oldest first, so a fresh install back-fills history in batches.
    """
    from models import Match, MatchPostResult, MatchScorecard
    q = (session.query(MatchScorecard, Match, MatchPostResult)
         .join(Match, Match.id == MatchScorecard.match_id)
         .outerjoin(MatchPostResult, MatchPostResult.match_id == Match.id)
         .filter(Match.status == "completed")
         .filter((MatchPostResult.id.is_(None)) | (MatchPostResult.hof_done.is_(False)))
         .order_by(MatchScorecard.id.asc())
         .limit(limit))
    done = 0
    for sc_row, match, post_row in q.all():
        try:
            with session.begin_nested():
                if post_row is None:
                    post_row = MatchPostResult(match_id=match.id, counted=True,
                                               ranked_done=False, rated=False,
                                               hof_done=False)
                    session.add(post_row)
                    session.flush()
                harvest_match(session, match, sc_row, post_row)
                post_row.hof_done = True
            done += 1
        except Exception:
            logger.exception("hall of fame harvest failed for match %s", match.id)
    return done


# ── reading ──────────────────────────────────────────────────────────

def top_entries(session, category, limit=5):
    from models import HallOfFameEntry
    return (session.query(HallOfFameEntry)
            .filter(HallOfFameEntry.category == category)
            .order_by(HallOfFameEntry.value.desc(), HallOfFameEntry.tiebreak.desc(),
                      HallOfFameEntry.achieved_at.asc(), HallOfFameEntry.id.asc())
            .limit(limit).all())


def career_boards(session, limit=5):
    """``[(title, emoji, [(label, holder name, user_id)])]`` for the career page."""
    from sqlalchemy import func
    from models import PlayerGameStats, Player, RankedRating, User

    def humans(q):
        return q.filter(User.telegram_id > 0, User.is_banned.is_(False))

    boards = []
    rows = humans(session.query(User).filter(User.best_streak > 0)) \
        .order_by(User.best_streak.desc(), User.id.asc()).limit(limit).all()
    boards.append(("Longest win streak", "🔥",
                   [(f"{u.best_streak} wins", _user_name(u), u.id) for u in rows]))
    rows = humans(session.query(User).filter(User.matches_won > 0)) \
        .order_by(User.matches_won.desc(), User.id.asc()).limit(limit).all()
    boards.append(("Most match wins", "🏆",
                   [(f"{u.matches_won} wins", _user_name(u), u.id) for u in rows]))
    rows = humans(session.query(RankedRating, User)
                  .join(User, User.id == RankedRating.user_id)
                  .filter(RankedRating.career_peak > 1000)) \
        .order_by(RankedRating.career_peak.desc()).limit(limit).all()
    from services.ranked_service import division_for
    boards.append(("Highest ranked rating", "📈",
                   [(f"{r.career_peak} {division_for(r.career_peak)[1]}",
                     _user_name(u), u.id) for r, u in rows]))
    for col, title, emoji, unit in (
            (PlayerGameStats.runs, "Most runs by one card", "🏏", "runs"),
            (PlayerGameStats.wickets_taken, "Most wickets by one card", "🎳", "wkts"),
            (PlayerGameStats.potm, "Most Player of the Match awards", "🌟", "POTM")):
        rows = humans(session.query(PlayerGameStats, Player, User)
                      .join(Player, Player.id == PlayerGameStats.player_id)
                      .join(User, User.id == PlayerGameStats.user_id)
                      .filter(col > 0)) \
            .order_by(col.desc(), PlayerGameStats.id.asc()).limit(limit).all()
        boards.append((title, emoji,
                       [(f"{getattr(g, col.key)} {unit}",
                         f"{p.name} ({_user_name(u)})", u.id)
                        for g, p, u in rows]))
    return boards


def _user_name(user):
    if user is None:
        return "—"
    return ((user.first_name or "").strip() or (user.team_name or "").strip()
            or (user.username or "Player"))


def holder_names(session, entries):
    """``{user_id: display name}`` for the owners named on a set of entries."""
    from models import User
    ids = {e.user_id for e in entries if e.user_id}
    if not ids:
        return {}
    return {u.id: _user_name(u)
            for u in session.query(User).filter(User.id.in_(ids)).all()}
