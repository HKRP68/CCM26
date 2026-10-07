"""Build the whole auction pool as IPL-style sets in one go.

Building a pool set by set — ``/apool 85-90 | Marquee``, then the next band,
then the next — is the slow part of setting an auction up. This lays the pool
out the way the real IPL auction does instead:

* **Marquee** — the best ``marquee`` cards, whatever their role, first.
* **Role sets** — everybody else split by role, best first, in tiers of
  ``set_size``: "Batsmen 1", "Bowlers 1", "All-rounders 1",
  "Wicket-keepers 1", then "Batsmen 2" … so the room rotates through the roles
  and every franchise gets a look at each one before the second tier opens.

Writing goes through :func:`auction_sets_io.import_sets`, the same path a sets
file takes, so the rules are the import's: only queued players move, anybody
already sold, retained or drafted stays exactly where he is, and the queue is
renumbered in the order planned here.
"""

from models import AuctionLot
from services import auction_service as A
from services import auction_sets_io as SIO
from services import player_query
from services.auction_service import AuctionError

MARQUEE_SET = "Marquee"
DEFAULT_MARQUEE = 10
DEFAULT_SET_SIZE = 10
MAX_SET_SIZE = 50
MAX_MARQUEE = 50

# The order the role sets rotate in within each tier, and what each is called.
ROLE_ORDER = ("bat", "bowl", "ar", "wk")
ROLE_SET_NAMES = {"bat": "Batsmen", "bowl": "Bowlers", "ar": "All-rounders",
                  "wk": "Wicket-keepers"}


def role_key(category):
    """``"All Rounder"`` / ``"All-rounder"`` / ``"WK"`` … → one of ROLE_ORDER.

    The catalogue spells roles more than one way, and an unknown one is filed
    with the batsmen rather than dropped — a player missing from the pool is a
    worse surprise than one in a slightly odd set.
    """
    text = "".join(ch for ch in (category or "").lower() if ch.isalpha())
    if "keeper" in text or text in ("wk", "wkbat", "wkbatsman"):
        return "wk"
    if "allround" in text or text in ("ar", "rounder"):
        return "ar"
    if "bowl" in text:
        return "bowl"
    return "bat"


def _bounded(value, default, low, high, label):
    if value is None or value == "":
        return default
    number = A._as_int(value, None)
    if number is None or not low <= number <= high:
        raise AuctionError(f"{label} has to be a whole number from {low} to "
                           f"{high}.")
    return number


def plan_auto_sets(session, season, *, marquee=DEFAULT_MARQUEE,
                   set_size=DEFAULT_SET_SIZE, min_rating=None, pool_size=None,
                   editions=False, versions=None):
    """``[(set name, [Player])]`` in running order. Writes nothing.

    ``min_rating`` leaves out every card below it; ``pool_size`` keeps only the
    best N overall. Editions: base cards only by default, every edition with
    ``editions``, or exactly the card ``versions`` named ("Base", "Icon" …).
    Whenever more than base cards are allowed, only each cricketer's best card
    is kept, so the pool never holds the same man twice. Players this season has already finished with (sold,
    retained, drafted, passed on) are left out so they cannot take a Marquee
    place; players already queued are included and get re-filed.
    """
    marquee = _bounded(marquee, DEFAULT_MARQUEE, 0, MAX_MARQUEE, "Marquee size")
    set_size = _bounded(set_size, DEFAULT_SET_SIZE, 2, MAX_SET_SIZE, "Set size")
    min_rating = _bounded(min_rating, None, 1, 999, "Minimum rating")
    pool_size = _bounded(pool_size, None, 1, 100_000, "Pool size")

    if isinstance(versions, str):
        versions = versions.split(",")
    versions = [str(v).strip() for v in versions or [] if str(v).strip()]

    filters = {}
    if min_rating is not None:
        filters["rating_min"] = min_rating
    if versions:
        filters["versions"] = versions
    elif not editions:
        filters["version_mode"] = "base"
    players = player_query.ordered(
        player_query.master_player_query(session, filters)).all()
    if versions or editions:
        # Best first already, so the first card seen is the one kept.
        seen, best = set(), []
        for player in players:
            root = player.parent_player_id or player.id
            if root not in seen:
                seen.add(root)
                best.append(player)
        players = best

    finished = {row[0] for row in session.query(AuctionLot.player_id)
                .filter(AuctionLot.season_id == season.id,
                        AuctionLot.player_id.isnot(None),
                        AuctionLot.status != A.LOT_QUEUED).all()}
    players = [p for p in players if p.id not in finished]
    if pool_size is not None:
        players = players[:pool_size]
    if not players:
        raise AuctionError(
            "No card matches"
            + (f" the version(s) {', '.join(versions)}" if versions else "")
            + " — nobody to put in the pool.")

    plan = []
    if marquee:
        plan.append((MARQUEE_SET, players[:marquee]))
    by_role = {key: [] for key in ROLE_ORDER}
    for player in players[marquee:]:
        by_role[role_key(player.category)].append(player)

    chunks = {key: [rows[i:i + set_size]
                    for i in range(0, len(rows), set_size)]
              for key, rows in by_role.items()}
    tiers = max((len(c) for c in chunks.values()), default=0)
    for tier in range(tiers):
        for key in ROLE_ORDER:
            if tier < len(chunks[key]):
                plan.append((f"{ROLE_SET_NAMES[key]} {tier + 1}",
                             chunks[key][tier]))
    return [(name, rows) for name, rows in plan if rows]


def auto_build_sets(session, season, *, replace=False, **options):
    """Plan the sets and load them into the pool. Returns the import's result
    dict plus ``sets`` — the planned set names in running order."""
    plan = plan_auto_sets(session, season, **options)
    groups = [(name, [{"player_id": p.id, "name": p.name} for p in rows])
              for name, rows in plan]
    result = SIO.import_sets(session, season, groups, replace=replace)
    result["sets"] = [name for name, _ in plan]
    return result


def summary_text(result):
    names = result.get("sets") or []
    shown = " → ".join(names[:8]) + (" …" if len(names) > 8 else "")
    return (f"{len(names)} sets: {shown}\n" + SIO.summary_text(result))
