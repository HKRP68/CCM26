"""Match Manager — coordination layer for ball-by-ball cricket matches.

Python translation of the JavaScript matchManager.js. Assembles existing services
(match_engine, probability_engine, bot_ai, match_rewards) into the Match class
pattern from the JS without reimplementing their logic.

JS import mapping:
  gameConstants.getMaxOversPerBowler  → config.get_max_overs_per_bowler
  gameConstants.WINNER_REWARD_PER_OVER → config.WINNER_REWARD_PER_OVER
  calculateBallOutcome                → services.probability_engine.calculate_outcome
  ai.pick_bot_next_bowler            → services.bot_ai.pick_bot_next_bowler
  sb.saveCricketMatch / getActive...  → SQLAlchemy helpers below
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from database import SessionLocal
from models import Match as MatchModel, MatchState
from config import WINNER_REWARD_PER_OVER, LOSER_REWARD_PER_OVER, get_max_overs_per_bowler

logger = logging.getLogger(__name__)


# ── Default XI ────────────────────────────────────────────────────────────────
# Static fallback team used when a real roster is unavailable (AI opponent, tests).
# Mirrors JS DEFAULT_XI; dict keys match the roster player format used by match_engine.

DEFAULT_XI: List[Dict[str, Any]] = [
    {"roster_id": -1,  "player_id": -1,  "name": "Rohit Sharma",    "rating": 92,
     "category": "Batsman",      "bat_rating": 92, "bowl_rating": 30,
     "bowl_style": "Off Spinner",  "bowl_hand": "Right", "bat_hand": "Right"},
    {"roster_id": -2,  "player_id": -2,  "name": "Virat Kohli",     "rating": 95,
     "category": "Batsman",      "bat_rating": 95, "bowl_rating": 25,
     "bowl_style": "Medium Pacer", "bowl_hand": "Right", "bat_hand": "Right"},
    {"roster_id": -3,  "player_id": -3,  "name": "Shubman Gill",    "rating": 85,
     "category": "Batsman",      "bat_rating": 85, "bowl_rating": 20,
     "bowl_style": "Off Spinner",  "bowl_hand": "Right", "bat_hand": "Right"},
    {"roster_id": -4,  "player_id": -4,  "name": "Hardik Pandya",   "rating": 87,
     "category": "All-rounder",  "bat_rating": 82, "bowl_rating": 84,
     "bowl_style": "Fast",         "bowl_hand": "Right", "bat_hand": "Right"},
    {"roster_id": -5,  "player_id": -5,  "name": "Rishabh Pant",    "rating": 88,
     "category": "Wicket Keeper","bat_rating": 88, "bowl_rating": 10,
     "bowl_style": "Off Spinner",  "bowl_hand": "Right", "bat_hand": "Left"},
    {"roster_id": -6,  "player_id": -6,  "name": "Ravindra Jadeja", "rating": 89,
     "category": "All-rounder",  "bat_rating": 80, "bowl_rating": 89,
     "bowl_style": "Leg Spinner",  "bowl_hand": "Left",  "bat_hand": "Left"},
    {"roster_id": -7,  "player_id": -7,  "name": "Axar Patel",      "rating": 80,
     "category": "All-rounder",  "bat_rating": 72, "bowl_rating": 82,
     "bowl_style": "Off Spinner",  "bowl_hand": "Left",  "bat_hand": "Left"},
    {"roster_id": -8,  "player_id": -8,  "name": "Jasprit Bumrah",  "rating": 95,
     "category": "Bowler",       "bat_rating": 20, "bowl_rating": 96,
     "bowl_style": "Fast",         "bowl_hand": "Right", "bat_hand": "Right"},
    {"roster_id": -9,  "player_id": -9,  "name": "Mohammed Shami",  "rating": 88,
     "category": "Bowler",       "bat_rating": 18, "bowl_rating": 88,
     "bowl_style": "Fast",         "bowl_hand": "Right", "bat_hand": "Right"},
    {"roster_id": -10, "player_id": -10, "name": "Kuldeep Yadav",   "rating": 83,
     "category": "Bowler",       "bat_rating": 16, "bowl_rating": 84,
     "bowl_style": "Leg Spinner",  "bowl_hand": "Left",  "bat_hand": "Left"},
    {"roster_id": -11, "player_id": -11, "name": "Arshdeep Singh",  "rating": 82,
     "category": "Bowler",       "bat_rating": 14, "bowl_rating": 83,
     "bowl_style": "Fast",         "bowl_hand": "Left",  "bat_hand": "Left"},
]


# ── In-memory match stores ─────────────────────────────────────────────────────
# JS: const activeMatches = {}; const completedMatches = {};

active_matches: Dict[int, "MatchManager"] = {}      # keyed by match_id
completed_matches: Dict[int, Dict[str, Any]] = {}   # serialized snapshots


# ── SQLAlchemy DB helpers (replace Supabase sb.* from the JS) ─────────────────

def _save_cricket_match(match_id: int, state_json: str, status: str = "PICK_DELIVERY") -> None:
    """JS: sb.saveCricketMatch(...)  Upserts the MatchState row for a match."""
    session = SessionLocal()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == match_id).first()
        if ms:
            ms.state_json = state_json
            ms.next_action = status
            ms.version = (ms.version or 0) + 1
            ms.last_modified = datetime.utcnow()
        else:
            ms = MatchState(
                match_id=match_id,
                state_json=state_json,
                next_action=status,
                version=1,
                ball_seq=0,
                last_modified=datetime.utcnow(),
            )
            session.add(ms)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("_save_cricket_match failed for match_id=%s", match_id)
    finally:
        session.close()


def _get_active_cricket_matches() -> List[Dict[str, Any]]:
    """JS: sb.getActiveCricketMatches()  Returns non-completed MatchState rows."""
    session = SessionLocal()
    try:
        rows = (
            session.query(MatchState)
            .filter(MatchState.next_action != "COMPLETED")
            .all()
        )
        return [
            {"match_id": r.match_id, "state_json": r.state_json, "next_action": r.next_action}
            for r in rows
        ]
    except Exception:
        logger.exception("_get_active_cricket_matches failed")
        return []
    finally:
        session.close()


# ── MatchManager class ────────────────────────────────────────────────────────

class MatchManager:
    """Coordination layer for a live ball-by-ball cricket match.

    Python translation of the JS Match class. Holds the live state dict and
    delegates all simulation / persistence to existing service modules.

    host / guest dicts must have:
      user_id (int, DB User.id PK), telegram_id (int), username (str), team_name (str)
    """

    def __init__(
        self,
        *,
        match_id: int,
        match_type: str = "pvp",
        chat_id: int,
        total_overs: int,
        pitch: str = "Flat",
        host: Dict[str, Any],
        guest: Dict[str, Any],
        host_xi: Optional[List[Dict]] = None,
        guest_xi: Optional[List[Dict]] = None,
    ) -> None:
        # JS: this.id, this.type, this.chatId, this.totalOvers, this.pitch, ...
        self.match_id = match_id
        self.match_type = match_type      # "pvp" | "vsbot"
        self.chat_id = chat_id
        self.total_overs = total_overs
        self.pitch = pitch
        self.host = host
        self.guest = guest
        self.host_xi: List[Dict] = host_xi or list(DEFAULT_XI)
        self.guest_xi: List[Dict] = guest_xi or list(DEFAULT_XI)

        # Live state dict — populated by start_first_innings
        self._state: Optional[Dict[str, Any]] = None

        # Set externally (after toss) before calling start_first_innings
        self.batting_first_user_id: Optional[int] = None
        self.bowling_first_user_id: Optional[int] = None

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def current_innings(self) -> int:
        """JS: get currentInnings()"""
        return self._state.get("innings", 1) if self._state else 1

    @property
    def batting_team(self) -> Optional[Dict[str, Any]]:
        """JS: get battingTeam()"""
        if not self._state:
            return None
        bat_uid = self._state.get("bat_team_id")
        return self.host if bat_uid == self.host["user_id"] else self.guest

    @property
    def bowling_team(self) -> Optional[Dict[str, Any]]:
        """JS: get bowlingTeam()"""
        if not self._state:
            return None
        bowl_uid = self._state.get("bowl_team_id")
        return self.host if bowl_uid == self.host["user_id"] else self.guest

    @property
    def striker(self) -> Optional[Dict[str, Any]]:
        """JS: get striker()"""
        if not self._state:
            return None
        from services.match_engine import get_striker
        return get_striker(self._state)

    @property
    def non_striker(self) -> Optional[Dict[str, Any]]:
        """JS: get nonStriker()"""
        if not self._state:
            return None
        from services.match_engine import get_non_striker
        return get_non_striker(self._state)

    @property
    def current_bowler(self) -> Optional[Dict[str, Any]]:
        """JS: get currentBowler()"""
        if not self._state:
            return None
        from services.match_engine import get_bowler
        return get_bowler(self._state)

    @property
    def state(self) -> Optional[Dict[str, Any]]:
        """Raw state dict — for services that need direct access."""
        return self._state

    # ── Innings setup ───────────────────────────────────────────────────────

    def start_first_innings(
        self,
        bat_xi: List[Dict],
        bowl_xi: List[Dict],
        opener1: Dict,
        opener2: Dict,
        opening_bowler: Dict,
    ) -> None:
        """JS: startFirstInnings() — creates the initial match state dict."""
        from services.match_engine import create_match_state

        bat_uid = self.batting_first_user_id
        bowl_uid = self.bowling_first_user_id
        batting_team = self.host if bat_uid == self.host["user_id"] else self.guest
        bowling_team = self.guest if bat_uid == self.host["user_id"] else self.host

        self._state = create_match_state(
            self.match_id, self.total_overs,
            bat_uid, bowl_uid,
            bat_xi, bowl_xi,
            opener1, opener2, opening_bowler,
        )
        # Populate context fields that create_match_state leaves empty
        self._state.update({
            "chat_id": self.chat_id,
            "pitch_type": self.pitch,
            "is_vsbot": self.match_type == "vsbot",
            "bat_team_name": batting_team.get("team_name") or batting_team.get("username", "Team A"),
            "bowl_team_name": bowling_team.get("team_name") or bowling_team.get("username", "Team B"),
            "bat_user_tg": batting_team.get("telegram_id"),
            "bowl_user_tg": bowling_team.get("telegram_id"),
            "bat_username": batting_team.get("username"),
            "bowl_username": bowling_team.get("username"),
        })

    def start_second_innings(self, opening_bowler: Dict) -> int:
        """JS: startSecondInnings() — transitions to innings 2. Returns target."""
        from services.match_engine import transition_to_second_innings
        target = transition_to_second_innings(self._state)
        self._state["current_bowler"] = opening_bowler
        return target

    def init_player_stats(self) -> None:
        """JS: initPlayerStats() — no-op; create_match_state already initialises stats."""
        pass

    # ── Bowler eligibility ──────────────────────────────────────────────────

    def is_bowler_eligible(self, player: Dict) -> bool:
        """JS: isBowlerEligible(player)
        A bowler is ineligible if they bowled the previous over or have hit their quota.
        """
        if not self._state:
            return True
        rid = player["roster_id"]
        if rid == self._state.get("prev_bowler_rid"):
            return False
        quota = get_max_overs_per_bowler(self.total_overs)
        bowl_stats = self._state.get("bowl_stats", {})
        player_stats = bowl_stats.get(rid) or bowl_stats.get(str(rid)) or {}
        return player_stats.get("overs_done", 0) < quota

    def get_eligible_bowler_indices(self) -> List[int]:
        """JS: getEligibleBowlerIndices()"""
        if not self._state:
            return []
        return [
            i for i, p in enumerate(self._state.get("bowl_xi", []))
            if self.is_bowler_eligible(p)
        ]

    def select_best_bowler(self) -> Optional[Dict]:
        """JS: selectBestBowler() — AI picks the highest-rated eligible bowler."""
        if not self._state:
            return None
        from services.bot_ai import pick_bot_next_bowler
        return pick_bot_next_bowler(
            self._state.get("bowl_xi", []),
            self._state.get("prev_bowler_rid"),
            self._state.get("bowl_stats", {}),
            self.total_overs,
        )

    # ── Ball simulation ─────────────────────────────────────────────────────

    def bowl_ball(self, shot: str, delivery: str) -> Dict[str, Any]:
        """JS: bowlBall() — calculates one ball outcome.

        Synchronous; returns the outcome dict from probability_engine.
        State mutation (runs, wickets, over counters) is the caller's responsibility,
        matching the handler pattern in handlers/match.py.

        Args:
            shot:     Shot type, e.g. "Drive", "Slog"
            delivery: Full delivery string, e.g. "Outswing Hard" or "Googly"

        Returns:
            {"type": "runs"|"wicket"|"wide"|"noball"|"legbye",
             "runs": int, "how": str, "traits_activated": [...]}
        """
        from services.probability_engine import calculate_outcome, calc_pitch_wear
        from services.bowling_service import is_spinner

        striker = self.striker
        bowler = self.current_bowler
        s = self._state
        if not striker or not bowler or not s:
            raise ValueError("bowl_ball called when match is not in a bowlable state")

        bowl_style = bowler.get("bowl_style", "Medium Pacer")
        bowl_hand = bowler.get("bowl_hand", "Right")

        # Parse delivery string into (variation, length).
        # Spinners have no length — the whole string is the variation.
        delivery_clean = delivery.replace(" (Surprise)", "").strip()
        if is_spinner(bowl_style):
            variation = delivery_clean
            length = None
        else:
            known_lengths = {
                "Hard", "Good", "Full", "Yorker", "Bouncer",
                "Good Length", "Full Length", "Short of Length",
                "Back of Length", "Hit the Deck",
            }
            variation = delivery_clean
            length = None
            for ln in sorted(known_lengths, key=len, reverse=True):
                if delivery_clean.endswith(ln):
                    variation = delivery_clean[: -len(ln)].strip()
                    length = ln
                    break
            if not length:
                parts = delivery_clean.rsplit(" ", 1)
                variation, length = (parts[0], parts[1]) if len(parts) == 2 else (delivery_clean, "Good")

        pitch_wear = calc_pitch_wear(
            s.get("innings", 1),
            s.get("current_over", 1),
            self.total_overs,
        )

        valid_pitches = {"Green", "Dry", "Dusty", "Hard", "Flat"}
        pitch_type = self.pitch if self.pitch in valid_pitches else "Flat"

        return calculate_outcome(
            bowl_style, bowl_hand,
            variation, length,
            pitch_type,
            s.get("current_over", 1),
            self.total_overs,
            shot,
            striker.get("bat_rating", 70),
            bowler.get("bowl_rating", 70),
            pitch_wear=pitch_wear,
            bat_hand=striker.get("bat_hand"),
        )

    # ── Innings / match end ─────────────────────────────────────────────────

    def check_innings_ended(self) -> bool:
        """JS: checkInningsEnded()"""
        if not self._state:
            return False
        from services.match_engine import is_innings_over
        return is_innings_over(self._state)

    async def finalize_match(self) -> Dict[str, Any]:
        """JS: async finalizeMatch() — awards rewards, updates DB, returns result dict.

        Returns:
            {winner_user_id, loser_user_id, winner_coins, winner_gems,
             loser_coins, loser_gems, margin_type, margin_value, result_text}
        """
        from services.match_engine import compute_match_result
        from services.match_rewards import award_match_rewards_core

        s = self._state
        if not s:
            raise RuntimeError("finalize_match called with no active state")

        result = compute_match_result(s)
        if not result:
            raise RuntimeError("finalize_match called before match is over")

        winner_uid = result.get("winner_team_id")   # DB User.id
        loser_uid = result.get("loser_team_id")
        margin_type = result.get("margin_type", "runs")
        margin_val = result.get("margin_value", 0)
        result_text = result.get("text", "")

        # Award coins/gems and update matches_won/lost via the existing rewards service.
        session = SessionLocal()
        w_coins = w_gems = l_coins = l_gems = 0
        try:
            w_coins, w_gems, l_coins, l_gems = award_match_rewards_core(
                session,
                winner_user_id=winner_uid,
                loser_user_id=loser_uid,
                overs=self.total_overs,
                is_vsbot=(self.match_type == "vsbot"),
            )
            # Update Match row with result details
            match_row = session.query(MatchModel).get(self.match_id)
            if match_row:
                match_row.status = "completed"
                match_row.winner_id = winner_uid
                match_row.loser_id = loser_uid
                match_row.margin_type = margin_type
                match_row.margin_value = margin_val
                match_row.inn1_runs = s.get("inn1_runs", 0)
                match_row.inn1_wickets = s.get("inn1_wickets", 0)
                match_row.inn2_runs = s.get("total_runs", 0)
                match_row.inn2_wickets = s.get("total_wickets", 0)
                match_row.completed_at = datetime.utcnow()
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("finalize_match: rewards/DB update failed (match_id=%s)", self.match_id)
            w_coins = self.total_overs * WINNER_REWARD_PER_OVER
            l_coins = self.total_overs * LOSER_REWARD_PER_OVER
        finally:
            session.close()

        # Win streaks / active days are handled inside award_match_rewards_core
        # above, so every mode that pays match rewards moves them identically.

        # Persist final state and mark completed
        _save_cricket_match(self.match_id, json.dumps(s, default=str), status="COMPLETED")

        # Move from active to completed snapshot cache
        active_matches.pop(self.match_id, None)
        completed_matches[self.match_id] = self.serialize()

        return {
            "winner_user_id": winner_uid,
            "loser_user_id": loser_uid,
            "winner_coins": w_coins,
            "winner_gems": w_gems,
            "loser_coins": l_coins,
            "loser_gems": l_gems,
            "margin_type": margin_type,
            "margin_value": margin_val,
            "result_text": result_text,
        }

    # ── Serialization ───────────────────────────────────────────────────────

    def serialize(self) -> Dict[str, Any]:
        """JS: serialize() — returns a JSON-safe snapshot of the full match."""
        return {
            "match_id": self.match_id,
            "match_type": self.match_type,
            "chat_id": self.chat_id,
            "total_overs": self.total_overs,
            "pitch": self.pitch,
            "host": self.host,
            "guest": self.guest,
            "batting_first_user_id": self.batting_first_user_id,
            "bowling_first_user_id": self.bowling_first_user_id,
            "state": self._state,
        }


# ── Module-level functions ────────────────────────────────────────────────────

def deserialize_match(data: Dict[str, Any]) -> MatchManager:
    """JS: deserializeMatch(data) — reconstructs a MatchManager from its serialized dict."""
    mm = MatchManager(
        match_id=data["match_id"],
        match_type=data.get("match_type", "pvp"),
        chat_id=data["chat_id"],
        total_overs=data["total_overs"],
        pitch=data.get("pitch", "Flat"),
        host=data["host"],
        guest=data["guest"],
    )
    mm.batting_first_user_id = data.get("batting_first_user_id")
    mm.bowling_first_user_id = data.get("bowling_first_user_id")
    mm._state = data.get("state")
    return mm


def save_to_db(mm: MatchManager, status: str = "PICK_DELIVERY") -> None:
    """JS: saveToDb(match) — persists current state to the MatchState table."""
    state_json = json.dumps(mm._state, default=str) if mm._state else "{}"
    _save_cricket_match(mm.match_id, state_json, status=status)


def load_active_matches_from_db() -> None:
    """JS: loadActiveMatchesFromDb() — startup recovery from the MatchState table.

    Reconstructs MatchManager instances for all in-progress matches and
    populates active_matches. Call once from bot.py before run_polling().
    """
    rows = _get_active_cricket_matches()
    for row in rows:
        try:
            state = json.loads(row["state_json"] or "{}")
            if not state:
                continue
            match_id = row["match_id"]
            bat_uid = state.get("bat_team_id")
            bowl_uid = state.get("bowl_team_id")
            host = {
                "user_id": bat_uid,
                "telegram_id": state.get("bat_user_tg", 0),
                "username": state.get("bat_username", ""),
                "team_name": state.get("bat_team_name", "Host"),
            }
            guest = {
                "user_id": bowl_uid,
                "telegram_id": state.get("bowl_user_tg", 0),
                "username": state.get("bowl_username", ""),
                "team_name": state.get("bowl_team_name", "Guest"),
            }
            mm = MatchManager(
                match_id=match_id,
                match_type="vsbot" if state.get("is_vsbot") else "pvp",
                chat_id=state.get("chat_id", 0),
                total_overs=state.get("overs", 20),
                pitch=state.get("pitch_type", "Flat"),
                host=host,
                guest=guest,
            )
            mm._state = state
            mm.batting_first_user_id = bat_uid
            mm.bowling_first_user_id = bowl_uid
            active_matches[match_id] = mm
        except Exception:
            logger.exception(
                "load_active_matches_from_db: failed to restore match_id=%s",
                row.get("match_id"),
            )
    logger.info("Restored %d active match(es) from DB into match_manager cache", len(active_matches))


def create_match_from_lobby(
    match_id: int,
    chat_id: int,
    total_overs: int,
    pitch: str,
    host: Dict[str, Any],
    guest: Dict[str, Any],
    batting_first_user_id: int,
    bowling_first_user_id: int,
    host_xi: Optional[List[Dict]] = None,
    guest_xi: Optional[List[Dict]] = None,
    match_type: str = "pvp",
) -> MatchManager:
    """JS: createMatchFromLobby(...) — factory: creates a MatchManager and registers it."""
    mm = MatchManager(
        match_id=match_id,
        match_type=match_type,
        chat_id=chat_id,
        total_overs=total_overs,
        pitch=pitch,
        host=host,
        guest=guest,
        host_xi=host_xi,
        guest_xi=guest_xi,
    )
    mm.batting_first_user_id = batting_first_user_id
    mm.bowling_first_user_id = bowling_first_user_id
    active_matches[match_id] = mm
    return mm


def get_active_match(match_id: int) -> Optional[MatchManager]:
    """JS: getActiveMatch(matchId)"""
    return active_matches.get(match_id)


def get_match(match_id: int) -> Optional[MatchManager]:
    """JS: getMatch(matchId) — checks active then completed."""
    if match_id in active_matches:
        return active_matches[match_id]
    snap = completed_matches.get(match_id)
    return deserialize_match(snap) if snap else None
