"""Saved match positions for player continuation and admin restoration.

A Challenge League / Lets Play match (``handlers.cipl_play``) lives in the
``match_state`` table while it is being played. When it stops before a result
— ``/pause``, ``/clearmatches``, ``/removematch``, or a tournament match's idle
timeout — this module keeps a copy in ``saved_matches`` so it can be continued:

* :func:`save_snapshot` stores the state and the pick it was waiting on;
* :func:`park_match_row` takes the ``Match`` row out of play (``paused``) while
  keeping a tournament fixture reserved for it;
* :func:`revive_match` puts the SAME ``Match`` row back in play — re-reserving
  the tournament fixture / CL Tour slot if a clear handed it back — so the
  result, when it comes, is recorded exactly as if the match had never stopped.

Forfeits in live modes also save the last playable position. An admin can
continue that position as a new unranked match while the original result stands.

Everything here is blocking DB work: async callers run it in a worker thread.
The async side (the commands, the chat messages, the resume itself) is in
``handlers.cipl_pause``.
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Why a match was saved.
REASON_PAUSED = "paused"          # /pause
REASON_CLEARED = "cleared"        # /clearmatches, /removematch
REASON_AUTO = "auto_ended"        # an idle timeout that saved instead of forfeiting
REASON_FORFEIT = "forfeited"       # a settled result, restorable by a bot admin

REASON_LABELS = {
    REASON_PAUSED: "⏸ Paused",
    REASON_CLEARED: "🧹 Cleared",
    REASON_AUTO: "⏱ Timed out",
    REASON_FORFEIT: "🏳️ Forfeited · admin restore only",
}

# Lifecycle of a saved row.
ST_SAVED = "saved"            # resumable
ST_RESUMING = "resuming"      # claimed by a resume in flight
ST_RESUMED = "resumed"
ST_DISCARDED = "discarded"
ST_VOID = "void"              # can never be resumed (fixture played, tour over…)

# Chat plumbing that belongs to the moment the match stopped, not to the match:
# message ids from a chat the match may not even be resumed in, and a delivery
# flag that has to be earned again by the resumed prompt.
TRANSIENT_KEYS = ("action_msg_id", "action_remind_msg_id", "over_msg_ids",
                  "pinned_msg_id", "prompt_delivered", "odi_idle_warning")

# Match.status values a revived match must not be sitting in.
_LIVE_STATUSES = ("pending", "accepted", "toss", "selecting", "playing",
                  "active", "in_progress")


def _session():
    from database import get_session
    return get_session()


def is_saveable(state, next_action=None):
    """Can this state be saved and picked up again later?

    Only an over-by-over match that has not finished: a completed pointer or a
    result already paid out means there is nothing left to resume.
    """
    from services.match_state_store import A_COMPLETED
    if not isinstance(state, dict) or state.get("mode") != "cipl_approach":
        return False
    if next_action == A_COMPLETED or state.get("result_finalized"):
        return False
    return True


def participants(state):
    """``[(user_id, telegram_id), (user_id, telegram_id)]`` — host side first.

    In the first innings the batting side is the one that won or lost the toss;
    it doesn't matter which is which here, only that both are listed.
    """
    from services.match_handover import controller_uid
    return [(controller_uid(state, state.get(f"{side}_team_id")),
             state.get(f"{side}_user_tg")) for side in ("bat", "bowl")]


def human_user_ids(state):
    """The DB user ids of the human captains (the AI captain left out)."""
    bot_uid = state.get("bot_user_id")
    # Both the current captains and the stable result identities must be free
    # before reviving the same Match row.
    ids = [uid for uid, _tg in participants(state)]
    ids += [state.get('bat_team_id'), state.get('bowl_team_id')]
    return list(dict.fromkeys(uid for uid in ids if uid is not None and uid != bot_uid))


def describe(state):
    """One line naming the match and where it stands.

    ``RCB 87/3 (11.0 ov) vs CSK · 1st innings`` — or, in a chase,
    ``CSK 120/4 (15.2 ov) vs RCB · need 41``.
    """
    try:
        from services import cipl_match as cm
        bat = str(state.get("bat_team_name") or "Batting side")
        bowl = str(state.get("bowl_team_name") or "Bowling side")
        score = cm.format_score(state)
        unit = "sets" if cm.is_hundred(state) else "ov"
        bpu = cm.balls_per_unit(state)
        balls = cm.balls_bowled(state)
        prog = (f"{balls // bpu}.{balls % bpu}" if not cm.is_hundred(state)
                else f"{balls} balls")
        line = f"{bat} {score} ({prog} {unit}) vs {bowl}"
        if int(state.get("innings") or 1) == 1:
            return f"{line} · 1st innings"
        target = state.get("target")
        if target:
            need = max(0, int(target) - int(state.get("total_runs") or 0))
            return f"{line} · need {need}"
        return f"{line} · 2nd innings"
    except Exception:
        logger.debug("saved match describe failed", exc_info=True)
        return (f"{state.get('bat_team_name', 'Team')} vs "
                f"{state.get('bowl_team_name', 'Team')}")


def clean_state(state):
    """A copy of ``state`` without the chat plumbing of the moment it stopped."""
    out = dict(state)
    for key in TRANSIENT_KEYS:
        out.pop(key, None)
    return out


def _row_dict(row):
    if row is None:
        return None
    return {
        "id": row.id, "match_id": row.match_id, "chat_id": row.chat_id,
        "user1_id": row.user1_id, "user2_id": row.user2_id,
        "user1_tg": row.user1_tg, "user2_tg": row.user2_tg,
        "tournament_id": row.tournament_id, "reason": row.reason,
        "status": row.status, "title": row.title,
        "state_json": row.state_json, "next_action": row.next_action,
        "prev_status": row.prev_status, "saved_by_id": row.saved_by_id,
        "saved_at": row.saved_at, "resumed_at": row.resumed_at,
        "times_saved": row.times_saved or 1,
    }


def save_snapshot(mid, state, next_action, reason, by_user_id=None,
                  prev_status=None, *, session=None):
    """Store ``state`` (waiting on ``next_action``) as match ``mid``'s save.

    One row per match: saving a match that was saved before (paused, resumed,
    paused again) overwrites the older snapshot. Returns the row as a dict.
    Raises on a DB error — a caller about to throw the live state away must
    not do so unless this landed. An optional caller-owned session keeps the
    snapshot in the same transaction as a clear; that caller must commit.
    """
    from models import SavedMatch
    snap = clean_state(state)
    state_json = json.dumps(snap, default=str)
    (u1, t1), (u2, t2) = participants(state)
    owns_session = session is None
    session = session if session is not None else _session()
    try:
        row = (session.query(SavedMatch)
               .filter(SavedMatch.match_id == int(mid)).first())
        if row is None:
            row = SavedMatch(match_id=int(mid), times_saved=1)
            session.add(row)
        else:
            row.times_saved = (row.times_saved or 1) + 1
        row.chat_id = state.get("chat_id")
        row.user1_id, row.user1_tg = u1, t1
        row.user2_id, row.user2_tg = u2, t2
        row.tournament_id = state.get("tournament_id")
        row.reason = reason
        row.status = ST_SAVED
        row.title = describe(state)[:200]
        row.state_json = state_json
        row.next_action = next_action
        row.prev_status = prev_status
        row.saved_by_id = by_user_id
        row.saved_at = datetime.utcnow()
        row.resumed_at = None
        if owns_session:
            session.commit()
        else:
            session.flush()
        return _row_dict(row)
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def get_saved(mid):
    """The saved row for match ``mid`` (any status) as a dict, or None."""
    from models import SavedMatch
    session = _session()
    try:
        return _row_dict(session.query(SavedMatch)
                         .filter(SavedMatch.match_id == int(mid)).first())
    finally:
        session.close()


def save_forfeit_snapshot(session, match, state, next_action=None):
    """Archive the playable position in the same transaction as the forfeit."""
    from models import MatchState, User
    from services.match_state_store import A_COMPLETED, A_PICK_DELIVERY

    if not state or state.get("result_finalized") or state.get("match_result"):
        return None
    live = session.query(MatchState).filter_by(match_id=match.id).first()
    if next_action is None:
        next_action = live.next_action if live else A_PICK_DELIVERY
    if next_action == A_COMPLETED:
        return None
    state = dict(state)
    if state.get("chat_id") is None:
        state["chat_id"] = match.chat_id
    for side, uid in (("bat", match.batting_first_id or match.user1_id),
                      ("bowl", match.bowling_first_id or match.user2_id)):
        team_key, tg_key = f"{side}_team_id", f"{side}_user_tg"
        uid = state.setdefault(team_key, uid)
        if state.get(tg_key) is None and uid is not None:
            user = session.get(User, uid)
            if user is not None:
                state[tg_key] = user.telegram_id
    saved = save_snapshot(match.id, state, next_action, REASON_FORFEIT,
                          prev_status=match.status, session=session)
    _stop_live_state(live)
    return saved


def _stop_live_state(live):
    """Stop recovery from re-opening an interrupted turn before cleanup."""
    from services.match_state_store import A_COMPLETED

    if live is not None:
        live.next_action = A_COMPLETED
        live.version = (live.version or 0) + 1


def list_saved(tg_id, limit=10):
    """Resumable matches ``tg_id`` is a captain in, newest first."""
    from sqlalchemy import and_, or_
    from models import Match, SavedMatch, User
    session = _session()
    try:
        user = session.query(User.id).filter_by(telegram_id=tg_id).scalar_subquery()
        original_captain = (session.query(Match.id)
                            .filter(or_(Match.user1_id == user, Match.user2_id == user)))
        rows = (session.query(SavedMatch)
                .filter(SavedMatch.status == ST_SAVED,
                        or_(SavedMatch.user1_tg == tg_id,
                            SavedMatch.user2_tg == tg_id,
                            and_(SavedMatch.reason == REASON_FORFEIT,
                                 SavedMatch.match_id.in_(original_captain))))
                .order_by(SavedMatch.saved_at.desc())
                .limit(limit).all())
        return [_row_dict(r) for r in rows]
    finally:
        session.close()


def _set_status(mid, from_status, to_status, **extra):
    """Compare-and-swap the saved row's status. True when it moved."""
    from models import SavedMatch
    values = {SavedMatch.status: to_status}
    for k, v in extra.items():
        values[getattr(SavedMatch, k)] = v
    session = _session()
    try:
        q = session.query(SavedMatch).filter(SavedMatch.match_id == int(mid),
                                             SavedMatch.reason != REASON_FORFEIT)
        if isinstance(from_status, (tuple, list)):
            q = q.filter(SavedMatch.status.in_(tuple(from_status)))
        else:
            q = q.filter(SavedMatch.status == from_status)
        moved = q.update(values, synchronize_session=False)
        session.commit()
        return bool(moved)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def claim(mid):
    """Take a saved match for a resume. Only one resume can ever win it."""
    return _set_status(mid, ST_SAVED, ST_RESUMING)


def unclaim(mid):
    """A resume that failed part-way hands the save back."""
    return _set_status(mid, ST_RESUMING, ST_SAVED)


def finish_claim(mid):
    return _set_status(mid, ST_RESUMING, ST_RESUMED, resumed_at=datetime.utcnow())


def void(mid):
    """The save can never be played again (e.g. its fixture was played since)."""
    return _set_status(mid, (ST_SAVED, ST_RESUMING), ST_VOID)


def park_match_row(mid, status="paused", end_reason=None, by_user_id=None,
                   control_action_id=None, *, snapshot=None):
    """Take the live ``Match`` row out of play. Returns its previous status.

    When given a snapshot, save it in the same transaction and return the save.

    A tournament fixture stays reserved for it (``heal_live_fixtures`` leaves a
    fixture alone while its match is in any status it doesn't know as ended,
    and ``paused`` is one of those), so nobody can start the same fixture from
    scratch while this one waits to be finished.
    """
    from models import Match, MatchState
    from services.match_outcome import END_PAUSED, mark_end
    session = _session()
    try:
        m = session.query(Match).filter_by(id=int(mid)).with_for_update().first()
        if m is None:
            return None
        if (snapshot is not None or control_action_id is not None) and m.status not in _LIVE_STATUSES:
            return None
        prev = m.status
        saved = None
        if snapshot is not None:
            state, next_action, reason = snapshot
            saved = save_snapshot(mid, state, next_action, reason, by_user_id,
                                  prev_status=prev, session=session)
            _stop_live_state(session.query(MatchState).filter_by(match_id=mid).first())
        m.status = status
        m.completed_at = datetime.utcnow()
        m.winner_id = None
        m.loser_id = None
        if control_action_id is not None:
            from models import MatchControlAction, SavedMatch
            action = session.get(MatchControlAction, control_action_id)
            by_user_id = action.user_id
            snapshot = session.query(SavedMatch).filter_by(match_id=int(mid)).first()
            if snapshot is not None:
                snapshot.saved_by_id = by_user_id
        mark_end(m, end_reason or END_PAUSED, by_user_id=by_user_id)
        if control_action_id is not None:
            from services.match_control_service import finish_in_session
            finish_in_session(session, control_action_id, {"match_ids": [int(mid)]})
        session.commit()
        return saved if snapshot is not None else prev
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def discard(mid, by_user_id=None):
    """Throw a save away for good and free whatever it was holding.

    The ``Match`` row is closed as abandoned (it never reached a result), a
    tournament fixture it still held goes back to ``scheduled`` and a CL Tour
    slot back to ``pending``, so the pairing can be played again from scratch.
    Returns True when a resumable save was discarded.
    """
    from models import Match, SavedMatch
    session = _session()
    try:
        row = (session.query(SavedMatch)
               .filter(SavedMatch.match_id == int(mid),
                       SavedMatch.reason != REASON_FORFEIT,
                       SavedMatch.status == ST_SAVED).first())
        if row is None:
            return False
        row.status = ST_DISCARDED
        try:
            state = json.loads(row.state_json or "{}")
        except (TypeError, ValueError):
            state = {}
        m = session.get(Match, int(mid))
        if m is not None and m.status not in _LIVE_STATUSES and m.status != "completed":
            m.status = "abandoned"
            m.completed_at = m.completed_at or datetime.utcnow()
            if by_user_id is not None:
                m.ended_by_id = by_user_id
        _release_holds(session, int(mid), state)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _release_holds(session, mid, state):
    """Hand back a fixture / tour slot still bound to match ``mid``."""
    championship = state.get("t20_championship")
    if championship:
        from services import t20_championship
        t20_championship.release(session, championship["fixture_id"], mid)
    rfx = state.get("reserved_fixture_id")
    if rfx:
        from models import TournamentMatch
        (session.query(TournamentMatch)
         .filter(TournamentMatch.id == int(rfx),
                 TournamentMatch.status == "live",
                 TournamentMatch.match_id == mid)
         .update({TournamentMatch.status: "scheduled",
                  TournamentMatch.match_id: None},
                 synchronize_session=False))
    slot = state.get("cl_tour_match_id")
    if slot:
        from models import CLTourMatch
        (session.query(CLTourMatch)
         .filter(CLTourMatch.id == int(slot),
                 CLTourMatch.status == "playing",
                 CLTourMatch.match_id == mid)
         .update({CLTourMatch.status: "pending", CLTourMatch.match_id: None},
                 synchronize_session=False))


class ReviveError(Exception):
    """A saved match could not be put back in play.

    ``permanent`` means it never will be (the fixture has been played, the
    tournament is over) — the save is voided rather than handed back.
    """

    def __init__(self, message, permanent=False):
        Exception.__init__(self, message)
        self.message = message
        self.permanent = permanent


def restore_forfeited_match(mid):
    """Create one unranked continuation of a settled forfeit, atomically."""
    from sqlalchemy import or_
    from models import BotChat, Match, MatchState, SavedMatch, User
    from services.match_handover import controlled_match
    from services.match_state_store import A_COMPLETED, _deserialize, serialize_state

    session = _session()
    try:
        row = session.query(SavedMatch).filter_by(
            match_id=mid, reason=REASON_FORFEIT).with_for_update().first()
        if row is None:
            raise ReviveError(f"No restorable forfeit was saved for match #{mid}.")
        if row.status != ST_SAVED:
            raise ReviveError(f"Match #{mid} has already been restored.")
        original = session.get(Match, mid)
        state = _deserialize(row.state_json)
        if (not original or original.status != "completed" or not state
                or not row.next_action or row.next_action == A_COMPLETED):
            raise ReviveError(f"Match #{mid} has no playable saved position.")
        chat_id = row.chat_id or original.chat_id
        if not chat_id and state.get("played_via") != "webapp":
            raise ReviveError(f"Match #{mid} has no original chat.")

        claimed = session.query(SavedMatch).filter_by(
            id=row.id, status=ST_SAVED, reason=REASON_FORFEIT).update(
            {"status": ST_RESUMED, "resumed_at": datetime.utcnow()},
            synchronize_session=False)
        if not claimed:
            raise ReviveError(f"Match #{mid} has already been restored.")

        # Serialize restores that share a chat or captain before checking for a
        # conflicting match. The claim rolls back if the restore cannot go ahead.
        if chat_id:
            session.query(BotChat.id).filter_by(chat_id=chat_id).with_for_update().first()
        humans = set(human_user_ids(state)) | {original.user1_id, original.user2_id}
        humans.discard(state.get("bot_user_id"))
        session.query(User.id).filter(User.id.in_(humans)).order_by(User.id).with_for_update().all()
        busy = session.query(Match).filter(
            Match.status.in_(_LIVE_STATUSES),
            or_(Match.expires_at.is_(None), Match.expires_at >= datetime.utcnow(),
                Match.status.in_(("playing", "active", "in_progress"))))
        if chat_id and busy.filter(Match.chat_id == chat_id).first():
            raise ReviveError("A match is already active in the original chat.")
        if (busy.filter(or_(Match.user1_id.in_(humans), Match.user2_id.in_(humans))).first()
                or any(controlled_match(session, uid) for uid in humans)):
            raise ReviveError("One of the captains is already in another match.")

        restored = Match(
            user1_id=original.user1_id, user2_id=original.user2_id,
            status=(row.prev_status if row.prev_status in _LIVE_STATUSES else "active"),
            match_type=original.match_type, overs=original.overs,
            ball_format=original.ball_format, toss_winner_id=original.toss_winner_id,
            toss_decision=original.toss_decision, batting_first_id=original.batting_first_id,
            bowling_first_id=original.bowling_first_id, stadium=original.stadium,
            pitch_type=original.pitch_type, weather=original.weather,
            temperature=original.temperature, umpire1=original.umpire1,
            umpire2=original.umpire2, chat_id=chat_id)
        session.add(restored)
        session.flush()
        new_mid = restored.id

        # The original match has already settled any competition and rewards.
        # Keep the lineups and gameplay position for the continuation.
        for key in list(state):
            if key.startswith(("tournament_", "cl_tour_", "tour_", "_planned_")):
                state.pop(key)
        for key in ("auction_league", "reserved_fixture_id", "t20_championship",
                    "result_msg_id", "match_result", "result_finalized",
                    "idle_saves", "idle_saved_tg"):
            state.pop(key, None)
        state.update(match_id=new_mid, chat_id=chat_id, original_lobby_chat_id=chat_id,
                     is_private=bool(chat_id and chat_id > 0), prompt_delivered=False,
                     unranked=True, stats_disabled=True, restored_from_forfeit=mid)
        session.add(MatchState(match_id=new_mid, state_json=serialize_state(state),
                               next_action=row.next_action, version=1))
        title = row.title
        session.commit()
        return new_mid, state, title
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def revive_match(mid, chat_id, state):
    """Put the ``Match`` row for ``mid`` back in play in ``chat_id``.

    Re-reserves what the match was holding when it stopped: its tournament
    fixture (``scheduled`` → ``live``, bound to this match) and its CL Tour slot
    (``pending`` → ``playing``). A fixture that has since been played or taken
    by another match makes the save unplayable — :class:`ReviveError` with
    ``permanent=True``. All in one transaction: either everything is back, or
    nothing moved.
    """
    from models import Match, Tournament, TournamentMatch
    session = _session()
    try:
        m = session.get(Match, int(mid))
        if m is None:
            raise ReviveError("That match no longer exists.", permanent=True)
        if (m.status or "") in _LIVE_STATUSES:
            raise ReviveError("That match is already being played.")

        championship = state.get("t20_championship")
        if championship:
            from models import T20ChampionshipFixture, User
            from services import t20_championship
            t20_championship.lock(session, championship["championship_id"])
            fixture = session.get(T20ChampionshipFixture, championship["fixture_id"], populate_existing=True)
            if fixture is None or fixture.status in t20_championship.TERMINAL:
                raise ReviveError("That championship fixture is closed.", permanent=True)
            if fixture.status == "live" and fixture.match_id != int(mid):
                raise ReviveError("That championship fixture is already in progress.")
            if fixture.status != "live":
                host, target = (session.get(User, uid) for uid in championship["users"])
                if not host or not target:
                    raise ReviveError("One of the original captains is unavailable.", permanent=True)
                try:
                    t20_championship.bind(session, championship, m, host, target)
                except t20_championship.FixtureUnavailable as exc:
                    raise ReviveError(str(exc)) from exc

        tid = state.get("tournament_id")
        if tid:
            tour = session.get(Tournament, int(tid))
            if tour is None or (tour.status or "") in ("completed", "cancelled"):
                raise ReviveError(
                    "That tournament is over — its matches can't be resumed.",
                    permanent=True)
        rfx = state.get("reserved_fixture_id")
        if rfx:
            fx = session.get(TournamentMatch, int(rfx))
            if fx is None:
                raise ReviveError("That fixture was removed from the schedule.",
                                  permanent=True)
            if fx.status == "live" and fx.match_id not in (None, int(mid)):
                raise ReviveError("That fixture is being played in another match.",
                                  permanent=True)
            if fx.status == "live":
                fx.match_id = int(mid)
            elif fx.status == "scheduled":
                claimed = (session.query(TournamentMatch)
                           .filter(TournamentMatch.id == fx.id,
                                   TournamentMatch.status == "scheduled")
                           .update({TournamentMatch.status: "live",
                                    TournamentMatch.match_id: int(mid)},
                                   synchronize_session=False))
                if not claimed:
                    raise ReviveError("That fixture was just taken — try again.")
            else:
                raise ReviveError("That fixture has already been played.",
                                  permanent=True)
        slot_id = state.get("cl_tour_match_id")
        if slot_id:
            from models import CLTourMatch
            slot = session.get(CLTourMatch, int(slot_id))
            if slot is None:
                raise ReviveError("That tour match no longer exists.", permanent=True)
            if slot.status == "playing" and slot.match_id == int(mid):
                pass
            elif slot.status == "pending" and not slot.match_id:
                slot.status = "playing"
                slot.match_id = int(mid)
            else:
                raise ReviveError("That tour match has moved on without this game.",
                                  permanent=True)

        m.status = "active"
        m.completed_at = None
        m.winner_id = None
        m.loser_id = None
        m.margin_type = None
        m.margin_value = None
        m.end_reason = None
        m.ended_by_id = None
        if chat_id is not None:
            m.chat_id = chat_id
        session.commit()
    except ReviveError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
