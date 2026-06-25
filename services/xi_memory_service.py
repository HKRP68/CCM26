"""Remember-my-XI service — persists a user's last confirmed Playing XI per team.

Used by the CIPL Challenge League XI picker (handlers/challenge.py) so a captain
who selects, say, RCB sees their last RCB XI offered as a one-tap "Use last XI"
instead of re-tapping 11 players every match. Storage is keyed by
``(user_tg_id, team_id)`` via the ``UserTeamLastXI`` table, so each team a user
captains keeps its own independent saved lineup.

All functions are best-effort: a persistence failure must never interrupt the
match flow, so writes catch and log rather than raise.
"""

import json
import logging

from database import get_session

logger = logging.getLogger(__name__)


def save_last_xi(tg_id, team_id, player_ids):
    """Upsert the user's last confirmed XI for ``team_id``.

    ``player_ids`` is an ordered list of ChallengePlayer ids (batting order).
    Returns True on success, False on any failure (logged, never raised).
    """
    if not tg_id or not team_id or not player_ids:
        return False
    from models import UserTeamLastXI
    payload = json.dumps([int(pid) for pid in player_ids])
    session = get_session()
    try:
        row = (session.query(UserTeamLastXI)
               .filter(UserTeamLastXI.user_tg_id == tg_id,
                       UserTeamLastXI.team_id == team_id)
               .first())
        if row is None:
            row = UserTeamLastXI(user_tg_id=tg_id, team_id=team_id, player_ids=payload)
            session.add(row)
        else:
            row.player_ids = payload
        session.commit()
        return True
    except Exception:
        logger.exception("Failed to save last XI for tg=%s team=%s", tg_id, team_id)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def load_last_xi(tg_id, team_id):
    """Return the saved ordered list of ChallengePlayer ids, or [] if none."""
    if not tg_id or not team_id:
        return []
    from models import UserTeamLastXI
    session = get_session()
    try:
        row = (session.query(UserTeamLastXI)
               .filter(UserTeamLastXI.user_tg_id == tg_id,
                       UserTeamLastXI.team_id == team_id)
               .first())
        if not row or not row.player_ids:
            return []
        ids = json.loads(row.player_ids)
        return [int(pid) for pid in ids]
    except Exception:
        logger.exception("Failed to load last XI for tg=%s team=%s", tg_id, team_id)
        return []
    finally:
        session.close()
