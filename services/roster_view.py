"""One numbering for a roster, shared by every surface that shows or takes a position.

The problem this solves
───────────────────────
A card has two different "positions" and they are not the same number:

  • ``UserRoster.order_position`` — the *batting order*. Slots 1-11 are the
    Playing XI in the order they bat, 12+ are the bench. Gameplay reads it
    directly (``handlers.batting_order``, the match engines), so it is data,
    not presentation, and nothing in here may write to it.

  • the *display position* — what the user actually reads on screen. /pxi
    groups the XI by category (batsmen → keepers → all-rounders → pacers →
    spinners) rather than by batting slot, so the seventh line on /pxi is
    usually not batting slot seven.

Commands that take a position (``/release N``, ``/releasemultiple N M``) are
quoting the number they read on screen, so they have to resolve against the
display position. Every surface therefore goes through this module: one
ordering, one number per card, no drift between what is listed and what is
sold.

Display order is XI first (category-sorted, 1..11), then the bench in batting
order (12..N) — the same sequence /pxi prints, bench included.
"""

from services.bowling_service import is_spinner as _is_spin
from services.roster_service import get_roster_ordered

# How many cards make up the Playing XI. The display order splits here.
XI_SIZE = 11


def build_display_order(roster_list):
    """Re-sort ``(UserRoster, Player)`` pairs into display order.

    ``roster_list`` must already be in batting order — the first
    :data:`XI_SIZE` entries are treated as the XI and grouped by category;
    everything after that is bench and keeps its incoming order.
    """
    top_11 = roster_list[:XI_SIZE]
    bench = roster_list[XI_SIZE:]

    batsmen, keepers, allrounders, pacers, spinners = [], [], [], [], []
    for pair in top_11:
        _, player = pair
        cat = player.category
        if cat == "Batsman":
            batsmen.append(pair)
        elif cat == "Wicket Keeper":
            keepers.append(pair)
        elif cat == "All-rounder":
            allrounders.append(pair)
        elif cat == "Bowler":
            if _is_spin(player.bowl_style):
                spinners.append(pair)
            else:
                pacers.append(pair)
        else:
            batsmen.append(pair)

    return batsmen + keepers + allrounders + pacers + spinners + bench


def get_display_roster(session, user_id):
    """The whole roster in display order — the numbering the user types back.

    Read-only: it never renumbers ``order_position``, so looking at a roster
    can't reshuffle anybody's batting order.
    """
    return build_display_order(get_roster_ordered(session, user_id))


def is_in_xi(position):
    """Is this display position part of the Playing XI (rather than bench)?"""
    return position <= XI_SIZE


def zone_of(position):
    """``"XI"`` or ``"Bench"`` for a display position — for labelling lists."""
    return "XI" if is_in_xi(position) else "Bench"


def split_xi_bench(display_roster):
    """Split a display-ordered roster into ``(xi, bench)``."""
    return display_roster[:XI_SIZE], display_roster[XI_SIZE:]


def numbered(display_roster, start=1):
    """Yield ``(position, entry, player)`` so callers never re-derive the index."""
    for offset, (entry, player) in enumerate(display_roster):
        yield start + offset, entry, player


def find_position(display_roster, roster_entry_id):
    """The display position of a roster row, or ``None`` if it isn't listed."""
    for position, entry, _player in numbered(display_roster):
        if entry.id == roster_entry_id:
            return position
    return None
