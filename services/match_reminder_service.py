"""Nudging two teams to go and play the fixture they still owe each other.

A scheduled tournament stalls in one very ordinary way: two people each think
the other will start the match. Nobody is being difficult — the fixture is
sitting on a list neither of them has opened today. ``/ctfixtures`` cannot fix
that, because it only speaks to whoever already typed it.

This builds the message that goes the other way:

    🏏 PENDING MATCH — Summer Trophy
    🟠 Sunrisers Hyderabad  vs  🩷 Rajasthan Royals
    M14 · 🏠 SRH · 🌱 Dusty

    🟠 @the_boss 🤝 @cookinmeth
    🩷 @someone_else

    ⏳ 1 league match left between you.
    🎮 Play it now — reply to your opponent and use /cipl
    📅 Full schedule: /clsd SRH · /clsd RR

and works out who it has to reach: the owner **and every co-owner** of both
sides, because a franchise run by two people is stalled by whichever of them
didn't see it.

Three things here exist only to keep a reminder from becoming spam, which is
what this feature turns into if nobody thinks about it:

* **A cooldown per fixture.** ``TournamentMatchReminder`` records every send;
  a fixture reminded inside ``COOLDOWN`` is skipped unless the sender overrides
  it deliberately.
* **Each person is messaged once.** Someone who co-owns two stalled teams gets
  one DM carrying both fixtures, not two DMs.
* **It only ever names people who are in the fixture.** No @everyone, no
  broadcast to the league.

Nothing here talks to Telegram: it finds, renders and records. The sending —
group post, DM fan-out, delivery report — is ``handlers.match_reminders``, which
keeps this module testable without a bot.
"""

import logging
from datetime import datetime, timedelta
from html import escape

from models import (
    ChallengeTeam, TournamentMatch, TournamentMatchReminder, TournamentTeam, User,
)
from services import tournament_service

logger = logging.getLogger(__name__)

# How long a fixture is left alone after being reminded about. Long enough that
# a second nudge means something, short enough to catch a stalled weekend.
COOLDOWN = timedelta(hours=12)

# Fixtures still to be played. A "live" one is already under way — reminding
# people to start a match they are in the middle of is noise.
PENDING_STATUSES = ("scheduled",)

_STAGE_LABEL = {
    "league": "league", "group": "group",
    "quarterfinal": "quarter-final", "semifinal": "semi-final",
    "qualifier1": "Qualifier 1", "eliminator": "Eliminator",
    "qualifier2": "Qualifier 2", "final": "final",
    "round_of_16": "Round of 16", "round_of_32": "Round of 32",
}


# ══════════════════════════════════════════════════════════════════════
# Team badges
# ══════════════════════════════════════════════════════════════════════
#
# The two sides need telling apart at a glance in a chat full of text. A
# Challenge League team already carries the franchise's ``primary_color``, so the
# badge is the coloured circle nearest to it — SRH comes out orange, RR pink,
# without anybody entering an emoji anywhere. A team with no colour set (every
# Lets Play team, for one) gets a stable circle derived from its name instead:
# arbitrary, but the same every time, which is all a badge has to be.

_PALETTE = (
    ("🔴", (0xE5, 0x39, 0x35)), ("🟠", (0xF5, 0x7C, 0x00)),
    ("🟡", (0xFD, 0xD8, 0x35)), ("🟢", (0x43, 0xA0, 0x47)),
    ("🔵", (0x1E, 0x88, 0xE5)), ("🟣", (0x8E, 0x24, 0xAA)),
    ("🟤", (0x6D, 0x4C, 0x41)), ("⚫", (0x21, 0x21, 0x21)),
    ("⚪", (0xF5, 0xF5, 0xF5)), ("🩷", (0xEC, 0x40, 0x7A)),
    ("🩵", (0x4D, 0xD0, 0xE1)), ("🩶", (0x9E, 0x9E, 0x9E)),
)
# The circles a nameless team falls back to: the palette minus the two that read
# as "no colour" rather than as a badge.
_FALLBACK = tuple(e for e, _ in _PALETTE if e not in ("⚪", "🩶"))


def _parse_hex(value):
    """``"#E53935"`` → ``(229, 57, 53)``; ``None`` for anything else."""
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) not in (6, 8):
        return None
    try:
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def team_badge(session, team):
    """A coloured circle for one team — its franchise colour, or its name."""
    rgb = None
    if getattr(team, "challenge_team_id", None):
        try:
            ct = session.query(ChallengeTeam).get(int(team.challenge_team_id))
            rgb = _parse_hex(getattr(ct, "primary_color", None)) if ct else None
        except Exception:
            logger.debug("Team badge colour lookup failed", exc_info=True)
    if rgb is not None:
        return min(_PALETTE,
                   key=lambda item: sum((a - b) ** 2
                                        for a, b in zip(rgb, item[1])))[0]
    name = (team.name or "").strip().casefold()
    return _FALLBACK[sum(name.encode()) % len(_FALLBACK)] if name else "⚪"


# ══════════════════════════════════════════════════════════════════════
# Finding what still has to be played
# ══════════════════════════════════════════════════════════════════════

def pending_fixtures(session, tournament_id, team_id=None, opponent_id=None):
    """Unplayed fixtures, in schedule order.

    ``team_id`` narrows to one side's fixtures and ``opponent_id`` to the ones
    between that pair. Both sides must be known: a knockout slot still reading
    "Winner of Q1" has nobody to remind yet.
    """
    q = (session.query(TournamentMatch)
         .filter(TournamentMatch.tournament_id == int(tournament_id),
                 TournamentMatch.status.in_(PENDING_STATUSES),
                 TournamentMatch.team1_id.isnot(None),
                 TournamentMatch.team2_id.isnot(None)))
    if team_id is not None:
        q = q.filter((TournamentMatch.team1_id == int(team_id))
                     | (TournamentMatch.team2_id == int(team_id)))
    if opponent_id is not None:
        q = q.filter((TournamentMatch.team1_id == int(opponent_id))
                     | (TournamentMatch.team2_id == int(opponent_id)))
    return q.order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                      TournamentMatch.id).all()


def last_reminded(session, fixture_ids):
    """``{fixture_id: datetime}`` of the most recent reminder for each fixture."""
    ids = [int(i) for i in fixture_ids if i is not None]
    if not ids:
        return {}
    rows = (session.query(TournamentMatchReminder)
            .filter(TournamentMatchReminder.tournament_match_id.in_(ids))
            .order_by(TournamentMatchReminder.sent_at).all())
    return {r.tournament_match_id: r.sent_at for r in rows}


def on_cooldown(sent_at, now=None, cooldown=COOLDOWN):
    """Whether a fixture reminded at ``sent_at`` is still inside its quiet window."""
    if sent_at is None:
        return False
    return (now or datetime.utcnow()) - sent_at < cooldown


# ══════════════════════════════════════════════════════════════════════
# Who to reach
# ══════════════════════════════════════════════════════════════════════

def _mention(tg_id, display_name):
    """One person as Telegram HTML.

    An ``@username`` is used where the bot has seen one, because that is what
    people recognise in a chat and it notifies them. Otherwise an inline
    ``tg://user`` link on their name does the same job — and unlike a raw id, it
    still reads as a person.
    """
    name = (display_name or "").strip() or f"User {tg_id}"
    return f'<a href="tg://user?id={int(tg_id)}">{escape(name)}</a>'


def team_contacts(session, team):
    """``[(tg_id, mention_html), ...]`` for everyone who runs this team.

    Owner first, then co-owners in the order the admin entered them — the owner
    is the name on the team, so they read first. A Lets Play team *is* a user, so
    ``user_tg_id`` counts as running it. Duplicates are dropped: someone listed
    both as owner and co-owner is one person and gets one mention.
    """
    ids = []
    for candidate in ([team.owner_tg_id, getattr(team, "user_tg_id", None)]
                      + list(tournament_service.co_owner_ids(team))):
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return []

    users = {u.telegram_id: u for u in
             session.query(User).filter(User.telegram_id.in_(ids)).all()}
    out = []
    for tg_id in ids:
        user = users.get(tg_id)
        username = (getattr(user, "username", "") or "").strip().lstrip("@")
        if username:
            out.append((tg_id, f"@{escape(username)}"))
            continue
        name = (getattr(user, "first_name", "") or "").strip()
        if not name and tg_id == team.owner_tg_id:
            name = (team.owner_name or "").strip()
        out.append((tg_id, _mention(tg_id, name)))
    return out


# ══════════════════════════════════════════════════════════════════════
# The card
# ══════════════════════════════════════════════════════════════════════

def _short(team):
    """The handle a player would type into /clsd for this team."""
    return (team.short_name or "").strip() or (team.name or "").strip()


def _fixture_line(fixture, home_name):
    """The match's own line: its number, who is at home, what it's played on."""
    bits = []
    if fixture.match_no:
        bits.append(f"M{fixture.match_no}")
    stage = (fixture.stage or "league").lower()
    if stage not in ("league", "group"):
        bits.append(_STAGE_LABEL.get(stage, stage.replace("_", " ").title()))
    if home_name:
        bits.append(f"🏠 {escape(home_name)}")
    pitch = (fixture.pitch_type or "").strip()
    if pitch:
        bits.append(f"🌱 {escape(pitch)}")
    return " · ".join(bits)


def render_reminder(session, tour, fixtures, team_a, team_b, command=None,
                    for_tg_id=None):
    """The reminder card for one pair of teams, covering everything they owe.

    ``fixtures`` is every unplayed match between these two — a double
    round-robin leaves two, and telling somebody about one of them just means
    doing this again next week.

    ``for_tg_id`` personalises the DM copy: the person reading it is told which
    of the two sides is theirs, so a co-owner of one team in a chat full of
    franchise names knows instantly that this one is on them.
    """
    badge_a, badge_b = team_badge(session, team_a), team_badge(session, team_b)
    name_a, name_b = escape(team_a.name or "—"), escape(team_b.name or "—")
    contacts_a, contacts_b = team_contacts(session, team_a), team_contacts(session, team_b)

    out = [f"🏏 <b>PENDING MATCH</b> — {escape(tour.name or 'Tournament')}", "",
           f"{badge_a} <b>{name_a}</b>  vs  {badge_b} <b>{name_b}</b>"]

    names = {tt.id: tt.name for tt in (team_a, team_b)}
    for fixture in fixtures:
        line = _fixture_line(fixture, names.get(fixture.home_team_id))
        if line:
            out.append(f"<code>{line}</code>")

    out.append("")
    for badge, contacts in ((badge_a, contacts_a), (badge_b, contacts_b)):
        if contacts:
            mentions = "  🤝 ".join(m for _tg, m in contacts)
            out.append(f"{badge} {mentions}")
        else:
            out.append(f"{badge} <i>nobody is assigned to this team yet</i>")

    yours = None
    if for_tg_id is not None:
        if tournament_service.is_team_member(team_a, for_tg_id):
            yours = team_a
        elif tournament_service.is_team_member(team_b, for_tg_id):
            yours = team_b
    if yours is not None:
        out += ["", f"👉 You run <b>{escape(yours.name or '—')}</b> in this one."]

    count = len(fixtures)
    stages = {(f.stage or "league").lower() for f in fixtures}
    # "league match" only while every fixture in the pair is one — a semi-final
    # and a league game together are just "matches".
    noun = "league match" if stages <= {"league", "group"} else "match"
    if count != 1:
        noun = noun.replace("match", "matches")
    out += ["", f"⏳ You still have <b>{count}</b> {noun} left against each other."]
    if command:
        cmd = escape(command if command.startswith("/") else f"/{command}")
        out.append(f"🎮 <b>Play it now</b> — reply to your opponent and use "
                   f"<code>{cmd}</code>")
    else:
        out.append("🎮 <b>Play it now</b> — reply to your opponent and use the "
                   "tournament command.")
    out.append(f"📅 Full schedule: <code>/clsd {escape(_short(team_a))}</code> · "
               f"<code>/clsd {escape(_short(team_b))}</code>")
    return "\n".join(out)


def render_roundup(session, tour, nudges, command=None):
    """One group message covering several stalled pairs.

    A reminder about a single pair is best as the full card — it is the whole
    message, and it reads like one. Eight of them posted back to back is a
    flood, and a flood is skipped. So once there is more than one pair the group
    gets this instead: every pair on one line with the people who owe it, while
    the full card still goes to those people privately.
    """
    out = [f"🏏 <b>PENDING MATCHES</b> — {escape(tour.name or 'Tournament')}",
           f"<i>{len(nudges)} fixtures are still waiting to be played.</i>", ""]
    for nudge in nudges:
        badge_a = team_badge(session, nudge.team_a)
        badge_b = team_badge(session, nudge.team_b)
        left = len(nudge.fixtures)
        out.append(f"{badge_a} <b>{escape(nudge.team_a.name or '—')}</b> vs "
                   f"{badge_b} <b>{escape(nudge.team_b.name or '—')}</b>"
                   + (f"  ×{left}" if left > 1 else ""))
        mentions = "  ".join(m for _tg, m in nudge.recipients)
        out.append(f"   {mentions}" if mentions
                   else "   <i>nobody is assigned to these teams yet</i>")
    out.append("")
    if command:
        cmd = escape(command if command.startswith("/") else f"/{command}")
        out.append(f"🎮 <b>Play them now</b> — reply to your opponent and use "
                   f"<code>{cmd}</code>")
    else:
        out.append("🎮 <b>Play them now</b> — reply to your opponent and use the "
                   "tournament command.")
    out.append("📅 Your own schedule: <code>/clsd &lt;team name&gt;</code>")
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════
# Putting a send together
# ══════════════════════════════════════════════════════════════════════

class Nudge:
    """One pair of teams, the fixtures they owe, and who has to hear about it."""

    def __init__(self, team_a, team_b, fixtures):
        self.team_a = team_a
        self.team_b = team_b
        self.fixtures = fixtures
        self.recipients = []   # [(tg_id, mention_html), ...] — both sides
        self.text = ""         # the group card
        self.skipped_until = None  # set when the pair is inside its cooldown

    @property
    def fixture_ids(self):
        return [f.id for f in self.fixtures]

    @property
    def title(self):
        return f"{self.team_a.name or '—'} vs {self.team_b.name or '—'}"


def build_nudges(session, tour, fixtures, command=None, force=False, now=None):
    """Group pending fixtures into one nudge per pair of teams.

    Grouping is the point: two teams with two legs left owe each other *two
    matches*, and that is one conversation, not two notifications. A pair whose
    most recent reminder is inside the cooldown comes back flagged rather than
    dropped, so the caller can say what it is holding back instead of silently
    sending less than it was asked to.
    """
    teams = {tt.id: tt for tt in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
    now = now or datetime.utcnow()
    last = last_reminded(session, [f.id for f in fixtures])

    grouped = {}
    for fixture in fixtures:
        a, b = teams.get(fixture.team1_id), teams.get(fixture.team2_id)
        if not a or not b:
            continue  # a team removed from the tournament mid-schedule
        key = tuple(sorted((a.id, b.id)))
        grouped.setdefault(key, (a, b, []))[2].append(fixture)

    out = []
    for (a, b, pair_fixtures) in grouped.values():
        nudge = Nudge(a, b, pair_fixtures)
        if not force:
            # The pair is quiet only while EVERY fixture in it is quiet: a newly
            # scheduled second leg is worth a nudge even if the first was
            # mentioned this morning.
            sent_times = [last.get(f.id) for f in pair_fixtures]
            if sent_times and all(on_cooldown(t, now) for t in sent_times):
                nudge.skipped_until = max(t for t in sent_times) + COOLDOWN
                out.append(nudge)
                continue
        seen = set()
        for tg_id, mention in (team_contacts(session, a)
                               + team_contacts(session, b)):
            if tg_id not in seen:
                seen.add(tg_id)
                nudge.recipients.append((tg_id, mention))
        nudge.text = render_reminder(session, tour, pair_fixtures, a, b,
                                     command=command)
        out.append(nudge)
    return out


def record_send(session, tour, nudge, sent_by_tg_id=None, chat_id=None,
                delivered=0, failed=0, now=None):
    """Log one nudge against every fixture it covered — the cooldown's memory."""
    stamp = now or datetime.utcnow()
    for fixture in nudge.fixtures:
        session.add(TournamentMatchReminder(
            tournament_id=tour.id, tournament_match_id=fixture.id,
            sent_by_tg_id=sent_by_tg_id, chat_id=chat_id,
            recipients=len(nudge.recipients), delivered=int(delivered),
            failed=int(failed), sent_at=stamp))
