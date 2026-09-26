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


def _innings_owners(innings, innings_owner, team_owner):
    """``[(bat user id, bowl user id)]`` for each main innings.

    ``innings_owner`` is ``[user who batted first, user who batted second]`` —
    an id per innings, so two captains fielding the same team name are still
    told apart. The name-keyed ``team_owner`` map is only a fallback, and only
    when the two batting sides have different names.
    """
    ids = [u for u in (innings_owner or []) if u]
    if len(ids) == 2:
        return [(ids[i % 2], ids[(i + 1) % 2]) for i in range(len(innings))]
    team_owner = team_owner or {}
    names = [i.get("bat_team") for i in innings]
    if len(set(names)) < len(names):
        return [(None, None)] * len(innings)
    return [(team_owner.get(i.get("bat_team") or ""),
             team_owner.get(i.get("bowl_team") or "")) for i in innings]


def entries_from_scorecard(scorecard, match=None, team_owner=None,
                           innings_owner=None):
    """Record-worthy entries from one scorecard dict. Pure — no DB.

    ``innings_owner`` is ``[user id batting first, user id batting second]``;
    ``team_owner`` (team name → user id) is the fallback when that is unknown.
    Returns a list of dicts shaped like ``HallOfFameEntry`` columns.
    """
    out = []
    innings = [i for i in (scorecard or {}).get("innings") or []
               if not i.get("super_over") and _int(i.get("number"), 1) <= 2]
    owners = _innings_owners(innings, innings_owner, team_owner)
    for inn, (bat_owner, bowl_owner) in zip(innings, owners):
        bat_team = inn.get("bat_team") or ""
        bowl_team = inn.get("bowl_team") or ""
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
            for inn, (bat_owner, _bowl) in zip(innings, owners):
                if bat_owner and bat_owner == match.winner_id:
                    winner_team = inn.get("bat_team")
            out.append(dict(category="win_runs", value=margin, tiebreak=0,
                            label=f"won by {margin} runs", player_name=None,
                            team_name=winner_team, user_id=match.winner_id))
    return out


def _owners(match, post_row):
    """``(innings_owner, team_owner)`` for a match.

    From the post-match payload when the hook ran; otherwise (older matches)
    from the toss columns, which name who batted first where a mode set them.
    """
    payload = {}
    if post_row is not None and post_row.payload_json:
        try:
            payload = json.loads(post_row.payload_json) or {}
        except Exception:
            payload = {}
    players = (match.user1_id, match.user2_id)
    innings_owner = [u for u in (payload.get("innings_owner") or []) if u in players]
    if len(innings_owner) != 2 and match.batting_first_id in players:
        other = (match.user2_id if match.batting_first_id == match.user1_id
                 else match.user1_id)
        innings_owner = [match.batting_first_id, other]
    team_owner = {k: v for k, v in (payload.get("team_owner") or {}).items() if v}
    return (innings_owner if len(innings_owner) == 2 else None), team_owner


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
    innings_owner, team_owner = _owners(match, post_row)
    rows = entries_from_scorecard(sc, match=match, team_owner=team_owner,
                                  innings_owner=innings_owner)
    when = match.completed_at or scorecard_row.created_at
    for r in rows:
        previous_best = _board_top(session, r.get("category"))
        entry = HallOfFameEntry(match_id=match.id, achieved_at=when,
                                **{k: (v[:120] if isinstance(v, str) else v)
                                   for k, v in r.items()})
        session.add(entry)
        _record_news(session, entry, previous_best, when)
    return len(rows)


# Only a record set recently is news — the first scan back-fills years of
# history, and every one of those "new records" is old.
RECORD_NEWS_MAX_AGE_HOURS = 48


def _board_top(session, category):
    if not category:
        return None
    top = top_entries(session, category, limit=1)
    return top[0] if top else None


def _record_news(session, entry, previous_best, when):
    """CMU News when a match takes the #1 spot on a Hall of Fame board."""
    try:
        from datetime import datetime, timedelta
        if previous_best is None or when is None:
            return
        if datetime.utcnow() - when > timedelta(hours=RECORD_NEWS_MAX_AGE_HOURS):
            return
        if (entry.value, entry.tiebreak or 0) <= (previous_best.value,
                                                  previous_best.tiebreak or 0):
            return
        from services.news_service import auto_story
        title, emoji = MATCH_CATEGORIES.get(entry.category, ("Hall of Fame record", "🌟"))
        who = entry.player_name or entry.team_name or "A new name"
        session.flush()
        auto_story(
            session, "hall_of_fame", f"hof:{entry.id}",
            f"{emoji} New record! {who} — {entry.label}",
            f"{who}{f' ({entry.team_name})' if entry.player_name and entry.team_name else ''} "
            f"now holds the Hall of Fame record for {title.lower()}: {entry.label}.\n\n"
            f"The previous best was {previous_best.label}"
            f"{f' by {previous_best.player_name or previous_best.team_name}' if (previous_best.player_name or previous_best.team_name) else ''}.",
            kicker="Hall of Fame")
    except Exception:
        logger.exception("hall of fame news failed")


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
