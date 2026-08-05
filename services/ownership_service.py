"""Who owns a player — per group and globally (the /owners command).

Trading is the reason this exists: before you offer a deal you want to know who
in *this* group is holding the card, which edition they hold, and how rare it
is everywhere else. The queries live here (and not in the handler) so they can
be tested against a real database without Telegram in the way.

Group membership comes from ``chat_members``, which the bot learns from
activity — see ``services.chat_tracker.record_chat_member``. It is a
best-effort view: a member who has never spoken since the bot joined is simply
unknown, which is why every group figure is reported as "of N known members".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Sequence

from models import ChatMember, Player, User, UserRoster

logger = logging.getLogger(__name__)


def group_owner_rows(session, chat_id: int, player_ids: Sequence[int]):
    """(User, UserRoster, Player) for the members of one group who own the card.

    Joins ``chat_members`` rather than passing a list of member ids into an
    ``IN`` clause, so a 5,000-strong group is still one ordinary query.
    Ordered by card rating then longest-held, so the best (and most settled)
    holdings — the ones worth asking about — lead the list.
    """
    if not player_ids:
        return []
    return (session.query(User, UserRoster, Player)
            .join(UserRoster, UserRoster.user_id == User.id)
            .join(Player, UserRoster.player_id == Player.id)
            .join(ChatMember, ChatMember.user_id == User.id)
            .filter(ChatMember.chat_id == chat_id,
                    ChatMember.is_active == True,  # noqa: E712
                    UserRoster.player_id.in_(list(player_ids)))
            .order_by(Player.rating.desc(), UserRoster.acquired_date.asc())
            .all())


def group_owners(session, chat_id: int, player_ids: Sequence[int]) -> List[Dict]:
    """One entry per *manager* in the group who owns the player.

    ``group_owner_rows`` returns a roster row, and a manager holding both Base
    and Gold has two of those — listing them twice would name the same person
    twice and count them twice ("2 of 1 known member"). Collapsing here keeps
    the identity right while preserving every edition they hold, which is
    exactly what a trader wants to see against the name.

    Each entry: ``{"user", "cards": [Player, …], "acquired": datetime|None}``,
    where ``acquired`` is the earliest of that manager's holdings.
    """
    collapsed: Dict[int, Dict] = {}
    for user, entry, card in group_owner_rows(session, chat_id, player_ids):
        holding = collapsed.get(user.id)
        if holding is None:
            collapsed[user.id] = {"user": user, "cards": [card],
                                  "acquired": entry.acquired_date}
            continue
        holding["cards"].append(card)
        if entry.acquired_date and (holding["acquired"] is None
                                    or entry.acquired_date < holding["acquired"]):
            holding["acquired"] = entry.acquired_date

    owners = list(collapsed.values())
    for holding in owners:
        holding["cards"].sort(key=lambda c: (-(c.rating or 0), c.id))
    # Best card first, then the longest-standing holding.
    owners.sort(key=lambda h: (-(h["cards"][0].rating or 0),
                               h["acquired"] or datetime.max))
    return owners


def group_member_count(session, chat_id: int) -> int:
    """How many managers the bot has actually seen in this group."""
    return (session.query(ChatMember.id)
            .filter(ChatMember.chat_id == chat_id,
                    ChatMember.is_active == True)  # noqa: E712
            .count())


def global_owner_count(session, player_ids: Sequence[int]) -> int:
    """How many managers hold any edition of this player, anywhere."""
    if not player_ids:
        return 0
    return (session.query(UserRoster.user_id)
            .filter(UserRoster.player_id.in_(list(player_ids)))
            .distinct().count())


def owners_per_version(session, player_ids: Sequence[int]) -> Dict[int, int]:
    """{player_id: owner count} for each edition, globally."""
    if not player_ids:
        return {}
    from sqlalchemy import func
    rows = (session.query(UserRoster.player_id,
                          func.count(func.distinct(UserRoster.user_id)))
            .filter(UserRoster.player_id.in_(list(player_ids)))
            .group_by(UserRoster.player_id)
            .all())
    return {pid: count for pid, count in rows}


def group_ids_for_user(session, user_id: int) -> List[int]:
    """Groups this manager is known to be in — used by the /owners DM view."""
    rows = (session.query(ChatMember.chat_id)
            .filter(ChatMember.user_id == user_id,
                    ChatMember.is_active == True)  # noqa: E712
            .order_by(ChatMember.last_seen_at.desc())
            .all())
    return [r[0] for r in rows]


def owner_counts_by_group(session, player_ids: Sequence[int],
                          chat_ids: Sequence[int]) -> Dict[int, int]:
    """{chat_id: how many of that group's known members own the player}."""
    if not player_ids or not chat_ids:
        return {}
    from sqlalchemy import func
    rows = (session.query(ChatMember.chat_id,
                          func.count(func.distinct(ChatMember.user_id)))
            .join(UserRoster, UserRoster.user_id == ChatMember.user_id)
            .filter(ChatMember.chat_id.in_(list(chat_ids)),
                    ChatMember.is_active == True,  # noqa: E712
                    UserRoster.player_id.in_(list(player_ids)))
            .group_by(ChatMember.chat_id)
            .all())
    return {chat_id: count for chat_id, count in rows}


def group_titles(session, chat_ids: Sequence[int]) -> Dict[int, str]:
    """{chat_id: group title} for the ids we have a bot_chats row for."""
    if not chat_ids:
        return {}
    from models import BotChat
    rows = (session.query(BotChat.chat_id, BotChat.title)
            .filter(BotChat.chat_id.in_(list(chat_ids)))
            .all())
    return {chat_id: (title or "") for chat_id, title in rows}


def total_managers(session) -> int:
    """Every debuted manager — the denominator for "how rare is this card"."""
    return session.query(User.id).count()


def scarcity_line(owners: int, managers: int) -> str:
    """"1 in every 7 managers" — the number a trader actually haggles with."""
    if owners <= 0 or managers <= 0:
        return "nobody owns this card yet"
    if owners >= managers:
        return "every manager owns one"
    share = owners / managers
    return f"1 in every {max(2, round(1 / share)):,} managers"
