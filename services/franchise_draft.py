"""Fixed-price franchise drafts with rotating rounds and durable confirmations.

The auction catalogue, squads, purses and ledger are shared. This module owns
the draft lifecycle and serializes every pick through a revision on its config
row. Callers own transactions; no Telegram request is made before commit.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
import json
import logging
import math
import random
import re
import secrets
from types import SimpleNamespace

from models import (AuctionBid, AuctionFranchise, AuctionLot, AuctionSeason,
                    FranchiseDraft, FranchiseDraftConfirmation, FranchiseDraftPick,
                    FranchiseDraftTurn, FranchiseDraftAdmin)
from sqlalchemy import func
from services import auction_service as A

AuctionError = A.AuctionError
logger = logging.getLogger(__name__)
MAX_ROUNDS = 100
QUOTE_LIFETIME = timedelta(minutes=10)


def _e(value):
    return escape(str(value if value is not None else ""))


def _loads(value, default):
    return json.loads(value) if value else default


def _dump(value):
    return json.dumps(value, separators=(",", ":"))


def _money(season, price):
    return A.render_money(int(price), season.currency_label or "₹")


def get_draft(session, season):
    return session.get(FranchiseDraft, season.id if hasattr(season, "id") else int(season))


def _draft(session, season):
    row = get_draft(session, season)
    if row is None:
        raise AuctionError("This season uses an auction. Choose Franchise Draft first.")
    return row


def _claim(session, season, draft=None):
    """One conditional write locks both simultaneous picks and setup edits."""
    draft = draft or _draft(session, season)
    version = int(draft.revision)
    session.flush()
    changed = (session.query(FranchiseDraft)
               .filter(FranchiseDraft.season_id == season.id,
                       FranchiseDraft.revision == version)
               .update({FranchiseDraft.revision: FranchiseDraft.revision + 1},
                       synchronize_session=False))
    if changed != 1:
        raise AuctionError("The draft changed. Open its latest board and try again.")
    session.expire(draft)
    session.refresh(draft)
    # Another operation can have changed the season while this transaction
    # waited on the conditional update's row lock.
    session.refresh(season)
    return draft


def _editable(session, season):
    draft = _draft(session, season)
    if season.league_id:
        raise AuctionError("Published draft rules are locked.")
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise AuctionError("Pause the draft before changing its rules or prices.")
    return draft


def _rounds(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise AuctionError("Rounds must be a whole number.")
    number = int(value)
    if not 1 <= number <= MAX_ROUNDS:
        raise AuctionError(f"Choose between 1 and {MAX_ROUNDS} rounds.")
    return number


def _manager(session, actor):
    # None is reserved for trusted internal callers (season setup, migrations).
    if actor is not None:
        require_draft_admin(session, actor)


def is_draft_admin(session, actor):
    from services.admin_ids import is_bot_admin
    if isinstance(actor, bool) or not str(actor).isascii() or not str(actor).isdigit() or int(actor) <= 0:
        return False
    return is_bot_admin(actor) or session.query(FranchiseDraftAdmin.id).filter_by(tg_id=int(actor)).first() is not None


def require_draft_admin(session, actor):
    if not is_draft_admin(session, actor):
        raise AuctionError("Only Draft Admins can manage a franchise draft.")


def draft_admins(session):
    return session.query(FranchiseDraftAdmin).order_by(FranchiseDraftAdmin.id).all()


def add_draft_admin(session, actor, *, name=None, by_tg_id=None):
    from services.admin_ids import is_bot_admin
    if by_tg_id is not None and not is_bot_admin(by_tg_id):
        raise AuctionError("Only Bot Owners and Bot Admins can appoint Draft Admins.")
    if isinstance(actor, bool) or not str(actor).isdigit() or not 0 < int(actor) < 10**16:
        raise AuctionError("A Draft Admin needs a positive Telegram user ID.")
    if session.query(FranchiseDraftAdmin.id).filter_by(tg_id=int(actor)).first():
        raise AuctionError("That person is already a Draft Admin.")
    row = FranchiseDraftAdmin(tg_id=int(actor), name=str(name).strip()[:120] if name else None,
                             added_by_tg_id=by_tg_id)
    session.add(row)
    session.flush()
    return row


def remove_draft_admin(session, actor, *, by_tg_id=None):
    from services.admin_ids import is_bot_admin
    if by_tg_id is not None and not is_bot_admin(by_tg_id):
        raise AuctionError("Only Bot Owners and Bot Admins can remove Draft Admins.")
    if isinstance(actor, bool) or not str(actor).isdigit() or not 0 < int(actor) < 10**16:
        raise AuctionError("Give a positive Telegram user ID.")
    row = session.query(FranchiseDraftAdmin).filter_by(tg_id=int(actor)).first()
    if row is None:
        raise AuctionError("That person is not an appointed Draft Admin.")
    session.delete(row)
    session.flush()
    return row


def update_team_roles(session, season, team, *, owner_tg_id=None, co_owners=None, by_tg_id=None):
    _manager(session, by_tg_id)
    draft = _editable(session, season)
    draft = _claim(session, season, draft)
    _editable(session, season)
    if team.season_id != season.id:
        raise AuctionError("That team belongs to another draft.")
    if owner_tg_id is not None:
        if isinstance(owner_tg_id, bool) or not str(owner_tg_id).isdigit() or not 0 < int(owner_tg_id) < 10**16:
            raise AuctionError("Give a positive owner Telegram ID.")
        if team.owner_tg_id != int(owner_tg_id):
            team.owner_name = None
        team.owner_tg_id = int(owner_tg_id)
    if co_owners is not None:
        if len(co_owners) > 30 or any(isinstance(i, bool) or not str(i).isdigit() or not 0 < int(i) < 10**16 for i in co_owners):
            raise AuctionError("Give up to 30 positive co-owner Telegram IDs, or clear.")
        A.set_co_owners(session, team, [int(i) for i in co_owners])
    _event(session, season, 'fd_roles', f'🏷 <b>{_e(team.name)} ownership updated</b>\nExisting pick confirmations have expired.',
           by_tg_id=by_tg_id, franchise=team)
    return team


def update_squad_rules(session, season, *, minimums=None, maximums=None, ratings=None, by_tg_id=None):
    _manager(session, by_tg_id)
    draft = _editable(session, season)
    _claim(session, season, draft)
    _editable(session, season)
    A.set_role_rules(session, season,
        A.normalize_draft_roles(minimums) if minimums is not None else A.role_minimums(season),
        A.normalize_draft_roles(maximums) if maximums is not None else A.role_maximums(season))
    if ratings is not None:
        A.set_rating_rules(session, season, ratings)
    _event(session, season, 'fd_rules', '📜 <b>Draft squad rules updated</b>\n'
           + _e(A.role_rule_line(season)) + '\n' + _e(A.rating_rule_line(season)), by_tg_id=by_tg_id)


def _invariants(season, draft):
    season.min_squad_size = season.max_squad_size = int(draft.rounds)
    season.min_overseas = 0
    season.max_overseas = int(draft.rounds)
    season.max_retentions = season.min_retentions = season.expansion_picks = 0
    season.rtm_enabled = False
    season.rtm_per_team = 0
    season.draft_pick_seconds = 0
    season.current_lot_id = None


def enable(session, season, rounds=15, *, by_tg_id=None):
    _manager(session, by_tg_id)
    count = _rounds(rounds)
    if get_draft(session, season) is not None:
        raise AuctionError("This season already uses Franchise Draft.")
    if season.status != A.STATUS_SETUP or season.league_id:
        raise AuctionError("Choose draft mode during setup, before any players are signed.")
    if (session.query(AuctionLot.id).filter(AuctionLot.season_id == season.id,
            AuctionLot.status != A.LOT_QUEUED).first()
            or session.query(AuctionBid.id).filter(AuctionBid.season_id == season.id).first()
            or any(int(f.squad_size or 0) for f in A.franchises(session, season.id))):
        raise AuctionError("Choose draft mode before any players are signed or bid on.")
    row = FranchiseDraft(season_id=season.id, rounds=count, cursor=0, revision=0)
    session.add(row)
    _invariants(season, row)
    # A setup may have been prepared as a dynamic-retention auction before
    # the organiser chose Draft. None of that unfinished auction state is a
    # draft rule; leave only its pool, purse, franchises and squad rulebook.
    for field in ("retention_mode", "retention_ranges_json", "retention_range_max_total",
                  "draft_ranges_json", "draft_order_json", "retention_slots_json",
                  "retention_rules_json", "retention_max_spend_lakh", "retention_deadline_at",
                  "retention_min_rating", "retention_max_rating", "retention_categories_json",
                  "retention_price_rules_json", "retention_locked_at", "draft_pick_deadline_at",
                  "draft_clock_turn"):
        setattr(season, field, None)
    season.draft_clock_paused = 0
    # Previous seasons may have been configured using the native BAT/WK
    # abbreviations. Store canonical roles so catalogue players satisfy them.
    lows = A.normalize_draft_roles(_loads(season.role_minimums_json, {}))
    highs = A.normalize_draft_roles(_loads(season.role_maximums_json, {}))
    A.set_role_rules(session, season, lows, highs)
    A.set_rating_rules(session, season, A.rating_rules(season))
    for team in A.franchises(session, season.id):
        team.draft_picks_total = count
        team.draft_picks_used = 0
        team.rtm_cards_total = team.rtm_cards_used = 0
        team.retention_finalized_at = None
        team.draft_shortlist_json = None
    session.flush()
    _event(session, season, "fd_enabled", "🏏 <b>Franchise Draft selected</b>\n"
           f"{count} rounds · {count} players per team · no overseas limit.", by_tg_id=by_tg_id)
    return row


def configure_rounds(session, season, rounds, *, by_tg_id=None):
    _manager(session, by_tg_id)
    count = _rounds(rounds)
    draft = _draft(session, season)
    if season.league_id:
        raise AuctionError("Add rounds before publishing the squads.")
    if season.status == A.STATUS_COMPLETED:
        if count <= int(draft.rounds):
            raise AuctionError("Add more rounds to reopen a completed draft.")
    else:
        _editable(session, season)
    teams = A.franchises(session, season.id)
    if any(int(f.squad_size or 0) > count for f in teams):
        raise AuctionError("The round count cannot be smaller than an existing squad.")
    if int(draft.cursor) and count < int(draft.rounds):
        raise AuctionError("After the first pick, rounds may only be added.")
    draft = _claim(session, season, draft)
    if season.league_id or season.published_at:
        raise AuctionError("Add rounds before publishing the squads.")
    was_completed = season.status == A.STATUS_COMPLETED
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED, A.STATUS_COMPLETED):
        raise AuctionError("Pause the draft before changing its rounds.")
    draft.rounds = count
    _invariants(season, draft)
    for team in teams:
        team.draft_picks_total = count
    if was_completed:
        season.status = A.STATUS_PAUSED
    session.flush()
    A.set_role_rules(session, season, A.role_minimums(season), A.role_maximums(season))
    A.set_rating_rules(session, season, A.rating_rules(season))
    _event(session, season, "fd_rounds", f"🔢 <b>Draft rounds updated: {count}</b>\n"
           f"Squad maximum: {count}." + (" Resume the draft to continue picking." if was_completed else ""),
           by_tg_id=by_tg_id)
    return draft


def _ordered_teams(session, season):
    draft = _draft(session, season)
    teams = A.franchises(session, season.id)
    stored = _loads(draft.order_json, [])
    ids = {f.id for f in teams}
    if stored and set(stored) == ids and len(stored) == len(ids):
        by_id = {f.id: f for f in teams}
        return [by_id[team_id] for team_id in stored]
    if draft.cursor:
        raise AuctionError("The draft teams changed. Restore the original franchises before continuing.")
    return teams


def set_order(session, season, team_ids=None, *, randomize=False, by_tg_id=None):
    _manager(session, by_tg_id)
    draft = _editable(session, season)
    if draft.cursor:
        raise AuctionError("Set the draft order before the first pick.")
    teams = A.franchises(session, season.id)
    ids = [f.id for f in teams]
    if team_ids is not None:
        try:
            selected = [int(i) for i in team_ids]
        except (ValueError, TypeError):
            raise AuctionError("Include every franchise ID exactly once.") from None
        if len(selected) != len(set(selected)) or set(selected) != set(ids):
            raise AuctionError("Include every franchise ID exactly once.")
        ids = selected
    if randomize:
        random.SystemRandom().shuffle(ids)
    draft = _claim(session, season, draft)
    _editable(session, season)
    if draft.cursor:
        raise AuctionError("Set the draft order before the first pick.")
    draft.order_json = _dump(ids)
    session.flush()
    _event(session, season, "fd_order", "📋 <b>Draft order saved</b>\n" + render_order(session, season),
           by_tg_id=by_tg_id)
    return _ordered_teams(session, season)


def parse_money(text):
    """Parse explicit crore/lakh values without floating point rounding."""
    match = re.fullmatch(r"\s*(?:₹\s*)?(\d+(?:\.\d+)?)\s*(cr|crore|crores|l|lac|lacs|lakh|lakhs)\s*",
                         str(text), re.IGNORECASE)
    if not match:
        raise AuctionError("Use a nonnegative amount with cr or lakh, for example 23cr or 50L.")
    try:
        amount = Decimal(match.group(1)) * (100 if match.group(2).lower() in ("cr", "crore", "crores") else 1)
    except InvalidOperation:
        raise AuctionError("Invalid price.") from None
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 <= amount <= 1_000_000_000:
        raise AuctionError("A price must resolve to whole lakh between 0 and 1,000,000,000.")
    return int(amount)


def _price_rows(text):
    rows = [line.strip() for line in re.split(r"[\n;]+", str(text or "")) if line.strip()]
    if not rows or len(rows) > 2000:
        raise AuctionError("Enter between 1 and 2,000 price lines, for example 96:23cr.")
    result = []
    for line in rows:
        if ":" not in line:
            raise AuctionError(f"Invalid price line: {line[:100]}. Use name:25.5cr or 96:23cr.")
        key, value = line.rsplit(":", 1)
        if not key.strip():
            raise AuctionError("Each price needs a rating or player name.")
        result.append((key.strip(), parse_money(value)))
    return result


def set_rating_prices(session, season, text, *, by_tg_id=None):
    _manager(session, by_tg_id)
    draft = _editable(session, season)
    prices = {}
    for raw, price in _price_rows(text):
        if not re.fullmatch(r"\d{1,3}", raw) or not 1 <= int(raw) <= 100:
            raise AuctionError("Ratings must be whole numbers between 1 and 100, for example 96:23cr.")
        key = str(int(raw))
        if key in prices:
            raise AuctionError(f"Rating {key} appears more than once.")
        prices[key] = price
    draft = _claim(session, season, draft)
    _editable(session, season)
    draft.rating_prices_json = _dump(prices)
    session.flush()
    _event(session, season, "fd_prices", f"💰 <b>Rating prices saved</b>\n{len(prices)} ratings priced. "
           "Custom player prices take priority. Existing signings keep their price.", by_tg_id=by_tg_id)
    return prices


def find_player(session, season, query, *, available_only=True):
    raw = str(query or "").strip()
    if not raw:
        raise AuctionError("Choose a player: /dpick Joe Root. Use a pool #ID for duplicate names.")
    rows = A.lots(session, season.id)
    if raw.startswith("#") and raw[1:].isdigit():
        found = [lot for lot in rows if lot.id == int(raw[1:])]
    else:
        found = [lot for lot in rows if lot.name.casefold() == raw.casefold()]
        if not found:
            found = [lot for lot in rows if lot.name.casefold().startswith(raw.casefold())]
    if not found:
        raise AuctionError(f"{raw[:100]} is not in this draft's player pool.")
    if len(found) > 1:
        choices = ", ".join(f"#{lot.id} {lot.name} ({lot.rating})" for lot in found[:5])
        raise AuctionError("That name matches several player cards. Use its #ID: " + choices)
    lot = found[0]
    if available_only and lot.status != A.LOT_QUEUED:
        raise AuctionError(f"{lot.name} is already signed or unavailable.")
    return lot


def set_custom_prices(session, season, text, *, by_tg_id=None):
    _manager(session, by_tg_id)
    draft = _editable(session, season)
    prices = dict(_loads(draft.custom_prices_json, {}))
    seen = set()
    for name, price in _price_rows(text):
        lot = find_player(session, season, name)
        key = str(lot.id)
        if key in seen:
            raise AuctionError(f"{lot.name} appears more than once.")
        seen.add(key)
        prices[key] = price
    draft = _claim(session, season, draft)
    _editable(session, season)
    draft.custom_prices_json = _dump(prices)
    session.flush()
    _event(session, season, "fd_prices", f"💎 <b>Custom player prices saved</b>\n{len(seen)} player prices updated.",
           by_tg_id=by_tg_id)
    return prices


def price_for(session, season, lot):
    draft = _draft(session, season)
    custom = _loads(draft.custom_prices_json, {})
    if str(lot.id) in custom:
        return int(custom[str(lot.id)])
    ratings = _loads(draft.rating_prices_json, {})
    if str(lot.rating) not in ratings:
        raise AuctionError(f"No draft price is configured for rating {lot.rating} ({lot.name}). "
                           "Add it with /dprices or set /dcustom.")
    return int(ratings[str(lot.rating)])


def round_order(session, season, round_no):
    teams = _ordered_teams(session, season)
    if not teams:
        return []
    shift = (int(round_no) - 1) % len(teams)
    return teams[shift:] + teams[:shift]


def schedule(session, season):
    return [team for number in range(1, _draft(session, season).rounds + 1)
            for team in round_order(session, season, number)]


def skipped_turns(session, season):
    return _loads(_draft(session, season).skipped_turns_json, [])


def _regular_total(session, season):
    return int(_draft(session, season).rounds) * len(_ordered_teams(session, season))


def turn_info(session, season):
    draft = _draft(session, season)
    teams = _ordered_teams(session, season)
    if teams and int(draft.cursor) < int(draft.rounds) * len(teams):
        round_no = int(draft.cursor) // len(teams) + 1
        return dict(team=round_order(session, season, round_no)[int(draft.cursor) % len(teams)],
                    round_no=round_no, catch_up=False)
    queue = skipped_turns(session, season)
    if queue:
        team = next((t for t in teams if t.id == queue[0]['franchise_id']), None)
        if team is None:
            raise AuctionError("A skipped team's franchise is missing. Restore it before continuing.")
        return dict(team=team, round_no=queue[0]['round_no'], catch_up=True)
    return dict(team=None, round_no=None, catch_up=False)


def turn(session, season):
    return turn_info(session, season)['team']


def _turn_state(draft):
    return dict(cursor=int(draft.cursor), skipped=_loads(draft.skipped_turns_json, []))


def _next_number(session, model, column, season):
    return int(session.query(func.max(column)).filter(model.season_id == season.id).scalar() or 0) + 1


def _record_turn(session, season, team, kind, before, *, pick_no=None, actor=None, now=None):
    row = FranchiseDraftTurn(season_id=season.id,
        number=_next_number(session, FranchiseDraftTurn, FranchiseDraftTurn.number, season),
        kind=kind, franchise_id=team.id, pick_no=pick_no,
        before_state_json=_dump(before), by_tg_id=actor, created_at=now or datetime.utcnow())
    session.add(row)
    return row


def _start_clock(session, season, *, now=None, seconds=None):
    draft = _draft(session, season)
    now = now or datetime.utcnow()
    if season.status == A.STATUS_LIVE and int(draft.pick_minutes or 0) and turn(session, season):
        duration = int(draft.pick_minutes) * 60 if seconds is None else max(0, int(seconds))
        draft.pick_deadline_at = now + timedelta(seconds=duration)
    else:
        draft.pick_deadline_at = None
    draft.paused_seconds_left = None


def _check_time(draft, now=None):
    if draft.pick_deadline_at is not None and (now or datetime.utcnow()) >= draft.pick_deadline_at:
        raise AuctionError("This team's pick time has expired. The bot will skip it and tag the next team.")


def clock_text(session, season):
    draft = _draft(session, season)
    if not int(draft.pick_minutes or 0):
        return "⏱ Pick timer: off"
    text = f"⏱ <b>{draft.pick_minutes} minutes</b> per turn"
    if draft.pick_deadline_at is not None:
        text += f" · deadline <b>{draft.pick_deadline_at:%d %b %H:%M:%S} UTC</b>"
    elif season.status == A.STATUS_PAUSED and draft.paused_seconds_left is not None:
        text += f" · paused with {draft.paused_seconds_left // 60}m {draft.paused_seconds_left % 60}s left"
    return text


def set_timer(session, season, minutes, *, by_tg_id=None):
    _manager(session, by_tg_id)
    if isinstance(minutes, bool) or not re.fullmatch(r"\d+", str(minutes).strip()) or not 0 <= int(minutes) <= 1440:
        raise AuctionError("Pick time must be whole minutes from 1 to 1440; use 0 to turn the timer off.")
    draft = _editable(session, season)
    draft = _claim(session, season, draft)
    _editable(session, season)
    draft.pick_minutes = int(minutes)
    draft.pick_deadline_at = None
    draft.paused_seconds_left = int(minutes) * 60 if season.status == A.STATUS_PAUSED and int(minutes) else None
    _event(session, season, "fd_timer", "⏱ <b>Draft pick timer updated</b>\n" + clock_text(session, season),
           by_tg_id=by_tg_id)
    return draft


def add_team(session, season, name, owner_tg_id, *, co_owners=None, by_tg_id=None):
    """An expansion franchise joins the full draft during team setup."""
    _manager(session, by_tg_id)
    draft = _draft(session, season)
    if season.status != A.STATUS_SETUP or draft.cursor or season.league_id:
        raise AuctionError("Add new draft teams during setup, before the draft starts.")
    identities = [owner_tg_id] + list(co_owners or [])
    if len(identities) > 31:
        raise AuctionError("A team can have up to 30 co-owners.")
    if any(isinstance(i, bool) or not str(i).isdigit() or not 0 < int(i) < 10**16 for i in identities):
        raise AuctionError("Give positive Telegram user IDs for the owner and co-owners.")
    draft = _claim(session, season, draft)
    if season.status != A.STATUS_SETUP or draft.cursor or season.league_id:
        raise AuctionError("Add new draft teams during setup, before the draft starts.")
    team = A.create_franchise(session, season, name, owner_tg_id=int(owner_tg_id),
                              draft_picks_total=draft.rounds)
    A.set_co_owners(session, team, [int(i) for i in co_owners or []])
    if draft.order_json:
        draft.order_json = _dump(_loads(draft.order_json, []) + [team.id])
    _event(session, season, "fd_team", f"🏷 <b>{_e(team.name)} join the draft</b>\n"
           f"{draft.rounds} picks · purse {_money(season, team.purse_remaining_lakh)}. Review /dorder before starting.",
           by_tg_id=by_tg_id, franchise=team)
    return team


def _advance_turn(session, season, info, *, skipped=False, now=None):
    draft = _draft(session, season)
    queue = skipped_turns(session, season)
    if info['catch_up']:
        entry = queue.pop(0)
        if skipped:
            entry['retries'] = int(entry.get('retries', 0)) + 1
            queue.append(entry)
    else:
        draft.cursor = int(draft.cursor) + 1
        if skipped:
            queue.append(dict(franchise_id=info['team'].id, round_no=info['round_no'], retries=0))
    draft.skipped_turns_json = _dump(queue)
    if turn(session, season) is None:
        season.status = A.STATUS_COMPLETED
    _start_clock(session, season, now=now)


def _skip_locked(session, season, *, actor=None, timeout=False, now=None):
    draft = _draft(session, season)
    info = turn_info(session, season)
    team = info['team']
    if team is None:
        raise AuctionError("Every draft turn is complete.")
    _record_turn(session, season, team, "timeout" if timeout else "skip", _turn_state(draft), actor=actor, now=now)
    _advance_turn(session, season, info, skipped=True, now=now)
    _event(session, season, "fd_skipped", f"⏭ <b>{_e(team.name)} {'ran out of pick time' if timeout else 'skip their turn'}</b>\n"
           f"Round {info['round_no']} pick moved to the back of the skipped-pick queue. No purse deducted.\n\n"
           + next_team_html(session, season), by_tg_id=actor, franchise=team,
           metadata={'timeout': timeout, 'round_no': info['round_no'], 'catch_up': info['catch_up']})
    return team


def skip_turn(session, season, actor_tg_id, *, expected_revision=None, now=None):
    draft = _draft(session, season)
    if season.status != A.STATUS_LIVE:
        raise AuctionError("Start or resume the draft before skipping a turn.")
    if expected_revision is not None and int(expected_revision) != int(draft.revision):
        raise AuctionError("This skip button is stale. Open /draft for the current turn.")
    draft = _claim(session, season, draft)
    if season.status != A.STATUS_LIVE:
        raise AuctionError("The draft is no longer live.")
    team = turn(session, season)
    if team is not None:
        session.refresh(team)
    _require_owner(team, actor_tg_id)
    return _skip_locked(session, season, actor=actor_tg_id, now=now)


def resolve_timeout(session, season, *, now=None):
    """Skip one expired turn; the next team receives a fresh full window."""
    now = now or datetime.utcnow()
    draft = _draft(session, season)
    if (season.status == A.STATUS_LIVE and draft.pick_deadline_at is None
            and int(draft.pick_minutes or 0) and turn(session, season) is not None):
        # A pre-clock draft resumed after an upgrade gets a full first window.
        draft = _claim(session, season, draft)
        if season.status == A.STATUS_LIVE and draft.pick_deadline_at is None:
            _start_clock(session, season, now=now)
            _event(session, season, "fd_timer_started", "⏱ <b>Draft pick timer started</b>\n"
                   + next_team_html(session, season))
        return None
    if season.status != A.STATUS_LIVE or draft.pick_deadline_at is None or draft.pick_deadline_at > now:
        return None
    draft = _claim(session, season, draft)
    if season.status != A.STATUS_LIVE or draft.pick_deadline_at is None or draft.pick_deadline_at > now:
        return None
    return _skip_locked(session, season, timeout=True, now=now)


def owner_ids(team):
    ids = [int(team.owner_tg_id)] if team.owner_tg_id else []
    return list(dict.fromkeys(ids + list(A.co_owner_ids(team))))


def _require_owner(team, actor):
    if team is None or actor is None or int(actor) not in owner_ids(team):
        raise AuctionError("Only the current team's owner or co-owner can pick for it.")


def next_team_html(session, season):
    team = turn(session, season)
    if team is None:
        return "🏁 <b>All draft picks are complete.</b> Review squads, then publish or add more rounds."
    identities = owner_ids(team)
    tags = " ".join(f'<a href="tg://user?id={identity}">{_e((team.owner_name or "Owner")[:60]) if index == 0 and team.owner_tg_id == identity else "Co-owner"}</a>'
                    for index, identity in enumerate(identities[:12]))
    if len(identities) > 12:
        tags += f" (+{len(identities) - 12} co-owners)"
    info = turn_info(session, season)
    phase = "Skipped-pick round" if info['catch_up'] else f"Round {info['round_no']}"
    return (f"⏭ <b>{_e(team.name)}</b>, you are on the clock. {tags}\n"
            f"{phase} · pick with <code>/dpick Player Name</code> or <code>/dskip</code>.\n"
            + clock_text(session, season))


def _available(session, season):
    return A.lots(session, season.id, status=A.LOT_QUEUED)


def _validate_pool(session, season, draft, teams, available):
    required = sum(max(0, draft.rounds - int(team.squad_size or 0)) for team in teams)
    if len(available) < required:
        raise AuctionError(f"The draft needs {required} available players; the pool has {len(available)}.")
    _validate_distinct_names(session, season, draft, teams, available)
    prices = sorted(price_for(session, season, lot) for lot in available)
    season.min_base_price_lakh = prices[0] if prices else 0
    role_pool = Counter(lot.category for lot in available)
    for role, minimum in A.role_minimums(season).items():
        needed = sum(max(0, minimum - A.role_counts(session, team.id).get(role, 0)) for team in teams)
        if needed > role_pool[role]:
            raise AuctionError(f"The pool needs {needed} more {role} players to meet every team's role minimum.")
    for rule in A.rating_rules(season):
        cap = rule["max_rating"]
        needed = sum(max(0, rule["min_players"] - sum(r <= cap for r in A.squad_ratings(session, team.id))) for team in teams)
        if needed > sum(int(lot.rating) <= cap for lot in available):
            raise AuctionError(f"Not enough players rated ≤{cap} remain for every team's rating minimum.")
    for team in teams:
        slots = max(0, draft.rounds - int(team.squad_size or 0))
        if sum(prices[:slots]) > int(team.purse_remaining_lakh or 0):
            raise AuctionError(f"{team.name}'s purse cannot cover its remaining {slots} picks.")
        owed, over = A.role_shortfall(session, season, team)
        if over or sum(owed.values()) > slots:
            raise AuctionError(f"{team.name}'s role rules cannot be met in {draft.rounds} rounds.")
        if any(rule["min_players"] > draft.rounds for rule in A.rating_rules(season)):
            raise AuctionError("A rating minimum exceeds the number of rounds.")


def _validate_distinct_names(session, season, draft, teams, available, *, signing_team=None, signing_lot=None):
    """Reject pools which count several editions as distinct squad places."""
    supply = Counter(lot.name.strip().casefold() for lot in available)
    needs, owned = {}, {}
    for team in teams:
        owned[team.id] = {lot.name.strip().casefold() for lot in A.squad(session, team.id)}
        count = int(team.squad_size or 0)
        if signing_team is not None and team.id == signing_team.id:
            owned[team.id].add(signing_lot.name.strip().casefold())
            count += 1
        needs[team.id] = max(0, int(draft.rounds) - count)
        if len(set(supply) - owned[team.id]) < needs[team.id]:
            raise AuctionError(f"{team.name} need {needs[team.id]} different players; "
                               "the pool has too few unique names. A squad can hold only one version of a player.")
    capacity = sum(min(count, sum(needs[team.id] > 0 and name not in owned[team.id] for team in teams))
                   for name, count in supply.items())
    if capacity < sum(needs.values()):
        raise AuctionError("Too many pool cards are versions of the same player. Add more different players to fill every squad.")


def start(session, season, *, by_tg_id=None, now=None):
    _manager(session, by_tg_id)
    draft = _draft(session, season)
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise AuctionError("Only a draft in setup or paused can start.")
    if not season.chat_id:
        raise AuctionError("Bind the draft to its Telegram group before starting.")
    draft = _claim(session, season, draft)
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise AuctionError("The draft state changed. Open its latest board.")
    _invariants(season, draft)
    teams = _ordered_teams(session, season)
    if len(teams) < 2:
        raise AuctionError("Add at least two franchises before starting the draft.")
    if any(not owner_ids(team) for team in teams):
        raise AuctionError("Assign an owner or co-owner to every team before starting.")
    if turn(session, season) is None:
        raise AuctionError("All picks are complete. Add rounds to continue.")
    A.set_role_rules(session, season, A.role_minimums(season), A.role_maximums(season))
    A.set_rating_rules(session, season, A.rating_rules(season))
    _validate_pool(session, season, draft, teams, _available(session, season))
    draft.order_json = _dump([team.id for team in teams])
    for team in teams:
        team.draft_picks_total = draft.rounds
    remaining = draft.paused_seconds_left
    season.status = A.STATUS_LIVE
    _start_clock(session, season, now=now, seconds=remaining)
    session.flush()
    _event(session, season, "fd_started", "🏏 <b>Franchise Draft is live!</b>\n"
           f"{draft.rounds} rounds · rotating order · no overseas limit.\n\n" + next_team_html(session, season),
           by_tg_id=by_tg_id)
    return draft


def pause(session, season, *, by_tg_id=None, now=None):
    _manager(session, by_tg_id)
    draft = _draft(session, season)
    if season.status != A.STATUS_LIVE:
        raise AuctionError("Only a live draft can be paused.")
    _claim(session, season, draft)
    if season.status != A.STATUS_LIVE:
        raise AuctionError("The draft is no longer live.")
    draft.paused_seconds_left = (max(0, math.ceil((draft.pick_deadline_at - (now or datetime.utcnow())).total_seconds()))
                                 if draft.pick_deadline_at is not None else None)
    draft.pick_deadline_at = None
    season.status = A.STATUS_PAUSED
    session.flush()
    _event(session, season, "fd_paused", "⏸ <b>Draft paused</b>\nPending confirmations have expired. Resume when every team is ready.",
           by_tg_id=by_tg_id)
    return draft


def _validate_pick(session, season, team, lot, price):
    draft = _draft(session, season)
    # Previewing is read-only apart from its durable quote. The shared
    # validator gets a rule snapshot so it cannot overwrite a concurrent
    # organiser's settings while checking draft-specific invariants.
    season = SimpleNamespace(**{column.key: getattr(season, column.key)
                               for column in AuctionSeason.__table__.columns})
    _invariants(season, draft)
    if lot.season_id != season.id or lot.status != A.LOT_QUEUED:
        raise AuctionError("That player is already signed or unavailable.")
    if lot.player_id is None:
        raise AuctionError("This player's catalogue card is missing. Ask the organiser to repair its pool entry.")
    if any(signed.name.strip().casefold() == lot.name.strip().casefold()
           for signed in A.squad(session, team.id)):
        raise AuctionError(f"{team.name} already have {lot.name}. A squad may hold only one version of a player.")
    available = _available(session, season)
    actual_prices = [price_for(session, season, p) for p in available]
    season.min_base_price_lakh = min(actual_prices, default=0)
    A.validate_preauction_signing(session, season, team, lot, price)
    A.check_role_rules(session, season, team, lot)
    slots_after = int(draft.rounds) - int(team.squad_size or 0) - 1
    remaining = [p for p in available if p.id != lot.id]
    _validate_distinct_names(session, season, draft, A.franchises(session, season.id), remaining,
                             signing_team=team, signing_lot=lot)
    counts = A.role_counts(session, team.id)
    counts[lot.category] = counts.get(lot.category, 0) + 1
    for role, minimum in A.role_minimums(season).items():
        needed = max(0, minimum - counts.get(role, 0))
        if needed > sum(p.category == role for p in remaining):
            raise AuctionError(f"This pick leaves too few {role} players available to meet the squad minimum.")
    ratings = A.squad_ratings(session, team.id) + [int(lot.rating)]
    for rule in A.rating_rules(season):
        needed = max(0, rule["min_players"] - sum(r <= rule["max_rating"] for r in ratings))
        if needed > sum(int(p.rating) <= rule["max_rating"] for p in remaining):
            raise AuctionError(f"This pick leaves too few players rated ≤{rule['max_rating']} for the rating minimum.")
    # Protect the room's remaining obligations too. A team that already has
    # its keeper must not consume the only keeper another team still needs.
    teams = A.franchises(session, season.id)
    role_pool = Counter(p.category for p in remaining)
    for role, minimum in A.role_minimums(season).items():
        total_needed = 0
        for other in teams:
            have = A.role_counts(session, other.id).get(role, 0)
            if other.id == team.id and lot.category == role:
                have += 1
            total_needed += max(0, minimum - have)
        if total_needed > role_pool[role]:
            raise AuctionError(f"This pick would leave too few {role} players for the remaining teams' role minimums.")
    for rule in A.rating_rules(season):
        cap = rule["max_rating"]
        total_needed = 0
        for other in teams:
            have = sum(r <= cap for r in A.squad_ratings(session, other.id))
            if other.id == team.id and int(lot.rating) <= cap:
                have += 1
            total_needed += max(0, rule["min_players"] - have)
        if total_needed > sum(int(p.rating) <= cap for p in remaining):
            raise AuctionError(f"This pick would leave too few players rated ≤{cap} for the remaining teams' rating minimums.")
    reserve = sum(sorted(price_for(session, season, p) for p in remaining)[:slots_after])
    if len(remaining) < slots_after or int(team.purse_remaining_lakh or 0) - price < reserve:
        raise AuctionError(f"This pick would leave too little purse to complete the squad. "
                           f"Hold back at least {_money(season, reserve)} for its remaining {slots_after} picks.")


def preview_pick(session, season, actor_tg_id, query, *, now=None):
    draft = _draft(session, season)
    if season.status != A.STATUS_LIVE:
        raise AuctionError("The draft is not live. Ask the organiser to start or resume it.")
    _check_time(draft, now)
    team = turn(session, season)
    if team is not None:
        session.refresh(team)
    _require_owner(team, actor_tg_id)
    lot = find_player(session, season, query)
    price = price_for(session, season, lot)
    _validate_pick(session, season, team, lot, price)
    row = FranchiseDraftConfirmation(token=secrets.token_hex(12), season_id=season.id,
            franchise_id=team.id, lot_id=lot.id, actor_tg_id=int(actor_tg_id),
            revision=int(draft.revision), price_lakh=price, status="pending", created_at=now or datetime.utcnow())
    session.add(row)
    session.flush()
    return row


def _confirmation(session, season, token, actor_tg_id, *, now=None):
    row = session.get(FranchiseDraftConfirmation, str(token))
    if row is None or row.season_id != season.id:
        raise AuctionError("This confirmation belongs to another draft or has expired.")
    if row.actor_tg_id != int(actor_tg_id):
        raise AuctionError("Only the owner or co-owner who requested this pick can answer its confirmation.")
    if row.status != "pending":
        raise AuctionError("This confirmation has already been answered. Open /draft for the latest state.")
    if (now or datetime.utcnow()) - row.created_at > QUOTE_LIFETIME:
        raise AuctionError("This pick confirmation expired. Use /dpick again for a fresh quote.")
    return row


def confirm_pick(session, season, token, actor_tg_id, *, now=None):
    row = _confirmation(session, season, token, actor_tg_id, now=now)
    draft = _draft(session, season)
    if season.status != A.STATUS_LIVE or int(row.revision) != int(draft.revision):
        raise AuctionError("This pick is stale because the draft changed. Use /dpick again.")
    draft = _claim(session, season, draft)
    # After acquiring the lock, stale quotes cannot be accepted by a second
    # owner even when they had both read the same current turn beforehand.
    if season.status != A.STATUS_LIVE or int(draft.revision) != int(row.revision) + 1:
        raise AuctionError("This pick is stale. Use /dpick again.")
    _check_time(draft, now)
    info = turn_info(session, season)
    team = info['team']
    if team is not None:
        session.refresh(team)
    _require_owner(team, actor_tg_id)
    if team.id != row.franchise_id:
        raise AuctionError("It is another team's turn. Use /dpick again when your team is called.")
    lot = session.get(AuctionLot, row.lot_id)
    if lot is None or price_for(session, season, lot) != row.price_lakh:
        raise AuctionError("The player's price changed. Use /dpick again to review the new price.")
    _validate_pick(session, season, team, lot, row.price_lakh)
    consumed = (session.query(FranchiseDraftConfirmation)
                .filter_by(token=row.token, status="pending")
                .update({"status": "confirmed"}, synchronize_session=False))
    if consumed != 1:
        raise AuctionError("This confirmation has already been answered or cancelled.")
    number = _next_number(session, FranchiseDraftPick, FranchiseDraftPick.overall_no, season)
    before = _turn_state(draft)
    # _sign_before_auction reuses the exact existing lot snapshot; it needs
    # only the catalogue id because a missing lot was refused above.
    signed = A._sign_before_auction(session, season, team, SimpleNamespace(id=lot.player_id), row.price_lakh,
        kind=A.ACQ_DRAFTED, counter="draft_picks_used", ledger_kind=A.LEDGER_DRAFT,
        verb="draft", note=f"Franchise draft pick {number}: {lot.name}",
        event_kind="fd_picked", mark="🏏", word="draft", by_tg_id=actor_tg_id, quiet=True, now=now)
    draft = _draft(session, season)
    row.status = "confirmed"
    session.add(FranchiseDraftPick(season_id=season.id, overall_no=number,
        franchise_id=team.id, lot_id=signed.id, price_lakh=row.price_lakh, actor_tg_id=actor_tg_id))
    _record_turn(session, season, team, "pick", before, pick_no=number, actor=actor_tg_id, now=now)
    _advance_turn(session, season, info, now=now)
    session.flush()
    phase = "Skipped-pick round" if info['catch_up'] else f"Round {info['round_no']}"
    _event(session, season, "fd_picked", f"✅ <b>Signing confirmed</b> · {phase}\n"
        f"<b>{_e(team.name)}</b> sign <b>{_e(signed.name)}</b>\n"
        f"{signed.rating} OVR · {_e(signed.category)} · {_money(season, signed.sold_price_lakh)}\n"
        f"💰 Purse left: <b>{_money(season, team.purse_remaining_lakh)}</b>\n"
        f"👥 Squad: <b>{team.squad_size}/{draft.rounds}</b>\n\n" + next_team_html(session, season),
        by_tg_id=actor_tg_id, lot=signed, franchise=team,
        metadata={'round_no': info['round_no'], 'catch_up': info['catch_up']})
    return signed


def cancel_pick(session, season, token, actor_tg_id):
    row = _confirmation(session, season, token, actor_tg_id)
    team = session.get(AuctionFranchise, row.franchise_id)
    _require_owner(team, actor_tg_id)
    changed = (session.query(FranchiseDraftConfirmation)
               .filter_by(token=row.token, status="pending")
               .update({"status": "cancelled"}, synchronize_session=False))
    if changed != 1:
        raise AuctionError("This confirmation has already been answered.")
    session.expire(row)
    return row


def undo_last(session, season, *, by_tg_id=None):
    _manager(session, by_tg_id)
    draft = _draft(session, season)
    if season.league_id:
        raise AuctionError("Published draft picks cannot be undone.")
    if season.status not in (A.STATUS_PAUSED, A.STATUS_COMPLETED):
        raise AuctionError("Pause the draft before undoing its last pick.")
    if not draft.cursor:
        raise AuctionError("There is no draft pick to undo.")
    draft = _claim(session, season, draft)
    if season.league_id or season.published_at:
        raise AuctionError("Published draft picks cannot be undone.")
    if season.status not in (A.STATUS_PAUSED, A.STATUS_COMPLETED):
        raise AuctionError("Pause the draft before undoing its last pick.")
    history = (session.query(FranchiseDraftTurn).filter_by(season_id=season.id)
               .order_by(FranchiseDraftTurn.number.desc()).first())
    if history is not None:
        before = _loads(history.before_state_json, {})
        if history.kind in ("skip", "timeout"):
            team = session.get(AuctionFranchise, history.franchise_id)
            draft.cursor = before['cursor']
            draft.skipped_turns_json = _dump(before['skipped'])
            draft.pick_deadline_at = None
            draft.paused_seconds_left = int(draft.pick_minutes or 0) * 60 or None
            season.status = A.STATUS_PAUSED
            session.delete(history)
            _event(session, season, "fd_undo", f"↩️ <b>Last skipped turn restored</b>\n"
                   f"{_e(team.name)} can pick when the draft resumes. No purse change.\n\n"
                   + next_team_html(session, season), by_tg_id=by_tg_id, franchise=team)
            return None
        pick = (session.query(FranchiseDraftPick)
                .filter_by(season_id=season.id, overall_no=history.pick_no).one_or_none())
    else:
        # Journals written before timed turns contained successful picks only.
        before = dict(cursor=int(draft.cursor) - 1, skipped=[])
        pick = (session.query(FranchiseDraftPick).filter_by(season_id=season.id)
                .order_by(FranchiseDraftPick.overall_no.desc()).first())
    if pick is None:
        raise AuctionError("The pick journal is incomplete. Ask an administrator to inspect this draft.")
    team = session.get(AuctionFranchise, pick.franchise_id)
    lot = session.get(AuctionLot, pick.lot_id)
    session.refresh(team)
    if lot is not None:
        session.refresh(lot)
    if lot is None or lot.status != A.LOT_SOLD or lot.sold_to_id != team.id or lot.sold_price_lakh != pick.price_lakh:
        raise AuctionError("This pick changed outside the draft. Restore it before using Undo.")
    team.purse_remaining_lakh = int(team.purse_remaining_lakh) + int(pick.price_lakh)
    team.squad_size = int(team.squad_size) - 1
    team.draft_picks_used = int(team.draft_picks_used) - 1
    A._ledger(session, team, A.LEDGER_CORRECTION, int(pick.price_lakh), lot=lot,
              note=f"Undo franchise draft pick {pick.overall_no}", by_tg_id=by_tg_id)
    lot.status = A.LOT_QUEUED
    lot.acquisition = A.ACQ_AUCTION
    lot.sold_to_id = lot.sold_price_lakh = lot.sold_at = None
    draft.cursor = before['cursor']
    draft.skipped_turns_json = _dump(before['skipped'])
    draft.pick_deadline_at = None
    draft.paused_seconds_left = int(draft.pick_minutes or 0) * 60 or None
    season.status = A.STATUS_PAUSED
    session.delete(pick)
    if history is not None:
        session.delete(history)
    session.flush()
    _event(session, season, "fd_undo", f"↩️ <b>Last draft pick undone</b>\n"
        f"{_e(lot.name)} returns to the pool. {_e(team.name)} receive {_money(season, pick.price_lakh)} back.\n"
        "The draft remains paused.\n\n" + next_team_html(session, season),
        by_tg_id=by_tg_id, lot=lot, franchise=team)
    return lot


def _event(session, season, kind, html, *, by_tg_id=None, lot=None, franchise=None, metadata=None):
    # Snapshot display values with the committed turn. Delivery may retry after
    # later picks, so rebuilding then would announce the wrong purse or owner.
    detail = {'html': html, **(metadata or {})}
    try:
        from services import franchise_draft_rich as DR
        blocks, rendered = DR.event_card(session, season, kind, html,
            lot=lot, team=franchise, metadata=metadata)
        detail.update(html=rendered, blocks=blocks)
    except Exception:
        # A presentation failure must never invalidate a signing or timeout.
        logger.exception('Draft event presentation failed; preserving HTML')
    event = A.log_event(session, season, kind, re.sub(r"<[^>]+>", "", html).split("\n")[0],
        detail=detail, by_tg_id=by_tg_id, lot=lot, franchise=franchise)
    session.flush()
    return event


def render_order(session, season):
    draft = _draft(session, season)
    lines = ["📋 <b>Rotating draft order</b>"]
    # A hundred rounds and a large field exceed Telegram's text limit. The
    # next three rounds explain the rotation while keeping the card readable.
    teams = _ordered_teams(session, season)
    current = min(int(draft.rounds), int(draft.cursor) // max(1, len(teams)) + 1)
    truncated = False
    for number in range(current, min(int(draft.rounds), current + 2) + 1):
        if truncated:
            break
        lines.append(f"\n<b>Round {number}</b>")
        for index, team in enumerate(round_order(session, season, number), 1):
            row = f"{index}. {_e(team.name[:100])} · #{team.id}"
            if len("\n".join(lines)) + len(row) > 3300:
                truncated = True
                break
            lines.append(row)
    if truncated:
        lines.append("\nMore teams follow the same rotation. Open /draft → Squads for the full franchise list.")
    if int(draft.rounds) > current + 2:
        lines.append(f"\n{draft.rounds} rounds in total. Each round moves the first team to the end.")
    return "\n".join(lines)


def render_skips(session, season, page=1):
    queue = skipped_turns(session, season)
    teams = {t.id: t for t in _ordered_teams(session, season)}
    pages = max(1, (len(queue) + 11) // 12)
    page = max(1, min(pages, int(page)))
    lines = [f"⏭ <b>Skipped-pick round</b> · Page {page}/{pages}",
             f"{len(queue)} deferred picks · played after the scheduled rounds, in skip order."]
    for index, entry in enumerate(queue[(page - 1) * 12:page * 12], (page - 1) * 12 + 1):
        team = teams.get(entry['franchise_id'])
        lines.append(f"{index}. <b>{_e(team.name if team else 'Missing franchise')}</b> · from round {entry['round_no']}")
    if not queue:
        lines.append("No skipped picks. Every team keeps its scheduled turns.")
    lines.append("Skipping again moves that pick to the back of the queue; it never costs purse.")
    return "\n".join(lines)


def render_board(session, season):
    draft = _draft(session, season)
    teams = _ordered_teams(session, season)
    total = draft.rounds * len(teams)
    number = min(draft.rounds, int(draft.cursor) // max(1, len(teams)) + 1)
    lines = [f"🏏 <b>{_e(season.name)}</b>",
        f"Franchise Draft · <b>{_e(season.status.title())}</b>",
        ("<b>Skipped-pick round</b>" if int(draft.cursor) >= total and skipped_turns(session, season)
         else f"Round <b>{number}/{draft.rounds}</b>") + f" · Signed <b>{sum(int(t.squad_size or 0) for t in teams)}/{total}</b>",
        f"⏭ {len(skipped_turns(session, season))} deferred picks · <code>/dskips</code>",
        f"Squad limit: {draft.rounds} · No overseas limit", ""]
    footer = next_team_html(session, season) if season.status in (A.STATUS_LIVE, A.STATUS_COMPLETED) else "Start or resume the draft when the field and prices are ready."
    for index, team in enumerate(teams):
        row = f"<b>{_e(team.name)}</b> · {int(team.squad_size or 0)}/{draft.rounds} · {_money(season, team.purse_remaining_lakh)}"
        if len("\n".join(lines)) + len(row) + len(footer) > 3100:
            lines.append(f"… {len(teams) - index} more franchises. Open /draft → Squads for players and purses.")
            break
        lines.append(row)
    if A.role_rule_line(season):
        lines.extend(["", "📐 Roles: " + _e(A.role_rule_line(season)[:250])])
    if A.rating_rule_line(season):
        lines.append("⭐ " + _e(A.rating_rule_line(season)[:250]))
    if season.status not in (A.STATUS_LIVE, A.STATUS_COMPLETED):
        lines.append(clock_text(session, season))
    lines.extend(["", footer])
    return "\n".join(lines)


def render_pool(session, season, page=1):
    rows = _available(session, season)
    pages = max(1, (len(rows) + 11) // 12)
    page = max(1, min(pages, int(page)))
    lines = [f"🔎 <b>Available draft players</b> · Page {page}/{pages}", f"{len(rows)} players available", ""]
    for lot in rows[(page - 1) * 12:page * 12]:
        try:
            price = _money(season, price_for(session, season, lot))
        except AuctionError:
            price = "Price needed"
        lines.append(f"<b>#{lot.id} {_e(lot.name)}</b> · {lot.rating} · {_e(lot.category)} · {price}")
    lines.append("\nPick by name or exact pool ID: <code>/dpick #123</code>.")
    return "\n".join(lines)


def render_squad(session, season, franchise):
    draft = _draft(session, season)
    if franchise.season_id != season.id:
        raise AuctionError("That franchise belongs to another season.")
    lines = [f"👥 <b>{_e(franchise.name)} — Draft squad</b>",
        f"Players: {franchise.squad_size}/{draft.rounds} · Purse: <b>{_money(season, franchise.purse_remaining_lakh)}</b>",
        f"Spent: {_money(season, int(franchise.purse_total_lakh) - int(franchise.purse_remaining_lakh))}", ""]
    for index, lot in enumerate(A.squad(session, franchise.id), 1):
        lines.append(f"{index}. <b>{_e(lot.name)}</b> · {lot.rating} · {_e(lot.category)} · {_money(season, lot.sold_price_lakh)}")
    counts = A.role_counts(session, franchise.id)
    lines.extend(["", " · ".join(f"{_e(role)}: {counts.get(role, 0)}" for role in A.SQUAD_ROLES)])
    if A.rating_progress_line(session, season, franchise):
        lines.append(_e(A.rating_progress_line(session, season, franchise)))
    return "\n".join(lines)
