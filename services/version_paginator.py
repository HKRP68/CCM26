"""Pagination helpers for player versions.

When a search/buy/info command lands on a player who has multiple versions
(base + variants), we render a paginated carousel:

  Page 1/N  →  full card image of variant N
                ◀ Previous   1/N   Next ▶
                💰 Buy (price 🪙)        ❌ Close
                [direct page jumps if N >= 3]

The same component is used by /playerinfo, /buypl, and similar flows so
the UX is consistent.
"""

from html import escape as _html_escape
from typing import List, Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from models import Player, User, UserRoster
from config import get_buy_value


def get_versions_ordered(session, base_id: int) -> List[Player]:
    """Return [base, ...variants] in display order (base first, then variants
    by version label / id).
    """
    from services.version_service import get_all_versions
    versions = get_all_versions(session, base_id)
    if not versions:
        return []
    # Canonical base = first row with no parent; fall back to the first edition.
    # NOTE: keep EVERY other edition — including same-named rows that were never
    # linked (parent_player_id IS NULL) — otherwise the carousel would silently
    # drop them and never show ◀ Prev / Next ▶.
    base = next((v for v in versions if v.parent_player_id is None), versions[0])
    others = [v for v in versions if v.id != base.id]
    # Remaining editions ordered by version label (alpha) then id
    others.sort(key=lambda v: ((v.version or "").lower(), v.id))
    return [base] + others


def _format_version_label(player: Player) -> str:
    """Short label for a single page — used in indicators."""
    v = (player.version or "").strip()
    if not v or v.lower() in ("base", "base card"):
        return "Base"
    return v


def build_pagination_keyboard(
    *,
    session,
    user: User,
    versions: List[Player],
    current_index: int,
    owner_tg: int,
    flow: str,  # 'buy' or 'info' — chooses which buy/cancel callback prefixes
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the current version page.

    Layout (all rows optional based on count):
      Row 1: ◀ Prev   1/N   Next ▶          (only if N > 1)
      Row 2: 💰 Buy …                       (if not owned + flow=='buy')
             OR  ✅ You own this Version    (if owned)
             OR  🔄 Already own (other ver) (if owns sibling)
      Row 3: 1   2   3 ...                  (only if N >= 3 — direct page jumps)
      Row 4: ❌ Close
    """
    n = len(versions)
    rows = []
    current = versions[current_index]

    # ── Row 1: Prev / page indicator / Next
    if n > 1:
        prev_idx = (current_index - 1) % n
        next_idx = (current_index + 1) % n
        rows.append([
            InlineKeyboardButton("◀ Prev",
                                 callback_data=f"plpg_{flow}_{owner_tg}_{versions[prev_idx].id}"),
            InlineKeyboardButton(f"{current_index + 1}/{n}",
                                 callback_data=f"plpgnoop_{owner_tg}"),
            InlineKeyboardButton("Next ▶",
                                 callback_data=f"plpg_{flow}_{owner_tg}_{versions[next_idx].id}"),
        ])

    # ── Row 2: Buy / ownership state
    # Check if user owns this exact version, OR owns a sibling
    base_id = current.parent_player_id or current.id
    version_ids = [v.id for v in versions]
    owned_row = (session.query(UserRoster)
                 .filter(UserRoster.user_id == user.id,
                         UserRoster.player_id.in_(version_ids)).first())

    if flow == "buy" or flow == "info":
        if owned_row and owned_row.player_id == current.id:
            # Owns this exact version
            rows.append([
                InlineKeyboardButton("✅ Owned (this version)",
                                     callback_data=f"plpgnoop_{owner_tg}"),
            ])
        elif owned_row:
            # Owns a different version — block buy with informative button
            owned_v = next((v for v in versions if v.id == owned_row.player_id), None)
            owned_label = _format_version_label(owned_v) if owned_v else "another"
            rows.append([
                InlineKeyboardButton(
                    f"🔒 You own [{owned_label}]",
                    callback_data=f"plpgnoop_{owner_tg}",
                ),
            ])
        elif flow == "buy" and getattr(current, "restricted_from_buypl", False):
            # Player is blocked from /buypl direct purchase.
            # Other paths (market, packs, debut, trades) remain available.
            rows.append([
                InlineKeyboardButton(
                    "🚫 Not available to Buy",
                    callback_data=f"plpgnoop_{owner_tg}",
                ),
            ])
        else:
            # Not owned — show Buy button
            price = get_buy_value(current.rating)
            rows.append([
                InlineKeyboardButton(
                    f"💰 Buy ({price:,} 🪙)",
                    callback_data=f"buypl_{current.id}_{user.id}_{owner_tg}",
                ),
            ])

    # ── Row 3: Direct page jumps (only if 3+ versions)
    if n >= 3:
        jump_buttons = []
        for i, v in enumerate(versions):
            label = f"• {i+1} •" if i == current_index else str(i + 1)
            jump_buttons.append(InlineKeyboardButton(
                label,
                callback_data=f"plpg_{flow}_{owner_tg}_{v.id}",
            ))
        # Wrap into rows of 5 for mobile readability
        while jump_buttons:
            rows.append(jump_buttons[:5])
            jump_buttons = jump_buttons[5:]

    # ── Row N: Close
    rows.append([
        InlineKeyboardButton("❌ Close",
                             callback_data=f"buycancel_{owner_tg}"),
    ])

    return InlineKeyboardMarkup(rows)


def find_player_for_search(session, search_term: str) -> Optional[Player]:
    """Find the BASE player matching the search term, or any variant if no base.
    Returns the player object the bot should display first.
    """
    # Default to base card matches first
    base = (session.query(Player)
            .filter(Player.name.ilike(f"%{search_term}%"),
                    Player.parent_player_id.is_(None),
                    Player.is_active == True).first())
    if base:
        return base
    # Fall back to any variant
    return (session.query(Player)
            .filter(Player.name.ilike(f"%{search_term}%"),
                    Player.is_active == True).first())


def find_players_for_search(session, search_term: str,
                            limit: int = 12) -> List[Player]:
    """Return the DISTINCT base players matching the search term.

    Different *versions* of the same cricketer collapse into a single base
    entry, so this returns one row per real player. It's used to disambiguate
    broad searches like "Sachin" or "Kumar" that legitimately match several
    different players (Sachin Verma, Sachin Tendulkar, …).

    An exact (case-insensitive) full-name match short-circuits to just that
    player, so "/buypl Sachin Tendulkar" keeps working even when many other
    "Sachin" players exist.
    """
    term = (search_term or "").strip()
    if not term:
        return []

    # Exact full-name match wins outright (ilike with no % = case-insensitive
    # equality). Prefer the base card of that player.
    exact = (session.query(Player)
             .filter(Player.name.ilike(term),
                     Player.is_active == True)
             .order_by(Player.parent_player_id.isnot(None), Player.id)
             .first())
    if exact:
        base_id = exact.parent_player_id or exact.id
        base = session.query(Player).get(base_id)
        return [base or exact]

    rows = (session.query(Player)
            .filter(Player.name.ilike(f"%{term}%"),
                    Player.is_active == True)
            .order_by(Player.rating.desc(), Player.name)
            .all())

    seen = {}
    order: List[int] = []
    for p in rows:
        base_id = p.parent_player_id or p.id
        if base_id in seen:
            continue
        base = (p if p.parent_player_id is None
                else (session.query(Player).get(base_id) or p))
        seen[base_id] = base
        order.append(base_id)
        if len(order) >= limit:
            break
    return [seen[b] for b in order]


def format_multiple_players_message(command: str, search_term: str,
                                    players: List[Player]) -> str:
    """Build the "multiple players found — use the full name" prompt.

    Lists each matching player as a tappable ``/command Full Name`` code block
    so the user can re-run the command unambiguously.
    """
    term = _html_escape((search_term or "").strip())
    lines = [
        f"🔎 <b>Multiple players found</b> for “<i>{term}</i>”.",
        "Run the command again with the <b>full name</b>:",
        "",
    ]
    for p in players:
        name = _html_escape(p.name)
        country = _html_escape(p.country or "")
        suffix = f" — {p.rating} OVR" + (f" · {country}" if country else "")
        lines.append(f"• <code>/{command} {name}</code>{suffix}")
    return "\n".join(lines)


def page_number_for(versions: List[Player], player_id: int) -> int:
    """Return the index of player_id in versions, or 0 if not found."""
    for i, v in enumerate(versions):
        if v.id == player_id:
            return i
    return 0
