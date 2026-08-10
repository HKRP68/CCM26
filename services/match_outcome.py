"""How a match ended, and what kind of match it was.

The ops pages ask two questions about every match — "what kind of game was
this?" and "how did it finish?" — and until now both had to be guessed from
side effects. Every terminal path wrote ``status = "completed"``, so a row with
a NULL ``winner_id`` could equally be a tie, a forfeit, an ``/endmatch``, or a
``/clearmatches`` sweep; nothing recorded whether a human pressed a button or a
cleanup job swept the row an hour later.

``Match.match_type`` and ``Match.end_reason`` now record both directly, written
at the creation and termination sites. Rows written before those columns
existed still render: :func:`derive_end_reason` and :func:`match_type_label`
reconstruct a best-effort answer from the fields that did exist, and flag it as
inferred so the admin table never presents a guess as a recorded fact.
"""

from sqlalchemy import and_, func, or_


# ══════════════════════════════════════════════════════════════════════
# END REASONS
# ══════════════════════════════════════════════════════════════════════

END_COMPLETED = "completed"            # played to a result
END_ENDED_BY_USER = "ended_by_user"    # /endmatch — a player forfeited it
END_CLEARED_BY_USER = "cleared_by_user"  # /clearmatches, /removematch
END_ENDED_BY_ADMIN = "ended_by_admin"  # admin panel force-end
END_AUTO = "auto_ended"                # cleanup job: stale invite / stuck match
END_UNKNOWN = "unknown"                # legacy row, ended before this was tracked

END_REASONS = (END_COMPLETED, END_ENDED_BY_USER, END_CLEARED_BY_USER,
               END_ENDED_BY_ADMIN, END_AUTO, END_UNKNOWN)

END_REASON_LABELS = {
    END_COMPLETED: "Completed",
    END_ENDED_BY_USER: "Ended by user",
    END_CLEARED_BY_USER: "Cleared by user",
    END_ENDED_BY_ADMIN: "Ended by admin",
    END_AUTO: "Automatically ended",
    END_UNKNOWN: "Unrecorded",
}

# Short explanations for the admin table's legend / tooltips.
#
# The reason answers "what stopped this match being playable", not "was there a
# winner" — the Winner column already answers that. So a timeout forfeit is
# Automatically ended (the idle timer stopped it) even though it names a
# winner, and a Mini App quit is Ended by user for the same reason in reverse.
END_REASON_HINTS = {
    END_COMPLETED: "Played out to a result — including a tie, super over or bowl-out.",
    END_ENDED_BY_USER: "A player stopped it: /endmatch, or the Mini App's forfeit button.",
    END_CLEARED_BY_USER: "Wiped with /clearmatches or /removematch — no winner.",
    END_ENDED_BY_ADMIN: "Force-ended from the admin panel.",
    END_AUTO: "Stopped by the system: an idle-player timeout, or the cleanup sweep.",
    END_UNKNOWN: "Finished before end reasons were recorded.",
}

# Tone keys the template maps to colours.
END_REASON_TONES = {
    END_COMPLETED: "good",
    END_ENDED_BY_USER: "warn",
    END_CLEARED_BY_USER: "warn",
    END_ENDED_BY_ADMIN: "bad",
    END_AUTO: "bad",
    END_UNKNOWN: "muted",
}

END_REASON_ICONS = {
    END_COMPLETED: "🏆",
    END_ENDED_BY_USER: "🛑",
    END_CLEARED_BY_USER: "🧹",
    END_ENDED_BY_ADMIN: "🛡",
    END_AUTO: "⏱",
    END_UNKNOWN: "❔",
}

# Statuses that mean the row is no longer playable but was never a real result.
DEAD_STATUSES = ("abandoned", "expired", "cancelled")

# A completed row with no ``winner_id`` is still a genuine result when the match
# ended in one of these ways — none of them name a winning side on the Match row.
#
# Both spellings of super over are here on purpose: handlers/super_over.py
# writes "super over", while sim_match.py, the Mini App finaliser and
# handlers/match.py's own super-over branch all write "super_over".
#
# "cancelled" (a no-progress Mini App cancel) is deliberately NOT here. It is a
# margin that means *nothing happened*, not a result reached without a named
# winner, and listing it would report those rows as Completed.
RESULTLESS_MARGINS = ("tie", "tied", "super over", "super_over",
                      "bowl-out", "forfeit")


# ══════════════════════════════════════════════════════════════════════
# MATCH TYPES
# ══════════════════════════════════════════════════════════════════════

TYPE_CRIC = "cric"
TYPE_PLAYMATCH = "playmatch"
TYPE_WSP = "wsp"
TYPE_CHALLENGE = "challenge"
TYPE_LETSPLAY = "letsplay"
TYPE_CIPL = "cipl"
TYPE_VSBOT = "vsbot"
TYPE_WSPBOT = "wspbot"
TYPE_WPMBOT = "wpmbot"
TYPE_BOTMATCH = "botmatch"
TYPE_BOTVSBOT = "botvsbot"

MATCH_TYPE_LABELS = {
    TYPE_CRIC: "Cric",
    TYPE_PLAYMATCH: "Playmatch",
    TYPE_WSP: "WSP",
    TYPE_CHALLENGE: "Challenge League",
    TYPE_LETSPLAY: "Lets Play",
    TYPE_CIPL: "CIPL",
    TYPE_VSBOT: "vs AI",
    TYPE_WSPBOT: "WSP vs AI",
    TYPE_WPMBOT: "Mini App vs AI",
    TYPE_BOTMATCH: "AI vs AI",
    TYPE_BOTVSBOT: "AI vs AI",
}

# "pvp" | "ai" | "spectate" — drives the colour of the type pill and lets the
# filter offer three coarse buckets alongside the exact modes.
MATCH_TYPE_FAMILY = {
    TYPE_CRIC: "pvp",
    TYPE_PLAYMATCH: "pvp",
    TYPE_WSP: "pvp",
    TYPE_CHALLENGE: "pvp",
    TYPE_LETSPLAY: "pvp",
    TYPE_CIPL: "pvp",
    TYPE_VSBOT: "ai",
    TYPE_WSPBOT: "ai",
    TYPE_WPMBOT: "ai",
    TYPE_BOTMATCH: "spectate",
    TYPE_BOTVSBOT: "spectate",
}


# ══════════════════════════════════════════════════════════════════════
# WRITING
# ══════════════════════════════════════════════════════════════════════

def mark_end(match, reason, by_user_id=None):
    """Record *why* ``match`` finished.

    Deliberately leaves ``status`` and ``completed_at`` alone: the callers own
    those (they mean different things per mode, and other queries filter on
    them), and this should be droppable next to an existing assignment without
    changing any behaviour beyond the bookkeeping.
    """
    if match is None:
        return match
    match.end_reason = reason
    if by_user_id is not None:
        match.ended_by_id = by_user_id
    return match


# ══════════════════════════════════════════════════════════════════════
# READING
# ══════════════════════════════════════════════════════════════════════

def derive_end_reason(match):
    """The end reason for ``match``, falling back to inference on old rows.

    Returns ``(reason, is_inferred)`` so callers can show a recorded reason
    plainly and mark a reconstructed one as a guess.
    """
    recorded = getattr(match, "end_reason", None)
    if recorded:
        return recorded, False

    status = (getattr(match, "status", None) or "").lower()
    if status in DEAD_STATUSES:
        return END_AUTO, True
    if status == "completed":
        margin = (getattr(match, "margin_type", None) or "").lower()
        if getattr(match, "winner_id", None) or margin in RESULTLESS_MARGINS:
            return END_COMPLETED, True
        # Completed, nobody won, no result-shaped margin: this is the shape
        # /clearmatches and /removematch leave behind, but /endmatch leaves it
        # too. Don't pick one — say so.
        return END_UNKNOWN, True
    return END_UNKNOWN, True


def end_reason_label(reason):
    """Display text for an end reason, tidying up anything unrecognised."""
    return END_REASON_LABELS.get(reason, (reason or "—").replace("_", " ").title())


def match_type_label(match, opponent_is_bot=False, both_bots=False):
    """Human label for the mode, as ``(label, is_inferred)``.

    Old rows carry no ``match_type``; the two facts that survive on them are
    whether either side is the bot user, which is enough for a coarse but
    honest answer.
    """
    recorded = getattr(match, "match_type", None)
    if recorded:
        return MATCH_TYPE_LABELS.get(recorded, recorded.replace("_", " ").title()), False
    if both_bots:
        return "AI vs AI", True
    if opponent_is_bot:
        return "vs AI", True
    return "PvP", True


def match_type_family(match, opponent_is_bot=False, both_bots=False):
    """Coarse bucket for the mode — ``pvp`` / ``ai`` / ``spectate``.

    Drives the colour of the type pill, and falls back the same way
    :func:`match_type_label` does on rows with no recorded mode.
    """
    recorded = getattr(match, "match_type", None)
    if recorded:
        return MATCH_TYPE_FAMILY.get(recorded, "pvp")
    if both_bots:
        return "spectate"
    return "ai" if opponent_is_bot else "pvp"


# ══════════════════════════════════════════════════════════════════════
# QUERYING
# ══════════════════════════════════════════════════════════════════════

def end_reason_filter(Match, reason):
    """A SQLAlchemy clause selecting matches that ended for ``reason``.

    Mirrors :func:`derive_end_reason` exactly, so filtering by "Completed"
    finds the old rows the table *displays* as completed rather than only the
    ones written since the column landed. Returns ``None`` for an unknown
    reason so callers can treat that as "no filter".
    """
    if reason not in END_REASONS:
        return None

    untagged = Match.end_reason.is_(None)
    # ``margin_type IN (...)`` is NULL — not false — for a NULL margin, and a
    # NULL survives the ``~`` below as a NULL, quietly dropping every row it
    # touches. Pin it to false first so the negated branch works.
    #
    # ``derive_end_reason`` lowercases the margin before comparing, and SQL
    # ``IN`` is case-sensitive on Postgres — lower() here so a stray "Tie"
    # can't land in a different bucket than the one the table shows.
    has_result = or_(Match.winner_id.isnot(None),
                     and_(Match.margin_type.isnot(None),
                          func.lower(Match.margin_type).in_(RESULTLESS_MARGINS)))
    # Any status is dead-or-alive; NULL compares as neither, so spell it out.
    is_dead = Match.status.in_(DEAD_STATUSES)
    not_dead = or_(Match.status.is_(None), Match.status.notin_(DEAD_STATUSES))
    not_completed = or_(Match.status.is_(None), Match.status != "completed")

    if reason == END_COMPLETED:
        return or_(Match.end_reason == END_COMPLETED,
                   and_(untagged, Match.status == "completed", has_result))
    if reason == END_AUTO:
        return or_(Match.end_reason == END_AUTO, and_(untagged, is_dead))
    if reason == END_UNKNOWN:
        # Everything left over: a completed row with nothing to show for it,
        # AND any other non-dead status, which derive_end_reason falls through
        # to unknown regardless of whether it has a winner. Without that second
        # branch such a row renders as Unrecorded but belongs to no bucket, so
        # no chip ever selects it.
        return and_(untagged, not_dead,
                    or_(not_completed, ~has_result))
    return Match.end_reason == reason
