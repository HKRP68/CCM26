"""Download every auction set as a file, and load one back.

A pool is built set by set — Marquee, 90+ OVR, the capped Indian batters —
and building a good one by hand is an evening's work an admin should not
have to repeat for the next season, or redo because a different group is
running the same pool. So the whole thing goes out as a file, in running
order, and comes back in:

* **JSON** — ``{"format": "franchise-auction/sets", "version": 1, "sets":
  [{"name", "set_no", "players": [{"player_id", "name", "version", "rating",
  "category", "base_price_lakh", "status"}]}]}``.
* **CSV** — one row per player: ``set, set_no, player_id, name, version,
  rating, category, base_price_lakh, status``. The spreadsheet shape, for an
  admin who wants to shuffle a pool around in Sheets.

Players are matched by ``player_id`` first, then by exact name and edition —
never by a best guess, because the wrong card in the pool costs somebody real
money. Whatever cannot be matched is reported by name, not dropped silently.

Only QUEUED players move. A player the file lists who is already sold,
retained or drafted here stays exactly where he is: an import rearranges the
pool, it does not undo anything that has happened in it.
"""

import csv
import io
import json
from datetime import datetime

from models import AuctionLot, Player
from services import auction_service as A
from services.auction_service import AuctionError

SETS_FILE_FORMAT = "franchise-auction/sets"
SETS_FILE_VERSION = 1
CSV_FIELDS = ("set", "set_no", "player_id", "name", "version", "rating",
              "category", "base_price_lakh", "status")
MAX_FILE_BYTES = 5_000_000


def _set_rows(session, season):
    """``[(set entry, [lots in queue order])]`` in the order the room meets them."""
    by_set = {}
    for lot in sorted(A.lots(session, season.id), key=lambda l: l.lot_no or 0):
        by_set.setdefault(A.set_label(lot), []).append(lot)
    return [(entry, by_set.get(entry["name"], []))
            for entry in A.list_sets(session, season)]


def export_sets(session, season):
    """The whole pool as a dict, ready for ``json.dumps``."""
    sets = []
    for entry, lots in _set_rows(session, season):
        sets.append({
            "name": entry["name"],
            "set_no": entry["set_no"],
            "players": [{
                "player_id": lot.player_id,
                "name": lot.name,
                "version": lot.version or "Base",
                "rating": lot.rating,
                "category": lot.category,
                "base_price_lakh": int(lot.base_price_lakh or 0),
                "status": lot.status,
            } for lot in lots],
        })
    return {
        "format": SETS_FILE_FORMAT,
        "version": SETS_FILE_VERSION,
        "season": season.name,
        "currency_label": season.currency_label or "₹",
        "exported_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "sets": sets,
    }


def export_sets_json(session, season):
    return json.dumps(export_sets(session, season), ensure_ascii=False,
                      indent=2).encode("utf-8")


def export_sets_csv(session, season):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_FIELDS)
    for entry in export_sets(session, season)["sets"]:
        for player in entry["players"]:
            writer.writerow([entry["name"], entry["set_no"],
                             player["player_id"], player["name"],
                             player["version"], player["rating"],
                             player["category"], player["base_price_lakh"],
                             player["status"]])
    # A BOM so Excel opens the non-ASCII names as UTF-8 rather than mojibake.
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def file_name(season, fmt):
    import re
    stamp = datetime.utcnow().strftime("%Y%m%d")
    slug = re.sub(r"[^A-Za-z0-9]+", "-",
                  (season.name or "auction")).strip("-").lower() or "auction"
    return f"{slug}-sets-{stamp}.{fmt}"


# ──────────────────────────────────────────────────────────────────────
# Reading a file
# ──────────────────────────────────────────────────────────────────────

def parse_sets_file(blob, fmt=None):
    """``[(set name, [player dict])]`` from the bytes somebody uploaded.

    ``fmt`` is ``json`` or ``csv``; left off, it is sniffed — a file that
    starts with ``{`` or ``[`` is JSON.
    """
    if blob is None:
        raise AuctionError("That file is empty.")
    if len(blob) > MAX_FILE_BYTES:
        raise AuctionError("That file is over 5 MB — a sets file is far "
                           "smaller, so this is almost certainly the wrong one.")
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AuctionError("That file is not UTF-8 text. Download one with "
                           "/asetsexport to see the shape an import expects.")
    if not text.strip():
        raise AuctionError("That file is empty.")
    fmt = (fmt or "").lower().lstrip(".")
    if fmt not in ("json", "csv"):
        fmt = "json" if text.lstrip()[:1] in "{[" else "csv"
    return _parse_json(text) if fmt == "json" else _parse_csv(text)


def _parse_json(text):
    try:
        payload = json.loads(text)
    except ValueError as bad:
        raise AuctionError(f"That is not valid JSON — {bad}.")
    sets = payload.get("sets") if isinstance(payload, dict) else payload
    if not isinstance(sets, list) or not sets:
        raise AuctionError("That file has no “sets” list. Download one with "
                           "/asetsexport to see the shape an import expects.")
    out = []
    for index, entry in enumerate(sets, start=1):
        if not isinstance(entry, dict):
            raise AuctionError(f"Set {index} in that file is not an object.")
        name = str(entry.get("name") or "").strip()[:40] or A.DEFAULT_SET
        players = entry.get("players") or []
        if not isinstance(players, list):
            raise AuctionError(f"Set “{name}” has no players list.")
        out.append((name, [p for p in players if isinstance(p, dict)]))
    return out


def _parse_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    fields = [f.strip().lower() for f in (reader.fieldnames or [])]
    if "set" not in fields or not ({"player_id", "name"} & set(fields)):
        raise AuctionError("A sets CSV needs a “set” column and a "
                           "“player_id” or “name” column. Download one with "
                           "/asetsexport to see the shape.")
    order, grouped = [], {}
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip()
               for k, v in row.items()}
        if not (row.get("player_id") or row.get("name")):
            continue
        name = row.get("set", "")[:40] or A.DEFAULT_SET
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(row)
    if not order:
        raise AuctionError("That CSV lists no players.")
    return [(name, grouped[name]) for name in order]


# ──────────────────────────────────────────────────────────────────────
# Writing it into a season
# ──────────────────────────────────────────────────────────────────────

def _match(session, row, by_id_cache):
    """The catalogue card a file row means, or ``(None, reason)``."""
    raw_id = A._as_int(row.get("player_id"), 0)
    name = str(row.get("name") or "").strip()
    version = str(row.get("version") or "").strip()
    if raw_id:
        player = by_id_cache.get(raw_id)
        if player is None:
            player = session.query(Player).filter(Player.id == raw_id).first()
            by_id_cache[raw_id] = player
        # The id is trusted only when the name agrees (or there is none): a
        # file from another deployment has other ids, and a matching id with
        # another man's name on it is exactly the silent mis-assignment this
        # has to refuse.
        if player is not None and (not name
                                   or (player.name or "").lower() == name.lower()):
            return player, None
    if not name:
        return None, f"player #{raw_id} is not in the catalogue"
    query = session.query(Player).filter(Player.name.ilike(name))
    rows = [p for p in query.all() if (p.name or "").lower() == name.lower()]
    active = [p for p in rows if getattr(p, "is_active", True)] or rows
    if version:
        active = [p for p in active
                  if (p.version or "Base").lower() == version.lower()]
    if len(active) == 1:
        return active[0], None
    if not active:
        return None, f"{name}{f' ({version})' if version else ''} is not in the catalogue"
    return None, f"{name} matches {len(active)} cards — add the player_id"


def import_sets(session, season, groups, *, replace=False):
    """Load ``[(set name, [player row])]`` into this season's pool.

    Returns a dict: ``added``, ``moved`` (already queued, filed under the
    file's set), ``kept`` (sold / retained / drafted here, left alone),
    ``removed`` (``replace`` only) and ``unmatched`` (reasons, by name).
    """
    if season.status not in (A.STATUS_SETUP, A.STATUS_PAUSED):
        raise AuctionError("Pause the auction before changing its pool.")
    existing = {lot.player_id: lot for lot in A.lots(session, season.id)
                if lot.player_id is not None}
    cache = {}
    added = moved = kept = 0
    unmatched = []
    front, seen = [], set()

    for set_name, rows in groups:
        set_name = (set_name or "").strip()[:40] or A.DEFAULT_SET
        label = None if set_name == A.DEFAULT_SET else set_name
        for row in rows:
            player, reason = _match(session, row, cache)
            if player is None:
                unmatched.append(reason)
                continue
            if player.id in seen:
                continue
            seen.add(player.id)
            lot = existing.get(player.id)
            if lot is None:
                A.add_players_to_pool(session, season, [player], set_name=label)
                lot = (session.query(AuctionLot)
                       .filter(AuctionLot.season_id == season.id,
                               AuctionLot.player_id == player.id).first())
                existing[player.id] = lot
                added += 1
            elif lot.status == A.LOT_QUEUED:
                lot.set_name = label
                moved += 1
            else:
                kept += 1
                continue
            price = A._as_int(row.get("base_price_lakh"), 0)
            if price > 0:
                lot.base_price_lakh = price
            front.append(lot)

    removed = 0
    if replace:
        for lot in A.queued_lots(session, season):
            if lot.player_id not in seen:
                A.remove_lot(session, season, lot)
                removed += 1
    session.flush()
    A._restamp_price_floor(session, season)
    live = [lot for lot in front if lot.status == A.LOT_QUEUED]
    if live:
        A._requeue(session, season, live)
    return {"added": added, "moved": moved, "kept": kept, "removed": removed,
            "unmatched": unmatched}


def summary_text(result):
    parts = [f"➕ {result['added']} added", f"🔀 {result['moved']} re-filed"]
    if result["kept"]:
        parts.append(f"🔒 {result['kept']} already signed, left alone")
    if result["removed"]:
        parts.append(f"🗑 {result['removed']} removed")
    text = " · ".join(parts)
    if result["unmatched"]:
        shown = result["unmatched"][:10]
        text += (f"\n⚠️ {len(result['unmatched'])} not matched: "
                 + "; ".join(shown)
                 + (" …" if len(result["unmatched"]) > 10 else ""))
    return text
