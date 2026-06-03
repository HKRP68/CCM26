"""Fantasy league service — admin-controlled weekly fantasy cricket competition."""

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

CAPTAIN_MULTIPLIER = 2.0
VC_MULTIPLIER = 1.5
SQUAD_SIZE = 11


def get_active_league(session):
    """Return the most recent open or locked league, or None."""
    from models import FantasyLeague
    return (session.query(FantasyLeague)
            .filter(FantasyLeague.status.in_(["open", "locked"]))
            .order_by(FantasyLeague.id.desc())
            .first())


def is_locked(league):
    """True if squads for this league can no longer be created or edited.

    A league is locked when its status is locked/scored, OR when an auto-lock
    time (``lock_at``, stored UTC) has been set and has now passed. This makes
    the lock effective immediately at request time even if the background
    auto-lock job has not run yet.
    """
    if league is None:
        return True
    if league.status in ("locked", "scored"):
        return True
    lock_at = getattr(league, "lock_at", None)
    if lock_at and datetime.utcnow() >= lock_at:
        return True
    return False


def fantasy_deep_link(league_id=None):
    """Build a t.me deep link that opens the Mini App into the fantasy picker.

    Web App inline buttons only work in private chats, so groups, DMs and
    broadcasts must use a t.me deep link instead. The Mini App reads the
    ``startapp`` start_param (``fantasy`` or ``fantasy_<leagueId>``) and routes
    to the squad picker. Returns None when BOT_USERNAME is not configured.
    """
    bot_username = (os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")
    miniapp_name = (os.getenv("MINIAPP_NAME", "") or "").strip()
    if not bot_username:
        return None
    param = f"fantasy_{league_id}" if league_id else "fantasy"
    if miniapp_name:
        return f"https://t.me/{bot_username}/{miniapp_name}?startapp={param}"
    return f"https://t.me/{bot_username}?startapp={param}"


def apply_auto_locks(session):
    """Lock any open leagues whose ``lock_at`` time has passed.

    Returns a list of ``(league_name, chat_ids, broadcast_message)`` for each
    league that was newly locked, so the caller can broadcast the lock notice.
    The session is flushed but NOT committed here — the caller commits.
    """
    from models import FantasyLeague
    now = datetime.utcnow()
    due = (session.query(FantasyLeague)
           .filter(FantasyLeague.status == "open",
                   FantasyLeague.lock_at.isnot(None),
                   FantasyLeague.lock_at <= now)
           .all())
    results = []
    for lg in due:
        name = lg.name
        chat_ids, msg = lock_league(session, lg.id)
        results.append((name, chat_ids, msg))
    return results


def get_league(session, league_id):
    from models import FantasyLeague
    return session.query(FantasyLeague).get(league_id)


def get_or_create_entry(session, user_id, league_id):
    """Get or create a FantasyEntry for this user+league."""
    from models import FantasyEntry
    entry = (session.query(FantasyEntry)
             .filter_by(user_id=user_id, league_id=league_id).first())
    if not entry:
        entry = FantasyEntry(user_id=user_id, league_id=league_id)
        session.add(entry)
        session.flush()
    return entry


def set_picks(session, entry_id, picks, bypass_lock=False):
    """Save a user's 11-player fantasy squad.

    picks: list of {"player_id": int, "role": str}
    role must be one of: captain | vc | player
    Returns (ok: bool, message: str).
    """
    from models import FantasyEntry, FantasyPick, Player, FantasyLeague
    entry = session.query(FantasyEntry).get(entry_id)
    if not entry:
        return False, "Entry not found."

    if not bypass_lock and entry.locked:
        return False, "Squads are locked. Ask admin to make changes."

    league = session.query(FantasyLeague).get(entry.league_id)
    if not league:
        return False, "League not found."
    if not bypass_lock and is_locked(league):
        return False, "This league is locked. No changes allowed."

    if len(picks) != SQUAD_SIZE:
        return False, f"Select exactly {SQUAD_SIZE} players (got {len(picks)})."

    roles = [p.get("role", "player") for p in picks]
    if roles.count("captain") != 1:
        return False, "Exactly 1 captain required."
    if roles.count("vc") != 1:
        return False, "Exactly 1 vice-captain required."

    player_ids = [p["player_id"] for p in picks]
    if len(set(player_ids)) != SQUAD_SIZE:
        return False, "Duplicate players in squad."

    # Verify all players exist
    valid_count = session.query(Player).filter(Player.id.in_(player_ids)).count()
    if valid_count != SQUAD_SIZE:
        return False, "One or more players not found."

    # Delete existing picks and replace
    session.query(FantasyPick).filter_by(entry_id=entry_id).delete()

    # Compute existing points per player (from already-scored matches)
    player_points = _compute_pick_points(session, entry.league_id, player_ids)

    for pick_data in picks:
        role = pick_data.get("role", "player")
        pid = pick_data["player_id"]
        mult = CAPTAIN_MULTIPLIER if role == "captain" else (VC_MULTIPLIER if role == "vc" else 1.0)
        fp = FantasyPick(
            entry_id=entry_id,
            player_id=pid,
            role=role,
            total_points=round(player_points.get(pid, 0.0) * mult, 2),
        )
        session.add(fp)

    session.flush()
    # Recompute entry total
    _refresh_entry_total(session, entry)
    return True, f"Squad saved! {SQUAD_SIZE} players locked in."


def _compute_pick_points(session, league_id, player_ids):
    """Return {player_id: raw_points} from all scored matches in this league."""
    from models import FantasyMatch, FantasyPlayerScore
    from sqlalchemy import func
    result = (
        session.query(FantasyPlayerScore.player_id,
                      func.sum(FantasyPlayerScore.points))
        .join(FantasyMatch, FantasyPlayerScore.fantasy_match_id == FantasyMatch.id)
        .filter(FantasyMatch.league_id == league_id,
                FantasyPlayerScore.player_id.in_(player_ids))
        .group_by(FantasyPlayerScore.player_id)
        .all()
    )
    return {pid: pts for pid, pts in result}


def _refresh_entry_total(session, entry):
    """Recompute entry.total_points from its FantasyPick rows."""
    from models import FantasyPick
    from sqlalchemy import func
    total = (session.query(func.sum(FantasyPick.total_points))
             .filter_by(entry_id=entry.id).scalar()) or 0.0
    entry.total_points = round(total, 2)


def save_player_scores(session, fantasy_match_id, scores):
    """Admin saves points for players in a real-world match.

    scores: list of {"player_id": int, "points": float, "notes": str}
    Upserts FantasyPlayerScore rows and cascades points to all pickers.
    """
    from models import FantasyPlayerScore, FantasyMatch, FantasyPick, FantasyEntry

    fmatch = session.query(FantasyMatch).get(fantasy_match_id)
    if not fmatch:
        return False, "Fantasy match not found."

    for score_data in scores:
        pid = score_data["player_id"]
        pts = float(score_data.get("points", 0.0))
        notes = score_data.get("notes", "") or ""

        existing = (session.query(FantasyPlayerScore)
                    .filter_by(fantasy_match_id=fantasy_match_id, player_id=pid).first())
        old_pts = existing.points if existing else 0.0

        if existing:
            existing.points = pts
            existing.notes = notes
        else:
            session.add(FantasyPlayerScore(
                fantasy_match_id=fantasy_match_id,
                player_id=pid,
                points=pts,
                notes=notes,
            ))

        delta = pts - old_pts
        if delta == 0:
            continue

        # Cascade delta to all picks of this player in this league
        picks = (session.query(FantasyPick)
                 .join(FantasyEntry, FantasyPick.entry_id == FantasyEntry.id)
                 .filter(FantasyPick.player_id == pid,
                         FantasyEntry.league_id == fmatch.league_id)
                 .all())
        for pick in picks:
            mult = (CAPTAIN_MULTIPLIER if pick.role == "captain"
                    else VC_MULTIPLIER if pick.role == "vc" else 1.0)
            pick.total_points = round(pick.total_points + delta * mult, 2)
            entry = session.query(FantasyEntry).get(pick.entry_id)
            if entry:
                entry.total_points = round(entry.total_points + delta * mult, 2)

    session.flush()
    return True, "Scores saved and points cascaded."


def lock_league(session, league_id):
    """Lock the league: freeze all entries, return group chat IDs for broadcast."""
    from models import FantasyLeague, FantasyEntry, BotChat
    league = session.query(FantasyLeague).get(league_id)
    if not league:
        return [], "League not found."
    league.status = "locked"
    session.query(FantasyEntry).filter_by(league_id=league_id).update({"locked": True})
    session.flush()

    chats = (session.query(BotChat)
             .filter(BotChat.is_active == True,
                     BotChat.chat_type.in_(["group", "supergroup"]))
             .all())
    chat_ids = [c.chat_id for c in chats]
    return chat_ids, f"🔒 Fantasy squads for **{league.name}** are now LOCKED! No further changes allowed. Good luck! 🍀"


def score_league(session, league_id):
    """Recalculate all entry totals from scratch and assign ranks."""
    from models import FantasyEntry, FantasyPick
    entries = session.query(FantasyEntry).filter_by(league_id=league_id).all()
    for entry in entries:
        _refresh_entry_total(session, entry)

    sorted_entries = sorted(entries, key=lambda e: e.total_points, reverse=True)
    for rank, entry in enumerate(sorted_entries, 1):
        entry.rank = rank

    league = session.query(__import__("models", fromlist=["FantasyLeague"]).FantasyLeague).get(league_id)
    if league:
        league.status = "scored"
    session.flush()
    return True, f"Scored {len(entries)} entries."


def get_leaderboard(session, league_id, page=1, per_page=10):
    """Return paginated leaderboard rows."""
    from models import FantasyEntry, User
    q = (session.query(FantasyEntry, User)
         .join(User, FantasyEntry.user_id == User.id)
         .filter(FantasyEntry.league_id == league_id)
         .order_by(FantasyEntry.total_points.desc()))
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    result = []
    for idx, (entry, user) in enumerate(rows, start=(page - 1) * per_page + 1):
        result.append({
            "rank": idx,
            "username": user.username or user.first_name or f"User#{user.telegram_id}",
            "team_name": user.team_name or "",
            "total_points": entry.total_points,
            "entry_id": entry.id,
        })
    return result, total


def get_player_ownership(session, league_id):
    """Return players sorted by how many users own them in this league."""
    from models import FantasyPick, FantasyEntry, Player
    from sqlalchemy import func
    rows = (session.query(Player.id, Player.name, Player.country, Player.category,
                          func.count(FantasyPick.id).label("owned_by"),
                          func.sum(FantasyPick.total_points).label("total_pts"))
            .join(FantasyPick, FantasyPick.player_id == Player.id)
            .join(FantasyEntry, FantasyPick.entry_id == FantasyEntry.id)
            .filter(FantasyEntry.league_id == league_id)
            .group_by(Player.id, Player.name, Player.country, Player.category)
            .order_by(func.count(FantasyPick.id).desc())
            .all())
    return [
        {"player_id": r.id, "name": r.name, "country": r.country,
         "category": r.category, "owned_by": r.owned_by,
         "total_pts": round(r.total_pts or 0, 2)}
        for r in rows
    ]


def get_top_scorers(session, league_id, n=10):
    """Return top n players by total fantasy points earned across all matches in league."""
    from models import FantasyPlayerScore, FantasyMatch, Player
    from sqlalchemy import func
    rows = (session.query(Player.id, Player.name, Player.country, Player.category,
                          func.sum(FantasyPlayerScore.points).label("total"))
            .join(FantasyPlayerScore, FantasyPlayerScore.player_id == Player.id)
            .join(FantasyMatch, FantasyPlayerScore.fantasy_match_id == FantasyMatch.id)
            .filter(FantasyMatch.league_id == league_id)
            .group_by(Player.id, Player.name, Player.country, Player.category)
            .order_by(func.sum(FantasyPlayerScore.points).desc())
            .limit(n).all())
    return [{"player_id": r.id, "name": r.name, "country": r.country,
             "category": r.category, "total_points": round(r.total or 0, 2)}
            for r in rows]


def get_user_team_info(session, user_id, league_id):
    """Return a user's squad with per-player points and overall rank."""
    from models import FantasyEntry, FantasyPick, Player
    entry = (session.query(FantasyEntry)
             .filter_by(user_id=user_id, league_id=league_id).first())
    if not entry:
        return None
    picks = (session.query(FantasyPick, Player)
             .join(Player, FantasyPick.player_id == Player.id)
             .filter(FantasyPick.entry_id == entry.id)
             .all())
    squad = [
        {"player_id": pl.id, "name": pl.name, "country": pl.country,
         "category": pl.category, "role": fp.role, "points": fp.total_points}
        for fp, pl in picks
    ]
    return {
        "entry_id": entry.id,
        "total_points": entry.total_points,
        "rank": entry.rank,
        "locked": entry.locked,
        "squad": squad,
    }

def get_group_chat_ids(session):
    """Return active group/supergroup chat IDs for fantasy broadcasts."""
    from models import BotChat
    chats = (session.query(BotChat)
             .filter(BotChat.is_active == True,
                     BotChat.chat_type.in_(["group", "supergroup"]))
             .all())
    return [c.chat_id for c in chats]


def build_broadcast_message(league):
    link = fantasy_deep_link(getattr(league, "id", None))
    return (f"🏏 <b>{league.name}</b> fantasy is live!\n\n"
            f"Pick your XI from the Fantasy Mini App before squads lock.\n"
            f"👉 {link}")


def generate_broadcast_image(league):
    """Create a lightweight fantasy broadcast PNG in memory."""
    import io
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        # Last-resort valid PNG so the broadcast route still sends an image in
        # minimal environments. Production requirements include Pillow.
        import struct, zlib
        w, h = 2, 2
        raw = b"".join(b"\x00" + bytes([8, 98, 65]) * w for _ in range(h))
        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data +
                    struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
        return (b"\x89PNG\r\n\x1a\n" +
                chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    w, h = 1280, 720
    img = Image.new("RGB", (w, h), (8, 18, 28))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        color = (int(8 + 8 * t), int(18 + 80 * t), int(28 + 35 * t))
        draw.line([(0, y), (w, y)], fill=color)
    draw.rounded_rectangle([70, 70, w - 70, h - 70], radius=36,
                           outline=(0, 201, 167), width=5)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 76)
        body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except Exception:
        title_font = body_font = small_font = ImageFont.load_default()
    draw.text((110, 130), "FANTASY CRICKET", fill=(0, 201, 167), font=body_font)
    name = str(getattr(league, "name", "Fantasy League") or "Fantasy League")[:34]
    draw.text((110, 220), name, fill=(255, 255, 255), font=title_font)
    draw.text((110, 360), "Build your XI • Captain • Vice Captain", fill=(232, 245, 241), font=body_font)
    draw.text((110, 465), "Tap the button/message link to open Fantasy", fill=(190, 210, 210), font=small_font)
    draw.ellipse([930, 165, 1150, 385], fill=(0, 201, 167), outline=(255, 255, 255), width=4)
    draw.text((985, 225), "XI", fill=(8, 18, 28), font=title_font)
    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    return buf.getvalue()
