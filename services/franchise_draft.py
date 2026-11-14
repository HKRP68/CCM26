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


class AmbiguousDraftPlayer(AuctionError):
    def __init__(self, message, players):
        super().__init__(message)
        self.players = players


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
    value=int(price)
    whole,fraction=divmod(abs(value),100)
    amount=str(whole)+(('.'+f'{fraction:02d}'.rstrip('0')) if fraction else '')
    return ('-' if value<0 else '')+f'{season.currency_label or "₹"}{amount} Cr'


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


def started(session, season):
    draft = _draft(session, season)
    return bool(draft.started_at or draft.cursor or season.status != A.STATUS_SETUP)


def timer_seconds(draft):
    return int(_loads(draft.settings_json, {}).get('pick_seconds', int(draft.pick_minutes or 0)*60))


def _price_editable(session, season, reason=None):
    draft = _editable(session, season)
    if started(session, season) and not str(reason or '').strip():
        raise AuctionError('Prices are locked after the draft starts. Use /dcorrect with a recorded reason while paused.')
    if len(str(reason or ''))>300:raise AuctionError('Use a correction reason of at most 300 characters.')
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
    season.max_squad_size = int(draft.rounds)
    season.min_squad_size = int(_loads(draft.settings_json, {}).get('min_squad_size', draft.rounds))
    if not 1 <= season.min_squad_size <= season.max_squad_size:
        raise AuctionError('Minimum squad size must be between 1 and the maximum squad size.')
    season.min_overseas = 0
    season.max_overseas = int(draft.rounds)
    season.max_retentions = season.min_retentions = season.expansion_picks = 0
    season.rtm_enabled = False
    season.rtm_per_team = 0
    season.draft_pick_seconds = 0
    season.current_lot_id = None


def enable(session, season, rounds=15, *, min_squad_size=None, by_tg_id=None):
    _manager(session, by_tg_id)
    count = _rounds(rounds)
    minimum = count if min_squad_size is None else _rounds(min_squad_size)
    if minimum > count:
        raise AuctionError('Minimum squad size cannot exceed maximum squad size.')
    if get_draft(session, season) is not None:
        raise AuctionError("This season already uses Franchise Draft.")
    if season.status != A.STATUS_SETUP or season.league_id:
        raise AuctionError("Choose draft mode during setup, before any players are signed.")
    if (session.query(AuctionLot.id).filter(AuctionLot.season_id == season.id,
            AuctionLot.status != A.LOT_QUEUED).first()
            or session.query(AuctionBid.id).filter(AuctionBid.season_id == season.id).first()
            or any(int(f.squad_size or 0) for f in A.franchises(session, season.id))):
        raise AuctionError("Choose draft mode before any players are signed or bid on.")
    row = FranchiseDraft(season_id=season.id, rounds=count, cursor=0, revision=0,
                         settings_json=_dump({'timeout_policy': 'pause', **({'min_squad_size': minimum} if min_squad_size is not None else {})}))
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
        team.draft_picks_total = count - int(team.retained_count or 0)
        team.draft_picks_used = 0
        team.rtm_cards_total = team.rtm_cards_used = 0
        team.retention_finalized_at = None
        team.draft_shortlist_json = None
    session.flush()
    _event(session, season, "fd_enabled", "🏏 <b>Franchise Draft selected</b>\n"
           f"{count} rounds · {minimum}–{count} players per team · no overseas limit.", by_tg_id=by_tg_id)
    return row


def configure_rounds(session, season, rounds, *, min_squad_size=None, by_tg_id=None):
    _manager(session, by_tg_id)
    count = _rounds(rounds)
    draft = _draft(session, season)
    settings = _loads(draft.settings_json, {})
    minimum = _rounds(min_squad_size) if min_squad_size is not None else int(settings.get('min_squad_size', count))
    if minimum > count:
        raise AuctionError('Minimum squad size cannot exceed maximum squad size.')
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
    if min_squad_size is not None:
        settings = _loads(draft.settings_json, {})
        settings['min_squad_size'] = minimum
        draft.settings_json = _dump(settings)
    _invariants(season, draft)
    for team in teams:
        team.draft_picks_total = count - int(team.retained_count or 0)
    if was_completed:
        season.status = A.STATUS_PAUSED
    session.flush()
    A.set_role_rules(session, season, A.role_minimums(season), A.role_maximums(season))
    A.set_rating_rules(session, season, A.rating_rules(season))
    _event(session, season, "fd_rounds", f"🔢 <b>Draft rounds updated: {count}</b>\n"
           f"Squad minimum: {minimum}. Squad maximum: {count}." + (" Resume the draft to continue picking." if was_completed else ""),
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
        if not re.search(r'[:=]', line):
            raise AuctionError(f"Invalid price line: {line[:100]}. Use name:25.5cr or 96:23cr.")
        key, value = re.split(r'[:=]', line, maxsplit=1)
        if not key.strip():
            raise AuctionError("Each price needs a rating or player name.")
        result.append((key.strip(), parse_money(value)))
    return result


def set_rating_prices(session, season, text, *, by_tg_id=None, correction_reason=None):
    _manager(session, by_tg_id)
    draft = _price_editable(session, season, correction_reason)
    before_prices=_loads(draft.rating_prices_json,{})
    prices = {}
    for raw, price in _price_rows(text):
        if not re.fullmatch(r"\d{1,3}", raw) or not 1 <= int(raw) <= 999:
            raise AuctionError("Ratings must be whole numbers between 1 and 999, for example 96:23cr.")
        key = str(int(raw))
        if key in prices:
            raise AuctionError(f"Rating {key} appears more than once.")
        prices[key] = price
    draft = _claim(session, season, draft)
    _price_editable(session, season, correction_reason)
    draft.rating_prices_json = _dump(prices)
    session.flush()
    _event(session, season, "fd_prices", f"💰 <b>Rating prices saved</b>\n{len(prices)} ratings priced. "
           "Custom player prices take priority. Existing signings keep their price.", by_tg_id=by_tg_id,
           metadata={'reason':correction_reason,'before_prices':before_prices,'after_prices':prices})
    return prices


def find_player(session, season, query, *, available_only=True, _lots=None, partial=False):
    raw = str(query or "").strip()
    if not raw:
        raise AuctionError("Choose a player: /dpick Joe Root. Use a pool #ID for duplicate names.")
    rows = A.lots(session, season.id) if _lots is None else _lots
    if raw.startswith("#") and raw[1:].isdigit():
        found = [lot for lot in rows if lot.id == int(raw[1:])]
    elif raw.lower().startswith('p') and raw[1:].isdigit():
        found = [lot for lot in rows if lot.player_id == int(raw[1:])]
    else:
        found = [lot for lot in rows if lot.name.casefold() == raw.casefold()]
        if not found:
            if partial:
                tokens = re.findall(r'\w+', raw.casefold())
                found = [lot for lot in rows if tokens and all(
                    any(word.startswith(token) for word in re.findall(r'\w+', lot.name.casefold()))
                    for token in tokens)]
            else:
                found = [lot for lot in rows if lot.name.casefold().startswith(raw.casefold())]
    if partial and available_only:
        available = [lot for lot in found if lot.status == A.LOT_QUEUED]
        if available:
            found = available
        elif found:
            raise AuctionError(f'{raw[:100]} is already signed or unavailable.')
    if not found:
        raise AuctionError(f"{raw[:100]} is not in this draft's player pool.")
    if len(found) > 1:
        choices = ", ".join(f"#{lot.id} {lot.name} ({lot.version or 'Base'}, {lot.rating} OVR, player ID {lot.player_id})" for lot in found[:5])
        raise AmbiguousDraftPlayer("That name matches several player cards. Choose the player/version or use its #ID: " + choices, found)
    lot = found[0]
    if available_only and lot.status != A.LOT_QUEUED:
        raise AuctionError(f"{lot.name} is already signed or unavailable.")
    return lot


def custom_price_rows(session, season, text):
    """Read pasted overrides or a UTF-8 CSV/TSV of exact catalogue cards."""
    import csv
    import io
    text = str(text or '').lstrip('\ufeff')
    if len(text.encode('utf-8')) > 2_000_000:
        raise AuctionError('Custom price files must be under 2 MB.')
    first = text.splitlines()[0] if text.splitlines() else ''
    delimiter = '\t' if '\t' in first else ','
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
        headers = [value.strip().lower() for value in (reader.fieldnames or [])]
    except csv.Error as exc:
        raise AuctionError('Invalid custom price CSV/TSV. Check quoting and columns.') from exc
    if 'price_lakh' not in headers:
        return _price_rows(text)
    required = {'player_id', 'name', 'version', 'rating', 'category', 'price_lakh'}
    if not required.issubset(headers) or len(headers) != len(set(headers)):
        raise AuctionError('Use columns: player_id,name,version,rating,category,price_lakh.')
    result = []
    pool = A.lots(session, season.id)
    try:
        reader.fieldnames = headers
        for number, row in enumerate(reader, 2):
            if not any(str(value or '').strip() for value in row.values()):
                continue
            if None in row or any(value is None for value in row.values()):
                raise AuctionError(f'Price row {number}: the number of columns does not match the header.')
            row = {key: value.strip() for key, value in row.items()}
            if not re.fullmatch(r'[0-9]{1,16}', row['player_id']):
                raise AuctionError(f'Price row {number}: player_id must be a catalogue player ID.')
            lot = find_player(session, season, 'p' + row['player_id'], _lots=pool)
            expected = {'name': lot.name, 'version': lot.version or 'Base',
                        'rating': str(lot.rating), 'category': lot.category}
            for key, value in expected.items():
                if row[key] and row[key].casefold() != str(value).casefold():
                    raise AuctionError(f'Price row {number}: {key} does not match player ID {lot.player_id} ({lot.name}).')
            if not re.fullmatch(r'[0-9]{1,10}', row['price_lakh']):
                raise AuctionError(f'Price row {number}: price_lakh must be a non-negative whole number (100 = 1 Cr).')
            result.append((f'#{lot.id}', parse_money(row['price_lakh'] + 'L')))
            if len(result) > 2000:
                raise AuctionError('Use at most 2,000 custom price rows.')
    except csv.Error as exc:
        raise AuctionError('Invalid custom price CSV/TSV. Check quoting and columns.') from exc
    if not result:
        raise AuctionError('The custom price file has no player rows.')
    return result


def set_custom_prices(session, season, text, *, by_tg_id=None, correction_reason=None):
    _manager(session, by_tg_id)
    draft = _price_editable(session, season, correction_reason)
    prices = dict(_loads(draft.custom_prices_json, {}))
    before_prices=dict(prices)
    seen = set()
    pool = A.lots(session, season.id)
    for name, price in custom_price_rows(session, season, text):
        lot = find_player(session, season, name, _lots=pool)
        key = str(lot.id)
        if key in seen:
            raise AuctionError(f"{lot.name} appears more than once.")
        seen.add(key)
        prices[key] = price
    draft = _claim(session, season, draft)
    _price_editable(session, season, correction_reason)
    draft.custom_prices_json = _dump(prices)
    session.flush()
    _event(session, season, "fd_prices", f"💎 <b>Custom player prices saved</b>\n{len(seen)} player prices updated.",
           by_tg_id=by_tg_id,metadata={'reason':correction_reason,'before_prices':before_prices,'after_prices':prices})
    return prices


def price_for(session, season, lot):
    draft = _draft(session, season)
    custom = _loads(draft.custom_prices_json, {})
    if str(lot.id) in custom:
        value=custom[str(lot.id)]
    else:
        ratings = _loads(draft.rating_prices_json, {})
        if str(lot.rating) not in ratings:
            raise AuctionError(f"No draft price is configured for rating {lot.rating} ({lot.name}). "
                               "Add it with /dprices or set /dcustom.")
        value=ratings[str(lot.rating)]
    if isinstance(value,bool) or not isinstance(value,(int,str)) or not re.fullmatch(r'[0-9]+',str(value)) or int(value)>1_000_000_000:
        raise AuctionError(f'Invalid stored price for {lot.name}. An administrator must correct the price table.')
    return int(value)


def round_order(session, season, round_no):
    teams = _ordered_teams(session, season)
    if not teams:
        return []
    shift = (int(round_no) - 1) % len(teams)
    return teams[shift:] + teams[:shift]


def schedule(session, season):
    teams = _ordered_teams(session, season)
    if not teams:
        return []
    return [team for number in range(_draft(session, season).rounds)
            for team in teams[number % len(teams):] + teams[:number % len(teams)]]


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
    if season.status == A.STATUS_LIVE and timer_seconds(draft) and turn(session, season):
        duration = timer_seconds(draft) if seconds is None else max(0, int(seconds))
        draft.pick_deadline_at = now + timedelta(seconds=duration)
    else:
        draft.pick_deadline_at = None
    draft.paused_seconds_left = None


def _check_time(draft, now=None):
    if draft.pick_deadline_at is not None and (now or datetime.utcnow()) >= draft.pick_deadline_at:
        raise AuctionError("This team's pick time has expired. The configured timeout policy will be applied; open /draft for the current turn.")


def clock_text(session, season):
    draft = _draft(session, season)
    seconds=timer_seconds(draft)
    if not seconds:
        return "⏱ Pick timer: off"
    duration=f'{seconds//60} minutes' if seconds%60==0 else f'{seconds} seconds'
    text = f"⏱ <b>{duration}</b> per turn"
    if draft.pick_deadline_at is not None:
        text += f" · deadline <b>{draft.pick_deadline_at:%d %b %H:%M:%S} UTC</b>"
    elif season.status == A.STATUS_PAUSED and draft.paused_seconds_left is not None:
        text += f" · paused with {draft.paused_seconds_left // 60}m {draft.paused_seconds_left % 60}s left"
    return text


def set_timer(session, season, minutes, *, by_tg_id=None, timeout_policy=None):
    if isinstance(minutes, bool) or not re.fullmatch(r"\d+", str(minutes).strip()) or not 0 <= int(minutes) <= 1440:
        raise AuctionError("Pick time must be whole minutes from 1 to 1440; use 0 to turn the timer off.")
    return configure_clock(session, season, int(minutes)*60, by_tg_id=by_tg_id, timeout_policy=timeout_policy)


def configure_clock(session, season, seconds, *, by_tg_id=None, timeout_policy=None, now=None):
    """Change a running clock atomically; its current turn gets a full window."""
    _manager(session, by_tg_id)
    if isinstance(seconds,bool) or not re.fullmatch(r'\d+',str(seconds).strip()) or not 0<=int(seconds)<=86400:
        raise AuctionError('Use 0–86400 whole seconds; 0 disables the timer.')
    if timeout_policy is not None and timeout_policy not in ('pause','skip','auto','admin'):
        raise AuctionError('Choose pause, skip, auto or admin for timeout handling.')
    draft = _draft(session, season)
    def editable():
        if season.league_id or season.published_at or season.status not in (A.STATUS_SETUP,A.STATUS_LIVE,A.STATUS_PAUSED):
            raise AuctionError('Timer changes are available during setup, live or paused drafts before publication.')
    editable()
    draft = _claim(session, season, draft)
    editable()
    previous = timer_seconds(draft)
    seconds = int(seconds)
    draft.pick_minutes = (seconds+59)//60
    options = _loads(draft.settings_json, {})
    previous_policy = options.get('timeout_policy','skip')
    options.update(pick_seconds=seconds)
    options['timeout_policy']=timeout_policy or previous_policy
    draft.settings_json = _dump(options)
    _start_clock(session, season, now=now)
    draft.paused_seconds_left = seconds if season.status == A.STATUS_PAUSED and seconds else None
    _event(session, season, "fd_timer", "⏱ <b>Draft pick timer updated</b>\n" + clock_text(session, season)
           + ('\nThe current team receives the new full pick window.' if season.status==A.STATUS_LIVE else ''),
           by_tg_id=by_tg_id, metadata={'previous_seconds':previous,'seconds':seconds,
               'previous_policy':previous_policy,'policy':options['timeout_policy']})
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
    _pass_full_teams(session, season)
    if turn(session, season) is None:
        season.status = A.STATUS_COMPLETED
    _start_clock(session, season, now=now)


def _skip_locked(session, season, *, actor=None, timeout=False, now=None, admin_reason=None):
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
           metadata={'timeout': timeout, 'round_no': info['round_no'], 'catch_up': info['catch_up'],
                     'admin_override': bool(admin_reason), 'reason': admin_reason})
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
            and timer_seconds(draft) and turn(session, season) is not None):
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
    policy = _loads(draft.settings_json, {}).get('timeout_policy', 'skip')
    if policy == 'skip':
        return _skip_locked(session, season, timeout=True, now=now)
    if policy == 'auto':
        team=turn(session,season)
        slots=draft.rounds-team.squad_size-1
        counts=A.role_counts(session,team.id)
        minimums,maximums=A.role_minimums(season),A.role_maximums(season)
        ratings=A.squad_ratings(session,team.id)
        rules=A.rating_rules(season)
        owned={p.name.strip().casefold() for p in A.squad(session,team.id)}
        for lot in sorted(_available(session, season), key=lambda p: (-p.rating, p.id)):
            try:
                price=price_for(session,season,lot)
                if price>team.purse_remaining_lakh or lot.name.strip().casefold() in owned:continue
                if counts.get(lot.category,0)+1>maximums.get(lot.category,draft.rounds):continue
                if sum(max(0,n-counts.get(role,0)-(lot.category==role)) for role,n in minimums.items())>slots:continue
                if any(r['min_players']-sum(v<=r['max_rating'] for v in ratings)-(lot.rating<=r['max_rating'])>slots for r in rules):continue
                _validate_pick(session, season, team, lot, price)
            except AuctionError:
                continue
            draft.pick_deadline_at = None
            actor = owner_ids(turn(session, season))[0]
            quote = preview_pick(session, season, actor, '#'+str(lot.id), now=now)
            return confirm_pick(session, season, quote.token, actor, now=now, automatic=True)
    # Waiting for an admin and the pause policy both stop the clock without
    # spending a slot. If no automatic pick is legal, pause for intervention.
    pause(session, season, now=now)
    _event(session, season, 'fd_timeout_paused', '⏱ <b>Pick time expired — draft paused</b>\nNo legal automatic selection, or the configured policy requires intervention. No player or purse changed.')
    return None


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
    required = sum(_required_slots(session, season, team) for team in teams)
    if len(available) < required:
        raise AuctionError(f"The draft needs {required} available players; the pool has {len(available)}.")
    _validate_distinct_names(session, season, draft, teams, available)
    prices = sorted(price_for(session, season, lot) for lot in available)
    ids=[lot.player_id for lot in A.lots(session,season.id)]
    if None in ids or len(ids)!=len(set(ids)):
        raise AuctionError('The pool contains missing or duplicate player database IDs.')
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
        mandatory = _required_slots(session, season, team)
        if sum(prices[:mandatory]) > int(team.purse_remaining_lakh or 0):
            raise AuctionError(f"{team.name}'s purse cannot cover its remaining {mandatory} required picks.")
        if completion_reserve(session,season,team,available)>int(team.purse_remaining_lakh or 0):
            raise AuctionError(f'{team.name} cannot afford its mandatory category/rating slots.')
        owed, over = A.role_shortfall(session, season, team)
        if over or sum(owed.values()) > slots:
            raise AuctionError(f"{team.name}'s role rules cannot be met in {draft.rounds} rounds.")
        if any(rule["min_players"] > draft.rounds for rule in A.rating_rules(season)):
            raise AuctionError("A rating minimum exceeds the number of rounds.")
    if required:
        # A per-team check alone can count the same scarce low-rated keeper
        # as a solution for every franchise. Aggregate role/rating supply as
        # well, excluding cards no franchise can afford at all.
        counts_by_team={team.id:A.role_counts(session,team.id) for team in teams}
        totals=Counter()
        for counts in counts_by_team.values():totals.update(counts)
        lows,highs=A.role_minimums(season),A.role_maximums(season)
        owed={role:sum(max(0,n-counts_by_team[t.id].get(role,0)) for t in teams) for role,n in lows.items()}
        caps={role:totals[role]+sum(min(max(0,draft.rounds-t.squad_size),max(0,highs.get(role,draft.rounds)-counts_by_team[t.id].get(role,0))) for t in teams) for role in A.SQUAD_ROLES}
        rules=A.rating_rules(season)
        needs=[sum(max(0,r['min_players']-sum(v<=r['max_rating'] for v in A.squad_ratings(session,t.id))) for t in teams) for r in rules]
        ceiling=max(int(t.purse_remaining_lakh) for t in teams)
        affordable=[p for p in available if price_for(session,season,p)<=ceiling]
        capacity=sum(max(0,draft.rounds-t.squad_size) for t in teams)
        reserve=_completion_flow(affordable,capacity,totals,owed,caps,rules,needs,
                                lambda p:price_for(session,season,p),distinct_names=False,min_slots=required)
        if reserve>sum(int(t.purse_remaining_lakh) for t in teams):
            raise AuctionError('The franchises collectively cannot afford the mandatory squad composition.')


def _validate_distinct_names(session, season, draft, teams, available, *, signing_team=None, signing_lot=None):
    """Reject pools which count several editions as distinct squad places."""
    supply = Counter(lot.name.strip().casefold() for lot in available)
    needs, owned = {}, {}
    for team in teams:
        signed = list(A.squad(session, team.id))
        owned[team.id] = {lot.name.strip().casefold() for lot in signed}
        if signing_team is not None and team.id == signing_team.id:
            owned[team.id].add(signing_lot.name.strip().casefold())
            signed.append(signing_lot)
        needs[team.id] = _required_slots(session,season,team,
            signing_lot=signing_lot if signing_team is not None and team.id == signing_team.id else None,_signed=signed)
        if len(set(supply) - owned[team.id]) < needs[team.id]:
            raise AuctionError(f"{team.name} need {needs[team.id]} different players; "
                               "the pool has too few unique names. A squad can hold only one version of a player.")
    capacity = sum(min(count, sum(needs[team.id] > 0 and name not in owned[team.id] for team in teams))
                   for name, count in supply.items())
    if capacity < sum(needs.values()):
        raise AuctionError("Too many pool cards are versions of the same player. Add more different players to fill every squad.")


def start(session, season, *, by_tg_id=None, now=None, resume_only=False, _audit_actor=None):
    _manager(session, by_tg_id)
    draft = _draft(session, season)
    allowed = (A.STATUS_PAUSED,) if resume_only else (A.STATUS_SETUP, A.STATUS_PAUSED)
    if season.status not in allowed:
        raise AuctionError("Only a draft in setup or paused can start.")
    if not season.chat_id:
        raise AuctionError("Bind the draft to its Telegram group before starting.")
    draft = _claim(session, season, draft)
    if season.status not in allowed:
        raise AuctionError("The draft state changed. Open its latest board.")
    _invariants(season, draft)
    teams = _ordered_teams(session, season)
    if len(teams) < 2:
        raise AuctionError("Add at least two franchises before starting the draft.")
    if any(not team.owner_tg_id or int(team.owner_tg_id)<=0 for team in teams):
        raise AuctionError("Assign a valid owner to every team before starting.")
    if turn(session, season) is None:
        raise AuctionError("All picks are complete. Add rounds to continue.")
    A.set_role_rules(session, season, A.role_minimums(season), A.role_maximums(season))
    A.set_rating_rules(session, season, A.rating_rules(season))
    _validate_pool(session, season, draft, teams, _available(session, season))
    draft.order_json = _dump([team.id for team in teams])
    for team in teams:
        team.draft_picks_total = draft.rounds - int(team.retained_count or 0)
    remaining = draft.paused_seconds_left
    draft.started_at = draft.started_at or now or datetime.utcnow()
    season.status = A.STATUS_LIVE
    _pass_full_teams(session, season)
    _start_clock(session, season, now=now, seconds=remaining)
    session.flush()
    if turn(session,season) is None:
        from services.franchise_draft_admin import validate_final
        validate_final(session,season)
        season.status=A.STATUS_COMPLETED
        _event(session,season,'fd_completed','🏆 <b>Draft squads completed through retained contracts</b>',by_tg_id=by_tg_id)
    else:
        _event(session, season, 'fd_resumed' if resume_only else 'fd_started',
               ('▶ <b>Franchise Draft resumed!</b>\n' if resume_only else '🏏 <b>Franchise Draft is live!</b>\n') +
               f"{draft.rounds} rounds · rotating order · no overseas limit.\n\n" + next_team_html(session, season),
               by_tg_id=_audit_actor if _audit_actor is not None else by_tg_id)
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


def finish(session, season, *, by_tg_id=None):
    """Close optional places only after every squad meets all final rules."""
    _manager(session, by_tg_id)
    draft = _claim(session, season)
    if season.status not in (A.STATUS_LIVE, A.STATUS_PAUSED) or season.published_at:
        raise AuctionError('Only a live or paused, unpublished draft can finish.')
    _invariants(season, draft)
    from services.franchise_draft_admin import validate_final
    teams = _ordered_teams(session, season)
    if not teams:
        raise AuctionError('Add franchises before finishing a draft.')
    validate_final(session, season)
    before = _turn_state(draft)
    _record_turn(session, season, teams[0], 'finish', before, actor=by_tg_id)
    draft.cursor = draft.rounds * len(teams)
    draft.skipped_turns_json = '[]'
    draft.pick_deadline_at = draft.paused_seconds_left = None
    season.status = A.STATUS_COMPLETED
    session.flush()
    _event(session, season, 'fd_completed', '🏆 <b>Franchise Draft completed</b>\n'
           'Every squad meets its minimum and final rules. Remaining optional places are closed.', by_tg_id=by_tg_id)
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
    if session.query(AuctionLot.id).filter(AuctionLot.season_id==season.id,
            AuctionLot.player_id==lot.player_id,AuctionLot.id!=lot.id).first():
        raise AuctionError('This player database ID occurs more than once in the pool. An administrator must remove the duplicate before signing.')
    if any(signed.name.strip().casefold() == lot.name.strip().casefold()
           for signed in A.squad(session, team.id)):
        raise AuctionError(f"{team.name} already have {lot.name}. A squad may hold only one version of a player.")
    available = _available(session, season)
    actual_prices = [price_for(session, season, p) for p in available]
    season.min_base_price_lakh = min(actual_prices, default=0)
    A.validate_preauction_signing(session, season, team, lot, price)
    A.check_role_rules(session, season, team, lot)
    slots_after = _required_slots(session, season, team, signing_lot=lot)
    remaining = [p for p in available if p.id != lot.id]
    teams = A.franchises(session, season.id)
    _validate_distinct_names(session, season, draft, teams, remaining,
                             signing_team=team, signing_lot=lot)
    # Read the room's composition once; querying every team separately for
    # every category and rating ceiling adds network round trips to each pick.
    team_counts = {other.id: Counter() for other in teams}
    team_ratings = {other.id: [] for other in teams}
    for fid, category, rating in session.query(AuctionLot.sold_to_id, AuctionLot.category, AuctionLot.rating).filter(
            AuctionLot.season_id == season.id, AuctionLot.status == A.LOT_SOLD).all():
        if fid in team_counts:
            team_counts[fid][category] += 1
            if rating is not None:team_ratings[fid].append(int(rating))
    counts = team_counts[team.id].copy()
    counts[lot.category] = counts.get(lot.category, 0) + 1
    for role, minimum in A.role_minimums(season).items():
        needed = max(0, minimum - counts.get(role, 0))
        if needed > sum(p.category == role for p in remaining):
            raise AuctionError(f"This pick leaves too few {role} players available to meet the squad minimum.")
    ratings = team_ratings[team.id] + [int(lot.rating)]
    for rule in A.rating_rules(season):
        needed = max(0, rule["min_players"] - sum(r <= rule["max_rating"] for r in ratings))
        if needed > sum(int(p.rating) <= rule["max_rating"] for p in remaining):
            raise AuctionError(f"This pick leaves too few players rated ≤{rule['max_rating']} for the rating minimum.")
    # Protect the room's remaining obligations too. A team that already has
    # its keeper must not consume the only keeper another team still needs.
    role_pool = Counter(p.category for p in remaining)
    for role, minimum in A.role_minimums(season).items():
        total_needed = 0
        for other in teams:
            have = team_counts[other.id].get(role, 0)
            if other.id == team.id and lot.category == role:
                have += 1
            total_needed += max(0, minimum - have)
        if total_needed > role_pool[role]:
            raise AuctionError(f"This pick would leave too few {role} players for the remaining teams' role minimums.")
    for rule in A.rating_rules(season):
        cap = rule["max_rating"]
        total_needed = 0
        for other in teams:
            have = sum(r <= cap for r in team_ratings[other.id])
            if other.id == team.id and int(lot.rating) <= cap:
                have += 1
            total_needed += max(0, rule["min_players"] - have)
        if total_needed > sum(int(p.rating) <= cap for p in remaining):
            raise AuctionError(f"This pick would leave too few players rated ≤{cap} for the remaining teams' rating minimums.")
    reserve = completion_reserve(session,season,team,remaining,signing_lot=lot)
    if len(remaining) < slots_after or int(team.purse_remaining_lakh or 0) - price < reserve:
        raise AuctionError(f"This pick would leave too little purse to complete the squad. "
                           f"Hold back at least {_money(season, reserve)} for its remaining {slots_after} picks.")
    return season.min_base_price_lakh


def _required_slots(session, season, team, *, signing_lot=None, _signed=None):
    if int(season.min_squad_size) == int(season.max_squad_size):
        return max(0, int(season.min_squad_size) - int(team.squad_size or 0) - int(signing_lot is not None))
    signed = _signed if _signed is not None else list(A.squad(session, team.id)) + ([signing_lot] if signing_lot else [])
    counts = Counter(p.category for p in signed)
    return max([0, int(season.min_squad_size) - len(signed),
        sum(max(0,n-counts[role]) for role,n in A.role_minimums(season).items())] +
        [max(0,r['min_players']-sum(p.rating<=r['max_rating'] for p in signed)) for r in A.rating_rules(season)])


def completion_reserve(session,season,team,available,*,signing_lot=None):
    """Budget floor for mandatory roles/ratings; exact combined check at the end.

    Small endgames collapse equivalent cards to their cheapest distinct names,
    keeping the check bounded while considering category/rating intersections.
    """
    from itertools import combinations
    signed=list(A.squad(session,team.id))+([signing_lot] if signing_lot else [])
    slots=_draft(session,season).rounds-len(signed)
    mandatory=_required_slots(session,season,team,signing_lot=signing_lot,_signed=signed)
    names={p.name.strip().casefold() for p in signed}
    pool=[p for p in available if p.name.strip().casefold() not in names]
    costs={p.id:price_for(session,season,p) for p in pool}
    counts=Counter(p.category for p in signed)
    lows,highs=A.role_minimums(season),A.role_maximums(season)
    rules=A.rating_rules(season)
    owed={role:max(0,n-counts[role]) for role,n in lows.items()}
    needs=[max(0,r['min_players']-sum(p.rating<=r['max_rating'] for p in signed)) for r in rules]
    if slots<0 or sum(owed.values())>slots or any(n>slots for n in needs):
        raise AuctionError('The remaining slots cannot satisfy mandatory category/rating requirements.')
    if any(counts[role]>cap for role,cap in highs.items()):raise AuctionError('A category maximum is exceeded.')
    def cheapest(rows,n):
        distinct={}
        for p in rows:
            name=p.name.strip().casefold();price=costs[p.id]
            distinct[name]=min(distinct.get(name,price),price)
        prices=sorted(distinct.values())
        if len(prices)<n:raise AuctionError('Too few distinct eligible players remain for mandatory slots.')
        return sum(prices[:n])
    floor=cheapest(pool,mandatory)
    role_floor=sum(cheapest([p for p in pool if p.category==role],need) for role,need in owed.items())
    floor=max(floor,role_floor+cheapest(pool,max(0,mandatory-sum(owed.values()))))
    for rule,need in zip(rules,needs):
        floor=max(floor,cheapest([p for p in pool if p.rating<=rule['max_rating']],need)+cheapest(pool,max(0,mandatory-need)))
    if not slots:
        return 0
    floor=max(floor,_completion_flow(pool,slots,counts,owed,highs,rules,needs,
                                    lambda p:costs[p.id],min_slots=mandatory))
    if slots<=3:
        groups={}
        for p in sorted(pool,key=lambda p:(costs[p.id],p.id)):
            key=(p.category,tuple(p.rating<=r['max_rating'] for r in rules))
            group=groups.setdefault(key,[])
            if len(group)<slots and p.name.strip().casefold() not in {v.name.strip().casefold() for v in group}:group.append(p)
        candidates=[p for group in groups.values() for p in group]
        best=None
        from itertools import chain
        for index,choice in enumerate(chain.from_iterable(combinations(candidates,n) for n in range(mandatory,slots+1))):
            if index>100000:raise AuctionError('The final-slot rules need administrator review; simplify overlapping rating requirements before resuming.')
            if len({p.name.strip().casefold() for p in choice})!=len(choice):continue
            final=counts+Counter(p.category for p in choice)
            if any(final[role]<n for role,n in lows.items()) or any(final[role]>n for role,n in highs.items()):continue
            if any(sum(p.rating<=r['max_rating'] for p in choice)<n for r,n in zip(rules,needs)):continue
            price=sum(costs[p.id] for p in choice)
            best=price if best is None else min(best,price)
            if best==floor:break
        if best is None:raise AuctionError('No remaining player combination can meet the category and rating minimums together.')
        return best
    return floor


def _completion_flow(pool,slots,counts,owed,highs,rules,needs,price,*,distinct_names=True,min_slots=None):
    """Minimum-cost lower-bound circulation for roles and nested rating rules.

    Bucket-chain flow is the count rated at or below each threshold. This
    checks their intersections with role minima/maxima in polynomial time.
    Multiple editions in different buckets form an optimistic cost bound;
    the distinct-name checks and exact final-slot search enforce uniqueness.
    """
    from collections import deque
    ordered=sorted(zip(rules,needs),key=lambda pair:pair[0]['max_rating'])
    roles=list(A.SQUAD_ROLES)
    buckets=1+len(ordered)
    source=0;start=1+len(roles);sink=start+buckets;ss=sink+1;tt=ss+1
    graph=[[] for _ in range(tt+1)];balance=[0]*(tt+1)
    def edge(u,v,low,cap,cost=0):
        if cap<low:raise AuctionError('Category maximum leaves too few mandatory slots.')
        forward=[v,cap-low,cost,len(graph[v])]
        backward=[u,0,-cost,len(graph[u])]
        graph[u].append(forward);graph[v].append(backward)
        balance[u]-=low;balance[v]+=low
    for index,role in enumerate(roles,1):
        edge(source,index,owed.get(role,0),min(slots,highs.get(role,counts[role]+slots)-counts[role]))
    grouped={}
    for p in pool:
        if p.category not in roles:continue
        bucket=next((i for i,(r,_) in enumerate(ordered) if p.rating<=r['max_rating']),len(ordered))
        key=(p.category,bucket,p.name.strip().casefold() if distinct_names else p.player_id)
        value=price(p)
        grouped[key]=min(grouped.get(key,value),value)
    for (role,bucket,_),cost in grouped.items():edge(roles.index(role)+1,start+bucket,0,1,cost)
    for index,(_,minimum) in enumerate(ordered):edge(start+index,start+index+1,minimum,slots)
    edge(start+buckets-1,sink,slots if min_slots is None else min_slots,slots)
    edge(sink,source,slots if min_slots is None else 0,slots)
    required=0
    for node in range(sink+1):
        if balance[node]>0:edge(ss,node,0,balance[node]);required+=balance[node]
        elif balance[node]<0:edge(node,tt,0,-balance[node])
    flow=cost=0
    while flow<required:
        distance=[None]*len(graph);previous=[None]*len(graph)
        distance[ss]=0;queue=deque([ss]);queued={ss}
        while queue:
            u=queue.popleft();queued.discard(u)
            for index,item in enumerate(graph[u]):
                v,capacity,weight,_=item
                if capacity and (distance[v] is None or distance[v]>distance[u]+weight):
                    distance[v]=distance[u]+weight;previous[v]=(u,index)
                    if v not in queued:queued.add(v);queue.append(v)
        if previous[tt] is None:raise AuctionError('No remaining player combination can meet the category and rating minimums together.')
        amount=required-flow;v=tt
        while v!=ss:
            u,index=previous[v];amount=min(amount,graph[u][index][1]);v=u
        v=tt
        while v!=ss:
            u,index=previous[v];item=graph[u][index]
            item[1]-=amount;graph[v][item[3]][1]+=amount;v=u
        flow+=amount;cost+=amount*distance[tt]
    return int(cost)


def _pick_authority(session, team, actor, admin_reason, trusted_admin):
    if admin_reason is None:
        _require_owner(team, actor)
    else:
        from services.franchise_draft_admin import require_admin
        require_admin(session, actor, trusted_admin)
        if not str(admin_reason).strip() or len(str(admin_reason)) > 300:
            raise AuctionError('An administrator pick needs a reason of 1–300 characters.')
        if team is None:
            raise AuctionError('Every draft turn is complete.')


def preview_pick(session, season, actor_tg_id, query, *, now=None, _admin_reason=None, _trusted_admin=False):
    draft = _draft(session, season)
    if season.status != A.STATUS_LIVE:
        raise AuctionError("The draft is not live. Ask the organiser to start or resume it.")
    _check_time(draft, now)
    team = turn(session, season)
    if team is not None:
        session.refresh(team)
    _pick_authority(session, team, actor_tg_id, _admin_reason, _trusted_admin)
    lot = find_player(session, season, query, partial=True)
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


def confirm_pick(session, season, token, actor_tg_id, *, now=None, automatic=False, _admin_reason=None, _trusted_admin=False):
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
    _pick_authority(session, team, actor_tg_id, _admin_reason, _trusted_admin)
    if team.id != row.franchise_id:
        raise AuctionError("It is another team's turn. Use /dpick again when your team is called.")
    lot = session.get(AuctionLot, row.lot_id)
    if lot is None or price_for(session, season, lot) != row.price_lakh:
        raise AuctionError("The player's price changed. Use /dpick again to review the new price.")
    minimum_price = _validate_pick(session, season, team, lot, row.price_lakh)
    _check_time(draft,now)
    consumed = (session.query(FranchiseDraftConfirmation)
                .filter_by(token=row.token, status="pending")
                .update({"status": "confirmed"}, synchronize_session=False))
    if consumed != 1:
        raise AuctionError("This confirmation has already been answered or cancelled.")
    number = _next_number(session, FranchiseDraftPick, FranchiseDraftPick.overall_no, season)
    before = _turn_state(draft)
    _invariants(season,draft)
    # Audited draft prices can change while paused. The shared signing
    # validator must use the same current pool floor as the draft preview.
    season.min_base_price_lakh = minimum_price
    # _sign_before_auction reuses the exact existing lot snapshot; it needs
    # only the catalogue id because a missing lot was refused above.
    signed = A._sign_before_auction(session, season, team, SimpleNamespace(id=lot.player_id), row.price_lakh,
        kind=A.ACQ_DRAFTED, counter="draft_picks_used", ledger_kind=A.LEDGER_DRAFT,
        verb="draft", note=(f"Franchise draft pick {number}: {lot.name}" + (f" · Admin: {_admin_reason}" if _admin_reason else ''))[:200],
        event_kind="fd_picked", mark="🏏", word="draft", by_tg_id=None if automatic else actor_tg_id, quiet=True, now=now)
    draft = _draft(session, season)
    row.status = "confirmed"
    session.add(FranchiseDraftPick(season_id=season.id, overall_no=number,
        franchise_id=team.id, lot_id=signed.id, price_lakh=row.price_lakh, actor_tg_id=None if automatic else actor_tg_id))
    _record_turn(session, season, team, "pick", before, pick_no=number, actor=None if automatic else actor_tg_id, now=now)
    _advance_turn(session, season, info, now=now)
    session.flush()
    phase = "Skipped-pick round" if info['catch_up'] else f"Round {info['round_no']}"
    _event(session, season, "fd_picked", f"✅ <b>Signing confirmed</b> · {phase}\n"
        f"<b>{_e(team.name)}</b> sign <b>{_e(signed.name)}</b>\n"
        f"{signed.rating} OVR · {_e(signed.category)} · {_money(season, signed.sold_price_lakh)}\n"
        f"💰 Purse left: <b>{_money(season, team.purse_remaining_lakh)}</b>\n"
        f"👥 Squad: <b>{team.squad_size}/{draft.rounds}</b>\n\n" + next_team_html(session, season),
        by_tg_id=None if automatic else actor_tg_id, lot=signed, franchise=team,
        metadata={'round_no': info['round_no'], 'catch_up': info['catch_up'], 'automatic': automatic,
                  'admin_override': bool(_admin_reason), 'reason': _admin_reason})
    if season.status == A.STATUS_COMPLETED:
        from services.franchise_draft_admin import validate_final
        validate_final(session, season)
        _event(session, season, 'fd_completed', '🏆 <b>Franchise Draft completed</b>\nAll squads validated. Review /dsquad, /dpurse and /dhistory, then /dpublish to continue the season.')
    return signed


def _pass_full_teams(session, season):
    """Retentions occupy squad slots; saturated teams owe no further selections."""
    draft = _draft(session, season)
    teams = _ordered_teams(session, season)
    while teams and draft.cursor < draft.rounds*len(teams):
        current = round_order(session, season, draft.cursor//len(teams)+1)[draft.cursor%len(teams)]
        if current.squad_size < draft.rounds:
            break
        draft.cursor += 1
    queue = [entry for entry in skipped_turns(session, season)
             if session.get(AuctionFranchise, entry['franchise_id']).squad_size < draft.rounds]
    draft.skipped_turns_json = _dump(queue)


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
        if history.kind in ("skip", "timeout", "finish"):
            team = session.get(AuctionFranchise, history.franchise_id)
            draft.cursor = before['cursor']
            draft.skipped_turns_json = _dump(before['skipped'])
            draft.pick_deadline_at = None
            draft.paused_seconds_left = timer_seconds(draft) or None
            season.status = A.STATUS_PAUSED
            session.delete(history)
            _event(session, season, "fd_undo", f"↩️ <b>{'Optional draft places reopened' if history.kind == 'finish' else 'Last skipped turn restored'}</b>\n"
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
    draft.paused_seconds_left = timer_seconds(draft) or None
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
    if lot is not None:
        detail['player_snapshot']=dict(id=lot.player_id,pool_id=lot.id,name=lot.name,version=lot.version,
            rating=lot.rating,category=lot.category,country=lot.country,price_lakh=lot.sold_price_lakh)
    if franchise is not None:
        detail['franchise_snapshot']=dict(id=franchise.id,name=franchise.name,
            purse_lakh=franchise.purse_remaining_lakh,squad_size=franchise.squad_size)
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
