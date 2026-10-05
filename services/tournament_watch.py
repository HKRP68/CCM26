"""The tournament watch — everything the bot announces on its own.

A result can land in a tournament five different ways: a played CIPL or Lets
Play match, a Super Over, ``/tsim``, the admin website, or ``/taddmatch``.
Anything that should *follow* a result — the next round's deadline, a round
recap, the playoff bracket, the awards ceremony — would have to be wired into
every one of those paths, and the next path somebody adds would forget. So none
of them do it. Instead a job runs every couple of minutes, looks at each running
tournament, compares what it sees with what it last announced (columns on
``Tournament``: ``round_tracked``, ``round_reminder_stage``, ``recap_round``,
``bracket_posted_key``, ``awards_announced_at``) and returns the posts that are
now due. The job sends them; :func:`tick` itself never touches Telegram, which
is what makes it testable with a fake clock.

Deadlines **never** settle a match. When a round's time runs out the admins are
told (in the group and by DM, with the ``/tsim`` buttons and an extend button);
only an admin decides what happens to an unplayed fixture.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

logger = logging.getLogger(__name__)

# Reminder stages for the open round (``Tournament.round_reminder_stage``).
STAGE_NONE, STAGE_24H, STAGE_2H, STAGE_EXPIRED = 0, 1, 2, 3

# Upper bound on a round's length, so a typo ("4800h") is caught.
MAX_ROUND_HOURS = 24 * 60

# Callback prefix for the "extend the deadline" button.
CB_EXTEND = "tdl_"


@dataclass
class Post:
    """One thing to send. ``chat_id`` None means "no group to post in"."""
    chat_id: int = None
    text: str = ""
    photo: bytes = None
    buttons: list = field(default_factory=list)  # [[(label, callback_data)]]
    # Private copies: [(tg_id, text)]. Sent with the same buttons when
    # ``dm_buttons`` is set (admin alerts), plain otherwise.
    dms: list = field(default_factory=list)
    dm_buttons: bool = False
    kind: str = ""


# ──────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────

def fmt_left(delta):
    """``timedelta`` → "1d 4h", "3h 20m", "12m" — or "overdue"."""
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "overdue"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{max(1, mins)}m"


def deadline_text(tour, now=None):
    """"⏳ ends in 1d 4h" for the open round, or "" when there's no deadline."""
    at = getattr(tour, "round_deadline_at", None)
    if not at:
        return ""
    left = at - (now or datetime.utcnow())
    if left.total_seconds() <= 0:
        return "⏰ deadline passed"
    return f"⏳ ends in {fmt_left(left)}"


def parse_hours(token):
    """"48h" / "48" / "2d" / "+12h" → ``(hours, relative)``. Raises ValueError."""
    t = str(token or "").strip().lower().replace(" ", "")
    relative = t.startswith("+")
    t = t.lstrip("+")
    mult = 1
    if t.endswith("d"):
        mult, t = 24, t[:-1]
    elif t.endswith("h"):
        t = t[:-1]
    if not t.isdigit() or int(t) <= 0:
        raise ValueError("Give a length like 48h, 2d or +12h.")
    hours = int(t) * mult
    if hours > MAX_ROUND_HOURS:
        raise ValueError(f"That's longer than the {MAX_ROUND_HOURS // 24}-day maximum.")
    return hours, relative


def _admin_ids():
    try:
        from services.admin_ids import configured_admin_ids
        return sorted(configured_admin_ids())
    except Exception:
        logger.exception("Could not read the admin list")
        return []


def _fixtures_cmd(tour):
    from services import tournament_service
    return ("/lptfixtures" if tournament_service.tournament_kind(tour)
            == tournament_service.KIND_LETSPLAY else "/ctfixtures")


def _play_cmd(session, tour):
    """The command players start a fixture with, for the reminder cards."""
    from services import tournament_service
    if tournament_service.tournament_kind(tour) == tournament_service.KIND_LETSPLAY:
        return "/lptour"
    try:
        from models import ChallengeLeague
        if tour.league_id:
            lg = session.get(ChallengeLeague, tour.league_id)
            if lg and lg.tournament_command:
                return lg.tournament_command
    except Exception:
        pass
    return tour.command_snapshot or None


# ──────────────────────────────────────────────────────────────────────
# Deadlines
# ──────────────────────────────────────────────────────────────────────

def open_round_pending(session, tour):
    """Unplayed fixtures of the open league round (both teams known)."""
    from services import league_schedule_service as lss
    from services import match_reminder_service as mrs
    rnd = lss.current_round(session, tour.id)
    if rnd is None:
        return rnd, []
    rows = mrs.pending_fixtures(session, tour.id)
    return rnd, [f for f in rows
                 if (f.stage or "") in lss.LEAGUE_STAGES and int(f.round_no or 0) == rnd]


def set_round_hours(session, tour, hours, now=None):
    """Set (or with None, clear) the round length; restarts the open round's clock."""
    now = now or datetime.utcnow()
    if hours is None:
        tour.round_hours = None
        tour.round_deadline_at = None
    else:
        tour.round_hours = int(hours)
        from services import league_schedule_service as lss
        if lss.current_round(session, tour.id) is not None:
            tour.round_deadline_at = now + timedelta(hours=int(hours))
            tour.round_reminder_stage = STAGE_NONE
    session.flush()
    return tour


def extend_deadline(session, tour, hours, now=None):
    """Push the open round's deadline back by ``hours``. Caller commits."""
    from services import league_schedule_service as lss
    now = now or datetime.utcnow()
    if lss.current_round(session, tour.id) is None:
        raise ValueError("No league round is open right now.")
    base = tour.round_deadline_at or now
    if base < now:
        base = now
    tour.round_deadline_at = base + timedelta(hours=int(hours))
    # Reminders that already fired fire again as the new deadline approaches.
    left = tour.round_deadline_at - now
    if left > timedelta(hours=24):
        tour.round_reminder_stage = STAGE_NONE
    elif left > timedelta(hours=2):
        tour.round_reminder_stage = STAGE_24H
    else:
        tour.round_reminder_stage = STAGE_2H
    session.flush()
    return tour


def _reminder_posts(session, tour, rnd, pending, label):
    """The roundup in the group plus the full card to each owner by DM."""
    from services import match_reminder_service as mrs
    nudges = [n for n in mrs.build_nudges(session, tour, pending,
                                         command=_play_cmd(session, tour),
                                         force=True)
              if not n.skipped_until]
    if not nudges:
        return []
    head = (f"⏳ <b>Round {rnd} ends in {label}</b> — "
            f"{escape(tour.name or 'Tournament')}\n")
    if len(nudges) == 1:
        group_text = head + "\n" + nudges[0].text
    else:
        group_text = head + "\n" + mrs.render_roundup(
            session, tour, nudges, command=_play_cmd(session, tour))
    dms = []
    for n in nudges:
        for tg_id, _m in n.recipients:
            dms.append((tg_id, head + "\n" + mrs.render_reminder(
                session, tour, n.fixtures, n.team_a, n.team_b,
                command=_play_cmd(session, tour), for_tg_id=tg_id)))
        mrs.record_send(session, tour, n, chat_id=tour.announce_chat_id)
    return [Post(chat_id=tour.announce_chat_id, text=group_text, dms=dms,
                 kind="reminder")]


def _expired_post(session, tour, rnd, pending):
    """The admin alert: what's unplayed, with /tsim buttons and +24h."""
    teams = {}
    from models import TournamentTeam
    for tt in session.query(TournamentTeam).filter_by(tournament_id=tour.id).all():
        teams[tt.id] = tt.name or "—"
    lines = [f"⏰ <b>Round {rnd} deadline passed</b> — {escape(tour.name or '')}",
             f"{len(pending)} match{'es' if len(pending) != 1 else ''} unplayed:", ""]
    buttons = []
    for fx in pending:
        a, b = teams.get(fx.team1_id, "TBD"), teams.get(fx.team2_id, "TBD")
        tag = f"M{fx.match_no}" if fx.match_no else f"#{fx.id}"
        lines.append(f"<code>{tag}</code> {escape(a)} vs {escape(b)}")
        if len(buttons) < 10:
            buttons.append([(f"✅ {a[:14]}", f"tsim_{fx.id}_1"),
                            (f"✅ {b[:14]}", f"tsim_{fx.id}_2"),
                            (f"🎲 {tag}", f"tsim_{fx.id}_random")])
    buttons.append([("⏳ +24h", f"{CB_EXTEND}{tour.id}_24"),
                    ("⏳ +48h", f"{CB_EXTEND}{tour.id}_48")])
    lines += ["", "Nothing is simulated automatically. A tournament admin can "
                  "settle a match (buttons or <code>/tsim</code>) or give the "
                  "round more time (<code>/tdeadline +24h</code>)."]
    text = "\n".join(lines)
    dms = [(tg, text) for tg in _admin_ids()]
    return Post(chat_id=tour.announce_chat_id, text=text, buttons=buttons,
                dms=dms, dm_buttons=True, kind="expired")


def _deadline_posts(session, tour, now):
    if not tour.round_deadline_at:
        return []
    rnd, pending = open_round_pending(session, tour)
    if rnd is None or not pending:
        return []
    left = tour.round_deadline_at - now
    stage = int(tour.round_reminder_stage or 0)
    hours = int(tour.round_hours or 0)
    if left <= timedelta(0):
        if stage < STAGE_EXPIRED:
            tour.round_reminder_stage = STAGE_EXPIRED
            return [_expired_post(session, tour, rnd, pending)]
        return []
    if left <= timedelta(hours=2):
        if stage < STAGE_2H:
            tour.round_reminder_stage = STAGE_2H
            # A round of 2h or less would fire this the moment it opens.
            if hours > 2 or not hours:
                return _reminder_posts(session, tour, rnd, pending, fmt_left(left))
        return []
    if left <= timedelta(hours=24):
        if stage < STAGE_24H:
            tour.round_reminder_stage = STAGE_24H
            if hours > 24 or not hours:
                return _reminder_posts(session, tour, rnd, pending, fmt_left(left))
    return []


# ──────────────────────────────────────────────────────────────────────
# The tick
# ──────────────────────────────────────────────────────────────────────

def _completed_round(session, tour, rnd):
    """The highest league round that is fully completed, or 0."""
    from sqlalchemy import func
    from models import TournamentMatch
    from services import league_schedule_service as lss
    if rnd is not None:
        return max(0, int(rnd) - 1)
    top = (session.query(func.max(TournamentMatch.round_no))
           .filter(TournamentMatch.tournament_id == tour.id,
                   TournamentMatch.stage.in_(lss.LEAGUE_STAGES)).scalar())
    return int(top or 0)


def _extra_posts(session, tour, now, first_look):
    """Recaps, bracket and ceremony — filled in by their own modules."""
    posts = []
    for name in ("_recap_posts", "_bracket_posts", "_ceremony_posts"):
        fn = globals().get(name)
        if fn is None:
            continue
        try:
            posts += fn(session, tour, now, first_look) or []
        except Exception:
            logger.exception("tournament watch: %s failed for %s", name, tour.id)
    return posts


def tick(session, tour, now=None):
    """Everything now due for one tournament. Updates the bookkeeping columns;
    the caller commits after the posts are handed off."""
    from services import league_schedule_service as lss
    now = now or datetime.utcnow()
    if (tour.status or "").lower() != "active":
        return []
    rnd = lss.current_round(session, tour.id)
    # The first time a tournament is seen, take a silent snapshot: an existing
    # season must not get a flood of recaps for rounds finished long ago.
    first_look = not tour.round_tracked and not tour.recap_round
    posts = []
    if rnd is not None and rnd != tour.round_tracked:
        tour.round_tracked = rnd
        tour.round_reminder_stage = STAGE_NONE
        tour.round_deadline_at = (now + timedelta(hours=int(tour.round_hours))
                                  if tour.round_hours else None)
    elif rnd is None and tour.round_deadline_at:
        tour.round_deadline_at = None
    if first_look:
        tour.recap_round = _completed_round(session, tour, rnd)
        if not tour.round_tracked:
            # A tournament with no league rounds at all: mark it seen anyway.
            tour.round_tracked = rnd or -1
    posts += _deadline_posts(session, tour, now)
    posts += _extra_posts(session, tour, now, first_look)
    session.flush()
    return posts


def running_tournaments(session):
    from models import Tournament
    return (session.query(Tournament)
            .filter(Tournament.status == "active").all())


def tick_all(session, now=None):
    """``[(tournament_id, Post)]`` for every running tournament."""
    out = []
    for tour in running_tournaments(session):
        try:
            for post in tick(session, tour, now=now):
                out.append((tour.id, post))
        except Exception:
            logger.exception("tournament watch failed for %s", tour.id)
    return out
