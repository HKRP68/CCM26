"""Validated catalogue and auction-pool CSV imports for a fresh auction bot."""
from dataclasses import dataclass
import csv
import io
import re

from sqlalchemy import or_

from models import AuctionLot, AuctionSeason, Player
from services import auction_service as A

MAX_BYTES = 2_000_000
MAX_ROWS = 2000
REQUIRED = {'name', 'rating', 'role', 'country'}
OPTIONAL = {'version', 'base_lakh', 'bat_rating', 'bowl_rating', 'overseas'}
_ROLE_NAMES = {
    'bat': 'Batsman', 'batsman': 'Batsman', 'batter': 'Batsman',
    'bowl': 'Bowler', 'bowler': 'Bowler',
    'ar': 'All-rounder', 'alr': 'All-rounder', 'allrounder': 'All-rounder',
    'wk': 'Wicket Keeper', 'keeper': 'Wicket Keeper', 'wicketkeeper': 'Wicket Keeper',
}


@dataclass(frozen=True)
class ImportRow:
    name: str
    rating: int
    role: str
    country: str
    version: str = 'Base'
    base_lakh: int | None = None
    bat_rating: int | None = None
    bowl_rating: int | None = None
    overseas: bool | None = None

    @property
    def key(self):
        return (self.name.casefold(), self.country.casefold(), self.version.casefold())


def _text(value, label, limit, line):
    value = (value or '').strip()
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise A.AuctionError(f'CSV row {line}: {label} needs 1–{limit} characters without control characters.')
    return value


def _integer(value, label, low, high, line, *, optional=False):
    value = (value or '').strip()
    if optional and not value:
        return None
    if not re.fullmatch(r'[0-9]+', value):
        raise A.AuctionError(f'CSV row {line}: {label} must be a whole number.')
    if len(value) > 10:
        raise A.AuctionError(f'CSV row {line}: {label} must be between {low} and {high}.')
    result = int(value)
    if not low <= result <= high:
        raise A.AuctionError(f'CSV row {line}: {label} must be between {low} and {high}.')
    return result


def parse_csv(data):
    """Parse the entire file before creating catalogue rows or changing a pool."""
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_BYTES:
        raise A.AuctionError('Send a non-empty CSV file under 2 MB.')
    try:
        source = bytes(data).decode('utf-8-sig')
    except UnicodeDecodeError:
        raise A.AuctionError('Save the CSV as UTF-8, then send it again.')
    if '\x00' in source:
        raise A.AuctionError('The CSV contains invalid control characters.')
    reader = csv.DictReader(io.StringIO(source, newline=''), strict=True)
    result, seen = [], {}
    try:
        headers = reader.fieldnames
        if (not headers or len(set(headers)) != len(headers)
                or not REQUIRED <= set(headers) or set(headers) - REQUIRED - OPTIONAL):
            raise A.AuctionError('CSV headers must include name,rating,role,country. Optional: version,base_lakh,bat_rating,bowl_rating,overseas.')
        for count, values in enumerate(reader, 1):
            if count > MAX_ROWS:
                raise A.AuctionError('Import at most 2,000 CSV rows at a time.')
            line = reader.line_num
            if None in values or any(value is None for value in values.values()):
                raise A.AuctionError(f'CSV row {line}: every row must have the same columns as the header.')
            role = _ROLE_NAMES.get(re.sub(r'[^a-z]', '', values['role'].lower()))
            if role is None:
                raise A.AuctionError(f'CSV row {line}: role must be BAT, BOWL, AR or WK.')
            overseas = values.get('overseas', '').strip().lower()
            if overseas not in ('', 'true', 'false', '1', '0', 'yes', 'no'):
                raise A.AuctionError(f'CSV row {line}: overseas is true or false.')
            row = ImportRow(
                name=_text(values['name'], 'name', 120, line),
                rating=_integer(values['rating'], 'rating', 1, 999, line), role=role,
                country=_text(values['country'], 'country', 60, line),
                version=_text(values.get('version') or 'Base', 'version', 50, line),
                base_lakh=_integer(values.get('base_lakh'), 'base_lakh (integer lakh)', 1, 1_000_000, line, optional=True),
                bat_rating=_integer(values.get('bat_rating'), 'bat_rating', 1, 999, line, optional=True),
                bowl_rating=_integer(values.get('bowl_rating'), 'bowl_rating', 1, 999, line, optional=True),
                overseas=None if not overseas else overseas in ('true', '1', 'yes'))
            prior = seen.get(row.key)
            if prior is not None and prior != row:
                raise A.AuctionError(f'CSV row {line}: the same name/country/version has conflicting definitions.')
            if prior is None:
                seen[row.key] = row
                result.append(row)
    except csv.Error:
        raise A.AuctionError('The CSV is malformed. Check quotes and column separators.')
    if not result:
        raise A.AuctionError('The CSV has no player rows.')
    return result


def import_players(db, season, rows, *, actor_tg_id, set_name='Imported'):
    """Atomically import validated players. The caller owns commit/rollback."""
    A.require_auction_manager(db, actor_tg_id)
    if (not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS
            or not all(isinstance(row, ImportRow) for row in rows)):
        raise A.AuctionError('Parse a valid player CSV before importing.')
    if (not isinstance(set_name, str) or not 1 <= len(set_name.strip()) <= 40
            or any(ord(c) < 32 for c in set_name)):
        raise A.AuctionError('Use a player set name of 1–40 characters.')
    # Serialize imports for this season before checking its phase and existing lots.
    current = db.query(AuctionSeason).filter_by(id=season.id).with_for_update().first()
    if current is None or current.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise A.AuctionError('Import players during setup or while the auction is paused.')
    db.refresh(current)
    if current.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise A.AuctionError('Import players during setup or while the auction is paused.')
    home = (current.home_country or '').strip().casefold()
    catalogue = {}
    requested = {row.key for row in rows}
    for player in db.query(Player).filter(or_(Player.is_career.is_(False), Player.is_career.is_(None))).all():
        key = ((player.name or '').strip().casefold(), (player.country or '').strip().casefold(), (player.version or 'Base').strip().casefold())
        if key in requested:
            if key in catalogue:
                raise A.AuctionError(f'The catalogue has multiple matches for {player.name}; resolve them before importing.')
            catalogue[key] = player
    lots = {lot.player_id: lot for lot in db.query(AuctionLot).filter_by(season_id=current.id).all()}
    for row in rows:
        if row.overseas is not None and row.overseas != bool(home and row.country.casefold() != home):
            raise A.AuctionError(f'{row.name}: overseas must match country and the auction home country.')
        player = catalogue.get(row.key)
        if player is None:
            continue
        if (not player.is_active or player.rating != row.rating or player.category != row.role
                or (row.bat_rating is not None and player.bat_rating != row.bat_rating)
                or (row.bowl_rating is not None and player.bowl_rating != row.bowl_rating)):
            raise A.AuctionError(f'{row.name} conflicts with the existing catalogue. This import never overwrites player ratings or roles.')
        lot = lots.get(player.id)
        if lot and row.base_lakh is not None and lot.base_price_lakh != row.base_lakh and lot.status != A.LOT_QUEUED:
            raise A.AuctionError(f'{row.name} has left the queue; its base price cannot be changed.')
    created = []
    players = []
    for row in rows:
        player = catalogue.get(row.key)
        if player is None:
            player = Player(name=row.name, country=row.country, version=row.version, rating=row.rating,
                            category=row.role, bat_hand='Right', bowl_hand='Right', bowl_style='Medium Pacer',
                            bat_rating=row.bat_rating if row.bat_rating is not None else row.rating,
                            bowl_rating=row.bowl_rating if row.bowl_rating is not None else row.rating,
                            is_active=True)
            db.add(player)
            catalogue[row.key] = player
            created.append(player)
        players.append(player)
    db.flush()
    added, skipped = A.add_players_to_pool(db, current, players, set_name=set_name.strip())
    pool = {lot.player_id: lot for lot in db.query(AuctionLot).filter_by(season_id=current.id).all()}
    prices_changed = 0
    for row, player in zip(rows, players):
        lot = pool[player.id]
        if row.base_lakh is not None and lot.base_price_lakh != row.base_lakh:
            if lot.status != A.LOT_QUEUED:
                raise A.AuctionError(f'{row.name} has left the queue; refresh before importing.')
            # All values/statuses are validated before mutation. Batch queued-only
            # prices, then use the shared reserve-floor restamp once rather than
            # 2,000 individual price/aggregate SQL round trips.
            lot.base_price_lakh = row.base_lakh
            prices_changed += 1
    db.flush()
    if prices_changed:
        A._restamp_price_floor(db, current)
    A.log_event(db, current, 'players_imported',
                f'📥 Imported CSV: {added} players added, {skipped} already in pool.',
                by_tg_id=actor_tg_id, by_admin=True,
                detail={'added':added, 'skipped':skipped, 'catalogue_created':len(created), 'prices_changed':prices_changed})
    return {'added':added, 'skipped':skipped, 'catalogue_created':len(created), 'prices_changed':prices_changed}
