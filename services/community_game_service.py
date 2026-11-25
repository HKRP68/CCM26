"""Community mini-games — generation, persistence, winner selection, scoring.

Six games (Mystery Card, Odd One Out, Cricket Wordle, Fastest Fingers, Guess
the Rating, Guess the Cricketer), all built from the real CCM26 player
database. This module owns:

* **generation** — building each round from live player data, including the
  uniqueness guarantees the spec requires;
* **persistence** — every game is a :class:`models.CommunityGame` row so the
  scheduler can recover / expire it across restarts, with a unique
  ``launch_key`` and a single-active-game-per-chat rule;
* **winner selection** — an atomic, row-locked first-correct-answer decision
  that cannot produce two winners or a duplicate reward;
* **result recording** — stats, achievements and the capped XP award.

Telegram sending lives in ``handlers/community_games.py``; this module stays
transport-agnostic so it is unit-testable without a bot.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import datetime, timedelta
from io import BytesIO

logger = logging.getLogger(__name__)

GAME_TYPES = ("mystery", "oddoneout", "wordle", "fastestfingers", "rating", "cricketer")

GAME_LABEL = {
    "mystery": "Mystery Card",
    "oddoneout": "Odd One Out",
    "wordle": "Cricket Wordle",
    "fastestfingers": "Fastest Fingers",
    "rating": "Guess the Rating",
    "cricketer": "Guess the Cricketer",
}

# ── In-memory active-game cache (chat_id set) ───────────────────────────
# Lets the chat-XP path and the answer router avoid a DB hit per message.
_ACTIVE_CHATS: set = set()


def has_active_game(chat_id) -> bool:
    return chat_id in _ACTIVE_CHATS


def mark_active(chat_id):
    _ACTIVE_CHATS.add(chat_id)


def mark_inactive(chat_id):
    _ACTIVE_CHATS.discard(chat_id)


def refresh_active_cache(session):
    """Rebuild the in-memory active-chat set from the database."""
    from models import CommunityGame
    rows = (session.query(CommunityGame.chat_id)
            .filter(CommunityGame.status == "active").all())
    _ACTIVE_CHATS.clear()
    for (cid,) in rows:
        _ACTIVE_CHATS.add(cid)


# ── Player pool ──────────────────────────────────────────────────────────

def _eligible_player_query(session, cfg):
    from models import Player
    from services.player_service import not_career
    q = session.query(Player).filter(Player.is_active.is_(True))
    elig = cfg.get("eligible", {})
    if elig.get("exclude_career", True):
        q = not_career(q)
    rmin = int(elig.get("rating_min", 0) or 0)
    rmax = int(elig.get("rating_max", 100) or 100)
    if rmin > 0:
        q = q.filter(Player.rating >= rmin)
    if rmax < 100:
        q = q.filter(Player.rating <= rmax)
    countries = [c for c in (elig.get("countries") or []) if c]
    if countries:
        q = q.filter(Player.country.in_(countries))
    categories = [c for c in (elig.get("categories") or []) if c]
    if categories:
        q = q.filter(Player.category.in_(categories))
    versions = [v for v in (elig.get("versions") or []) if v]
    if versions:
        q = q.filter(Player.version.in_(versions))
    return q


def eligible_players(session, cfg, *, exclude_ids=None):
    rows = _eligible_player_query(session, cfg).all()
    if exclude_ids:
        ex = set(exclude_ids)
        rows = [p for p in rows if p.id not in ex]
    return rows


# ── Card imaging ─────────────────────────────────────────────────────────

def card_bytes(player):
    """Full generated card PNG bytes, or None."""
    try:
        from services.card_generator import generate_card
        return generate_card(player)
    except Exception:
        logger.exception("card render failed for player %s", getattr(player, "id", "?"))
        return None


def blur_card_bytes(raw: bytes, radius: int = 16):
    """Return a heavily blurred copy of a card image (name/photo/metadata all
    become unreadable). Aspect ratio preserved; the original is never modified."""
    if not raw:
        return None
    try:
        from PIL import Image, ImageFilter
        img = Image.open(BytesIO(raw)).convert("RGB")
        blurred = img.filter(ImageFilter.GaussianBlur(max(1, int(radius))))
        out = BytesIO()
        blurred.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.exception("card blur failed")
        return None


# ── Cricket Wordle scoring (two-pass, repeated letters correct) ─────────

def wordle_score(guess: str, answer: str) -> list:
    """Return per-letter feedback: 'g' (green), 'y' (yellow), 'b' (black).

    Two-pass algorithm so repeated letters are scored correctly: greens are
    assigned first and consume answer letters; yellows are then assigned only
    from the letters that remain.
    """
    guess = (guess or "").lower()
    answer = (answer or "").lower()
    n = len(answer)
    result = ["b"] * len(guess)
    # Pass 1: greens, and tally the answer letters they did NOT consume.
    remaining = {}
    for i in range(n):
        if i < len(guess) and guess[i] == answer[i]:
            result[i] = "g"
        else:
            remaining[answer[i]] = remaining.get(answer[i], 0) + 1
    # Pass 2: yellows from whatever answer letters are left.
    for i in range(len(guess)):
        if result[i] == "g":
            continue
        ch = guess[i]
        if remaining.get(ch, 0) > 0:
            result[i] = "y"
            remaining[ch] -= 1
    return result


_WORDLE_EMOJI = {"g": "🟩", "y": "🟨", "b": "⬛"}


def wordle_render(guess: str, answer: str) -> str:
    return "".join(_WORDLE_EMOJI[c] for c in wordle_score(guess, answer))


# ── Generators ─────────────────────────────────────────────────────────

def _player_meta(p) -> dict:
    return {
        "id": p.id, "name": p.name, "version": p.version,
        "rating": p.rating, "country": p.country, "category": p.category,
        "bat_hand": p.bat_hand, "bowl_hand": p.bowl_hand,
        "bowl_style": p.bowl_style, "bat_rating": p.bat_rating,
        "bowl_rating": p.bowl_rating,
    }


def _rating_band(r, size=5):
    r = int(r or 0)
    lo = (r // size) * size
    return f"{lo}-{lo + size - 1}"


def _accept_for_player(p, pool, *, allow_surname=True):
    """Build a safe accept list for a player-name answer.

    Includes the full name and dot-free variants always; adds the bare surname
    and 'first surname' only when they are unambiguous within ``pool``.
    """
    from services import community_answer_service as A
    accept = set(A.name_variants(p.name))
    if allow_surname:
        sn = A.surname(p.name)
        if sn and len(sn) >= 3:
            clashes = sum(1 for q in pool if A.surname(q.name) == sn)
            if clashes <= 1:
                accept.add(sn)
                # "V Kohli" style
                fn = A.first_name(p.name)
                if fn:
                    accept.add(f"{fn[0]} {sn}")
    return sorted(accept)


def generate_mystery(session, cfg, exclude_ids=None):
    pool = eligible_players(session, cfg, exclude_ids=exclude_ids)
    pool = [p for p in pool if p.name]
    if not pool:
        return None
    p = random.choice(pool)
    gcfg = cfg.get("games", {}).get("mystery", {})
    accept = _accept_for_player(p, pool)
    return {
        "game_type": "mystery",
        "player_id": p.id, "player_version": p.version,
        "reward": int(gcfg.get("reward", 30)),
        "duration": int(gcfg.get("duration", 60)),
        "question": {"kind": "blurred_card", "blur": int(gcfg.get("blur", 16))},
        "answer": {"name": p.name, "accept": accept},
    }


def generate_fastestfingers(session, cfg, exclude_ids=None):
    pool = eligible_players(session, cfg, exclude_ids=exclude_ids)
    pool = [p for p in pool if p.name]
    if not pool:
        return None
    p = random.choice(pool)
    gcfg = cfg.get("games", {}).get("fastestfingers", {})
    # Fastest Fingers wants precise names, not ambiguous surnames.
    accept = _accept_for_player(p, pool, allow_surname=False)
    return {
        "game_type": "fastestfingers",
        "player_id": p.id, "player_version": p.version,
        "reward": int(gcfg.get("reward", 20)),
        "duration": int(gcfg.get("duration", 30)),
        "question": {"kind": "spoiler_card"},
        "answer": {"name": p.name, "accept": accept},
    }


_ODD_CATEGORIES = [
    ("country", "Nationality", lambda p: p.country),
    ("bat_hand", "Batting Hand", lambda p: p.bat_hand),
    ("bowl_hand", "Bowling Hand", lambda p: p.bowl_hand),
    ("bowl_style", "Bowling Style", lambda p: p.bowl_style),
    ("category", "Player Category", lambda p: p.category),
    ("overall_band", "Overall Rating Range", lambda p: _rating_band(p.rating)),
    ("bat_band", "Batting Rating Range", lambda p: _rating_band(p.bat_rating)),
    ("bowl_band", "Bowling Rating Range", lambda p: _rating_band(p.bowl_rating)),
    ("version", "Player Version", lambda p: p.version),
]


def generate_oddoneout(session, cfg, exclude_ids=None):
    pool = eligible_players(session, cfg, exclude_ids=exclude_ids)
    pool = [p for p in pool if p.name]
    if len(pool) < 4:
        return None
    gcfg = cfg.get("games", {}).get("oddoneout", {})
    cats = _ODD_CATEGORIES[:]
    random.shuffle(cats)
    for _key, label, fn in cats:
        # Group players by their value for this category.
        groups = {}
        for p in pool:
            v = fn(p)
            if v in (None, ""):
                continue
            groups.setdefault(v, []).append(p)
        # Need a value with >=3 players (the shared trio) and at least one
        # player with a DIFFERENT value (the odd one).
        shareable = [(v, ps) for v, ps in groups.items() if len(ps) >= 3]
        random.shuffle(shareable)
        for shared_val, members in shareable:
            others = [p for p in pool if fn(p) not in (None, "", shared_val)]
            if not others:
                continue
            trio = random.sample(members, 3)
            odd = random.choice(others)
            options = trio + [odd]
            random.shuffle(options)
            correct_idx = options.index(odd)
            letters = ["A", "B", "C", "D"]
            accept = [letters[correct_idx]]
            accept += _accept_for_player(odd, pool)
            return {
                "game_type": "oddoneout",
                "player_id": odd.id, "player_version": odd.version,
                "reward": int(gcfg.get("reward", 20)),
                "duration": int(gcfg.get("duration", 30)),
                "question": {
                    "hint": label,
                    "options": [{"letter": letters[i], "name": options[i].name}
                                for i in range(4)],
                },
                "answer": {"letter": letters[correct_idx], "name": odd.name,
                           "accept": accept},
            }
    return None


# Common cricketing words to avoid as Wordle answers would be surnames anyway.
def generate_wordle(session, cfg, exclude_ids=None):
    import re
    pool = eligible_players(session, cfg, exclude_ids=exclude_ids)
    gcfg = cfg.get("games", {}).get("wordle", {})
    min_len = int(gcfg.get("min_len", 4))
    max_len = int(gcfg.get("max_len", 8))
    # Candidate surnames of the configured length, alphabetic only.
    candidates = []
    seen = set()
    random.shuffle(pool)
    for p in pool:
        if not p.name:
            continue
        sn = p.name.strip().split()[-1]
        letters = re.sub(r"[^A-Za-z]", "", sn)
        if min_len <= len(letters) <= max_len:
            key = letters.lower()
            if key not in seen:
                seen.add(key)
                candidates.append((p, letters))
    if not candidates:
        return None
    p, word = random.choice(candidates)
    return {
        "game_type": "wordle",
        "player_id": p.id, "player_version": p.version,
        "reward": int(gcfg.get("reward", 40)),
        "duration": int(gcfg.get("duration", 120)),
        "max_attempts": int(gcfg.get("max_attempts", 6)),
        "question": {"length": len(word), "player_name": p.name},
        "answer": {"word": word, "name": p.name,
                   "accept": [word]},
    }


def generate_rating(session, cfg, exclude_ids=None):
    pool = eligible_players(session, cfg, exclude_ids=exclude_ids)
    pool = [p for p in pool if p.rating]
    if not pool:
        return None
    p = random.choice(pool)
    gcfg = cfg.get("games", {}).get("rating", {})
    return {
        "game_type": "rating",
        "player_id": p.id, "player_version": p.version,
        "reward": int(gcfg.get("reward", 25)),
        "duration": int(gcfg.get("duration", 45)),
        "max_attempts": int(gcfg.get("max_attempts", 3)),
        "question": {
            "name": p.name, "country": p.country, "category": p.category,
            "version": p.version,
        },
        "answer": {"rating": int(p.rating), "name": p.name,
                   "accept": [str(int(p.rating))]},
    }


# Guess the Cricketer clue ladder (ordered; emoji label + value fn)
_CRIC_CLUES = [
    ("country", "🌍 Country", lambda p: p.country),
    ("category", "🏏 Category", lambda p: p.category),
    ("bat_hand", "✋ Batting Hand", lambda p: p.bat_hand),
    ("overall", "⭐ Overall Rating", lambda p: p.rating),
    ("version", "🃏 Version", lambda p: p.version),
    ("bowl_hand", "🤾 Bowling Hand", lambda p: p.bowl_hand),
    ("bowl_style", "🎯 Bowling Style", lambda p: p.bowl_style),
    ("bat_rating", "🏏 Batting Rating", lambda p: p.bat_rating),
    ("bowl_rating", "🎳 Bowling Rating", lambda p: p.bowl_rating),
]


def _count_matches(pool, target, clues):
    cnt = 0
    for p in pool:
        if all(fn(p) == fn(target) for _k, _lbl, fn in clues):
            cnt += 1
    return cnt


def generate_cricketer(session, cfg, exclude_ids=None, difficulty=None):
    pool = eligible_players(session, cfg, exclude_ids=exclude_ids)
    pool = [p for p in pool if p.name and p.country and p.category]
    if not pool:
        return None
    gcfg = cfg.get("games", {}).get("cricketer", {})
    difficulty = (difficulty or gcfg.get("difficulty", "medium")).lower()

    # Difficulty shapes the candidate pool + how many clues we start with.
    if difficulty == "easy":
        cand = [p for p in pool if (p.rating or 0) >= 84] or pool
        start_clues = 5
    elif difficulty == "hard":
        cand = [p for p in pool if (p.rating or 0) <= 83] or pool
        start_clues = 3
    else:
        cand = pool
        start_clues = 4

    random.shuffle(cand)
    for target in cand[:40]:
        clues = _CRIC_CLUES[:start_clues]
        # Add clues until exactly one eligible player matches all shown.
        ok = False
        while True:
            matches = _count_matches(pool, target, clues)
            if matches <= 1:
                ok = matches == 1
                break
            if len(clues) >= len(_CRIC_CLUES):
                break
            clues = _CRIC_CLUES[:len(clues) + 1]
        if not ok:
            continue
        accept = _accept_for_player(target, pool)
        shown = []
        for _k, lbl, fn in clues:
            shown.append({"label": lbl, "value": str(fn(target))})
        return {
            "game_type": "cricketer",
            "player_id": target.id, "player_version": target.version,
            "reward": int(gcfg.get("reward", 30)),
            "duration": int(gcfg.get("duration", 60)),
            "difficulty": difficulty,
            "question": {"clues": shown},
            "answer": {"name": target.name, "accept": accept},
        }
    return None


_GENERATORS = {
    "mystery": generate_mystery,
    "oddoneout": generate_oddoneout,
    "wordle": generate_wordle,
    "fastestfingers": generate_fastestfingers,
    "rating": generate_rating,
    "cricketer": generate_cricketer,
}


def generate_payload(session, game_type, cfg, *, exclude_ids=None, difficulty=None):
    gen = _GENERATORS.get(game_type)
    if gen is None:
        return None
    if game_type == "cricketer":
        return gen(session, cfg, exclude_ids=exclude_ids, difficulty=difficulty)
    return gen(session, cfg, exclude_ids=exclude_ids)


# ── Persistence / lifecycle ────────────────────────────────────────────

def active_game_for_chat(session, chat_id, *, lock=False):
    from models import CommunityGame
    q = (session.query(CommunityGame)
         .filter(CommunityGame.chat_id == chat_id,
                 CommunityGame.status == "active"))
    if lock:
        q = q.with_for_update()
    return q.first()


def create_game(session, chat_id, payload, *, source="auto", launched_by=None,
                launch_key=None, now=None):
    """Persist a generated payload as an active game. Returns the game or None
    if one is already active in the chat (single-active rule)."""
    from models import CommunityGame
    if payload is None:
        return None
    now = now or datetime.utcnow()
    if active_game_for_chat(session, chat_id) is not None:
        return None
    duration = int(payload.get("duration", 60))
    game = CommunityGame(
        game_type=payload["game_type"], chat_id=chat_id, status="active",
        question_json=json.dumps(payload.get("question", {})),
        answer_json=json.dumps(payload.get("answer", {})),
        reward_xp=int(payload.get("reward", 0)),
        duration_seconds=duration,
        max_attempts=payload.get("max_attempts"),
        difficulty=payload.get("difficulty"),
        source=source, launched_by=launched_by,
        player_id=payload.get("player_id"),
        player_version=payload.get("player_version"),
        started_at=now, deadline=now + timedelta(seconds=duration),
        launch_key=launch_key or f"{chat_id}:{payload['game_type']}:{uuid.uuid4().hex}",
    )
    session.add(game)
    try:
        session.flush()
    except Exception:
        session.rollback()
        return None
    mark_active(chat_id)
    return game


def _answer_data(game):
    try:
        return json.loads(game.answer_json or "{}")
    except (ValueError, TypeError):
        return {}


def _question_data(game):
    try:
        return json.loads(game.question_json or "{}")
    except (ValueError, TypeError):
        return {}


def get_or_create_participant(session, game, user_id, telegram_id, now=None):
    from models import CommunityGameParticipant
    now = now or datetime.utcnow()
    part = (session.query(CommunityGameParticipant)
            .filter(CommunityGameParticipant.game_id == game.id,
                    CommunityGameParticipant.user_id == user_id).first())
    if part is None:
        part = CommunityGameParticipant(
            game_id=game.id, user_id=user_id, telegram_id=telegram_id,
            attempts=0, first_attempt_at=now)
        session.add(part)
        game.participant_count = int(game.participant_count or 0) + 1
        session.flush()
    return part


def submit_answer(session, game_id, user_id, telegram_id, text, *,
                  server_received_at=None, fuzzy=False):
    """Process one answer submission atomically.

    Returns a dict with ``status`` in:
      ``won`` | ``wrong`` | ``closed`` | ``expired`` | ``exhausted`` |
      ``wordle`` (feedback, no win) | ``ignored``
    plus game-specific fields. The game row is locked so concurrent correct
    answers collapse to a single winner.
    """
    from services import community_answer_service as A
    now = server_received_at or datetime.utcnow()

    from models import CommunityGame
    game = (session.query(CommunityGame)
            .filter(CommunityGame.id == game_id)
            .with_for_update().first())
    if game is None:
        return {"status": "ignored"}
    if game.status != "active":
        return {"status": "closed"}
    if now > game.deadline:
        return {"status": "expired"}

    part = get_or_create_participant(session, game, user_id, telegram_id, now)
    ans = _answer_data(game)
    gtype = game.game_type

    # Per-user attempt limit (rating, wordle).
    max_attempts = game.max_attempts
    if max_attempts and part.attempts >= int(max_attempts):
        return {"status": "exhausted", "max_attempts": int(max_attempts)}
    part.attempts = int(part.attempts or 0) + 1

    correct = False
    feedback = None

    if gtype == "rating":
        guess = (text or "").strip()
        if guess.isdigit():
            correct = int(guess) == int(ans.get("rating", -1))
    elif gtype == "wordle":
        word = str(ans.get("word", ""))
        guess = (text or "").strip()
        # Only treat as a guess if it is alphabetic and the right length.
        import re as _re
        gl = _re.sub(r"[^A-Za-z]", "", guess)
        if len(gl) != len(word):
            # Not a valid guess shape — don't consume an attempt.
            part.attempts -= 1
            session.flush()
            return {"status": "ignored"}
        feedback = wordle_render(gl, word)
        # Track guess history on the participant.
        try:
            hist = json.loads(part.data_json or "[]")
        except (ValueError, TypeError):
            hist = []
        hist.append({"guess": gl.lower(), "fb": feedback})
        part.data_json = json.dumps(hist[-12:])
        correct = A.key(gl) == A.key(word)
    elif gtype == "oddoneout":
        guess = (text or "").strip()
        accept = ans.get("accept", [])
        # Single-letter answers must equal the correct letter exactly.
        if len(guess) == 1 and guess.isalpha():
            correct = guess.upper() == str(ans.get("letter", "")).upper()
        else:
            correct = A.matches(guess, [a for a in accept if len(a) > 1], fuzzy=fuzzy)
    else:  # mystery, fastestfingers, cricketer → name match
        correct = A.matches(text, ans.get("accept", []), fuzzy=fuzzy)

    session.flush()

    if not correct:
        if feedback is not None:
            return {"status": "wordle", "feedback": feedback,
                    "attempts": part.attempts, "max_attempts": game.max_attempts}
        return {"status": "wrong", "attempts": part.attempts,
                "max_attempts": game.max_attempts}

    # Correct — win the game (still holding the row lock).
    response_ms = int(max(0, (now - game.started_at).total_seconds() * 1000))
    game.status = "closed"
    game.revealed = True
    game.closed_at = now
    game.winner_user_id = user_id
    game.winner_telegram_id = telegram_id
    game.winner_response_ms = response_ms
    part.is_winner = True
    part.response_ms = response_ms
    session.flush()

    award = record_win(session, game, user_id, telegram_id, response_ms, now=now)
    session.commit()
    mark_inactive(game.chat_id)

    return {"status": "won", "response_ms": response_ms,
            "participants": int(game.participant_count or 0),
            "feedback": feedback, "award": award,
            "answer_name": ans.get("name"), "game": game}


def _bump_stat(session, user_id, game_type, *, won, response_ms=None, now=None):
    from models import CommunityGameStat
    now = now or datetime.utcnow()
    stat = (session.query(CommunityGameStat)
            .filter(CommunityGameStat.user_id == user_id,
                    CommunityGameStat.game_type == game_type).first())
    if stat is None:
        stat = CommunityGameStat(user_id=user_id, game_type=game_type,
                                 plays=0, wins=0, total_win_ms=0)
        session.add(stat)
    stat.plays = int(stat.plays or 0) + 1
    if won:
        stat.wins = int(stat.wins or 0) + 1
        stat.last_win_at = now
        if response_ms is not None:
            stat.total_win_ms = int(stat.total_win_ms or 0) + int(response_ms)
            if stat.fastest_ms is None or response_ms < stat.fastest_ms:
                stat.fastest_ms = int(response_ms)
    session.flush()
    return stat


def record_win(session, game, user_id, telegram_id, response_ms, now=None):
    """Update stats, award capped XP, check achievements. Caller commits.

    Returns the XP award result dict (so the announcement can report the real
    credited amount and any level-up)."""
    from services import community_xp_service as X
    from services import community_achievement_service as ACH
    from services import community_config_service as C

    now = now or datetime.utcnow()
    gtype = game.game_type

    # Plays are recorded for every participant at reveal time; the winner's
    # play+win is recorded here.
    _bump_stat(session, user_id, gtype, won=True, response_ms=response_ms, now=now)

    activity = X.GAME_ACTIVITY.get(gtype, f"game_{gtype}")
    gcfg = C.game_cfg(gtype, session)
    daily_win_limit = int(gcfg.get("daily_win_limit", 0) or 0) or None

    award = X.award_xp(
        user_id, activity, int(game.reward_xp or 0),
        event_key=f"game_win:{game.id}",
        chat_id=game.chat_id, detail=f"{GAME_LABEL.get(gtype, gtype)} win",
        now=now, session=session,
        activity_daily_limit=daily_win_limit)

    try:
        got = ACH.check_game_win_achievements(session, user_id, gtype, response_ms)
        got += ACH.check_xp_achievements(session, user_id)
        award["achievements"] = ACH.names(got)
    except Exception:
        logger.exception("achievement check failed (non-fatal)")
        award["achievements"] = []

    # Milestone reward on level-up (cosmetic always; monetary if enabled).
    try:
        if award.get("leveled_up"):
            from services import community_level_service as L
            award["milestones"] = L.grant_due_milestones(session, user_id)
    except Exception:
        logger.exception("milestone grant failed (non-fatal)")
        award["milestones"] = []
    return award


def record_participation_plays(session, game):
    """At reveal, count a 'play' for every participant who did not win (the
    winner's play is counted in record_win)."""
    from models import CommunityGameParticipant
    parts = (session.query(CommunityGameParticipant)
             .filter(CommunityGameParticipant.game_id == game.id,
                     CommunityGameParticipant.is_winner.is_(False)).all())
    for part in parts:
        _bump_stat(session, part.user_id, game.game_type, won=False)


def expire_game(session, game, now=None):
    """Mark an overdue, unwon game expired and record participation plays.
    Caller commits."""
    now = now or datetime.utcnow()
    if game.status != "active":
        return False
    game.status = "expired"
    game.revealed = True
    game.closed_at = now
    try:
        record_participation_plays(session, game)
    except Exception:
        logger.exception("participation stat update failed (non-fatal)")
    session.flush()
    mark_inactive(game.chat_id)
    return True


def cancel_game(session, game, now=None):
    now = now or datetime.utcnow()
    if game.status != "active":
        return False
    game.status = "cancelled"
    game.closed_at = now
    session.flush()
    mark_inactive(game.chat_id)
    return True
