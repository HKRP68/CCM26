"""Who actually played today — the dashboard's match-day board.

The ops tiles answer "how many matches finished today", and the Top Users panel
ranks all-time wins. Neither answers the question an admin opens the dashboard
asking: *who is playing?* The manager who ground out twenty games this morning is
invisible on both, and so is the day a handful of people played everything.

This builds that board: one row per player, counted from **both** sides of every
match completed in the window, with the wins they took from those games.

Two deliberate choices:

* **The synthetic opponents are left out.** The /vsbot "Bot Opponent" is a real
  ``User`` row (telegram id ``-1``) that plays every bot match, so it would top
  this board every single day and inflate the head count with nobody.
* **The counting is done in Python.** A match contributes to two players, which
  in SQL needs a UNION of the two columns; a day's finished matches are a small
  set, and the caller already pays for far heavier queries on the same page.
"""

import logging

from models import Match, User

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 10
# How deep to resolve names before giving up. Only the synthetic opponents and
# deleted accounts are dropped after ranking, so a small margin over the limit
# is always enough to fill the board.
_LOOKUP_DEPTH = 40


def most_matches(session, start, end, limit=DEFAULT_LIMIT):
    """``{"total", "players", "leaders"}`` for matches completed in a window.

    ``start``/``end`` are UTC datetimes (``completed_at`` is stored in UTC), so a
    caller showing an IST calendar day converts its wall-clock bounds first.

    ``total`` is every completed match in the window, ``players`` the number of
    real people who appeared in one, and ``leaders`` the busiest of them —
    ``[{"user", "count", "won", "pct"}, …]``, ordered by matches played, with
    ``pct`` as a share of the busiest player's tally for a bar to read off.
    """
    sides = (session.query(Match.user1_id, Match.user2_id, Match.winner_id)
             .filter(Match.status == "completed",
                     Match.completed_at >= start,
                     Match.completed_at < end).all())

    played, won = {}, {}
    for user1_id, user2_id, winner_id in sides:
        for user_id in (user1_id, user2_id):
            if user_id is None:
                continue
            played[user_id] = played.get(user_id, 0) + 1
        if winner_id is not None:
            won[winner_id] = won.get(winner_id, 0) + 1

    synthetic = {row[0] for row in
                 session.query(User.id).filter(User.telegram_id <= 0).all()}

    leaders = []
    if played:
        # Ties break on user id, so the order is stable between refreshes.
        ranked = sorted(played.items(), key=lambda kv: (-kv[1], kv[0]))
        depth = max(int(limit) + len(synthetic), _LOOKUP_DEPTH)
        ranked = ranked[:depth]
        people = {u.id: u for u in session.query(User).filter(
            User.id.in_([user_id for user_id, _count in ranked])).all()}
        for user_id, count in ranked:
            person = people.get(user_id)
            if person is None or user_id in synthetic:
                continue    # a bot opponent, or an account that has since gone
            leaders.append({"user": person, "count": count,
                            "won": won.get(user_id, 0)})
            if len(leaders) >= int(limit):
                break

    top = max((row["count"] for row in leaders), default=0)
    for row in leaders:
        row["pct"] = round(row["count"] / top * 100) if top else 0

    return {
        "total": len(sides),
        "players": len([user_id for user_id in played if user_id not in synthetic]),
        "leaders": leaders,
    }
