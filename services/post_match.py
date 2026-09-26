"""The post-match layer: ranked ladder and rivalry.

Every flow that finishes a match with a result calls
:func:`process_completed_match` inside its own finalize transaction, right
after it has written the winner onto the ``Match`` row. That one call:

  1. rates the match on the ranked ladder (``services.ranked_service``),
  2. folds it into the pair's rivalry (``services.rivalry_service``),

and stores what happened in a ``MatchPostResult`` row, keyed on the match id,
so a replayed finalize changes nothing the second time. After the caller has
committed, :func:`announce` posts the short "📈 Ranked · ⚔️ Rivalry" card
into the match chat.

Everything is best-effort: the work runs in a savepoint and any failure is
logged and swallowed, because a match that has already been paid out must never
be held up by its own bookkeeping.
"""

import asyncio
import html
import json
import logging

logger = logging.getLogger(__name__)


def _team_owner(state):
    """``{team name: user id}`` from a live match state (both innings' sides)."""
    owners = {}
    if not isinstance(state, dict):
        return owners
    for side in ("bat", "bowl"):
        name, uid = state.get(f"{side}_team_name"), state.get(f"{side}_team_id")
        if name and uid:
            owners[str(name)] = uid
    return owners


def _innings_owner(state, match):
    """``[user who batted first, user who batted second]`` or None.

    An id per innings, so the Hall of Fame can tell two captains apart even
    when they field the same team name.
    """
    if not isinstance(state, dict):
        return None
    players = (match.user1_id, match.user2_id)
    first = state.get("inn1_bat_team_id")
    if first is None and int(state.get("innings") or 1) == 2:
        first = state.get("bowl_team_id")      # the side now bowling batted first
    if first not in players:
        return None
    return [first, match.user2_id if first == match.user1_id else match.user1_id]


def _humans(session, match):
    from models import User
    from services.match_rewards import is_ai_user
    return all(not is_ai_user(session.get(User, uid))
               for uid in (match.user1_id, match.user2_id))


def process_completed_match(session, match, *, count_result=True, state=None):
    """Run the post-match layer for ``match``. Returns the summary dict or None.

    Call inside the finalize transaction after the result is written; the
    caller commits. Idempotent on ``match.id``.
    """
    if match is None or not match.id:
        return None
    from models import MatchPostResult
    try:
        with session.begin_nested():
            row = (session.query(MatchPostResult)
                   .filter(MatchPostResult.match_id == match.id).first())
            if row is not None and row.ranked_done:
                try:
                    return json.loads(row.payload_json or "{}")
                except Exception:
                    return None
            if row is None:
                row = MatchPostResult(match_id=match.id, counted=bool(count_result),
                                      ranked_done=False, rated=False, hof_done=False)
                session.add(row)
            else:
                row.counted = bool(count_result)

            tie = (match.margin_type == "tie"
                   or (match.winner_id is None and match.loser_id is None))
            summary = {"match_id": match.id, "user1_id": match.user1_id,
                       "user2_id": match.user2_id, "winner_id": match.winner_id,
                       "tie": tie, "team_owner": _team_owner(state),
                       "innings_owner": _innings_owner(state, match),
                       "ranked": None, "rivalry": None}

            if count_result and _humans(session, match):
                from services import ranked_service, rivalry_service
                if (match.overs or 20) < ranked_service.MIN_RANKED_OVERS:
                    summary["ranked"] = {
                        "rated": False, "players": {},
                        "reason": (f"matches under {ranked_service.MIN_RANKED_OVERS}"
                                   f" overs don't count")}
                else:
                    summary["ranked"] = ranked_service.apply_result(
                        session, match.user1_id, match.user2_id,
                        winner_id=match.winner_id, tie=tie, match_id=match.id)
                summary["rivalry"] = rivalry_service.record_match(session, match)

            row.rated = bool((summary["ranked"] or {}).get("rated"))
            row.ranked_done = True
            row.payload_json = json.dumps(summary, default=str)
            session.flush()
            return summary
    except Exception:
        logger.exception("post-match processing failed for match %s", match.id)
        return None


# ── chat card ────────────────────────────────────────────────────────

def _name(users, uid):
    u = users.get(uid) if uid else None
    if u is None:
        return "Player"
    return html.escape((u.first_name or "").strip() or (u.team_name or "").strip()
                       or (u.username or "Player"))


def render_card(summary, users):
    """HTML lines for the post-match card. ``users`` maps id → User. Pure."""
    if not summary:
        return ""
    lines = []
    ranked = summary.get("ranked") or {}
    if ranked.get("rated"):
        parts = []
        for uid, p in (ranked.get("players") or {}).items():
            uid = int(uid)
            sign = "+" if p["delta"] >= 0 else ""
            move = ""
            if p.get("promoted"):
                move = f" ⬆️ <b>{p['division']}!</b>"
            elif p.get("relegated"):
                move = f" ⬇️ {p['division']}"
            parts.append(f"{_name(users, uid)} {p['after']} "
                         f"({sign}{p['delta']}) {p.get('emoji', '')}{move}")
        lines.append("📈 <b>Ranked</b> · " + "  ·  ".join(parts))
    elif ranked.get("reason") and ("daily cap" in ranked["reason"]
                                   or "overs don't count" in ranked["reason"]):
        lines.append(f"📈 <i>Unrated — {html.escape(ranked['reason'], quote=False)}.</i>")

    riv = summary.get("rivalry") or {}
    if riv.get("is_rivalry"):
        a, b = riv["a_id"], riv["b_id"]
        title = html.escape(riv.get("name") or "Rivalry")
        if riv.get("just_formed"):
            lines.append(f"🔥 <b>A rivalry is born — {title}!</b>")
        score = (f"{_name(users, a)} <b>{riv['a_wins']}</b> – "
                 f"<b>{riv['b_wins']}</b> {_name(users, b)}")
        if riv.get("ties"):
            score += f" ({riv['ties']} tied)"
        lines.append(f"⚔️ <b>{title}</b>: {score}")
        rr = riv.get("round_result")
        if rr:
            if rr.get("winner_id"):
                lines.append(
                    f"🏅 Round {rr['round_no']} to {_name(users, rr['winner_id'])} "
                    f"({max(rr['a_wins'], rr['b_wins'])}–{min(rr['a_wins'], rr['b_wins'])})"
                    f" · +{rr['coins']:,} coins, +{rr['gems']} 💎")
            else:
                lines.append(f"🤝 Round {rr['round_no']} drawn "
                             f"{rr['a_wins']}–{rr['b_wins']} — no bonus.")
        elif riv.get("round"):
            r = riv["round"]
            lines.append(f"🔁 Round {r['round_no']}: {r['a_wins']}–{r['b_wins']} "
                         f"after {r['played']}/5")
        if riv.get("win_bonus"):
            lines.append(f"💰 Rivalry win bonus: +{riv['win_bonus']['coins']:,} coins "
                         f"to {_name(users, riv['win_bonus']['user_id'])}")
        if (riv.get("streak") or 0) >= 3 and riv.get("streak_user_id"):
            lines.append(f"🔥 {_name(users, riv['streak_user_id'])} have won "
                         f"{riv['streak']} in a row")
    elif riv and riv.get("played"):
        from services.rivalry_service import RIVALRY_THRESHOLD
        left = RIVALRY_THRESHOLD - riv["played"]
        if 0 < left <= 2:
            lines.append(f"⚔️ <i>{left} more meeting{'s' if left > 1 else ''} and "
                         f"this becomes a rivalry.</i>")

    return "\n".join(lines)


def _load_card(match_id):
    from database import get_session
    from models import MatchPostResult, User
    session = get_session()
    try:
        row = (session.query(MatchPostResult)
               .filter(MatchPostResult.match_id == match_id).first())
        if row is None or not row.payload_json:
            return ""
        summary = json.loads(row.payload_json)
        ids = {summary.get("user1_id"), summary.get("user2_id")}
        for extra in (summary.get("rivalry") or {}).get("streak_user_id"), \
                ((summary.get("rivalry") or {}).get("win_bonus") or {}).get("user_id"), \
                ((summary.get("rivalry") or {}).get("round_result") or {}).get("winner_id"):
            ids.add(extra)
        ids.discard(None)
        users = {u.id: u for u in session.query(User).filter(User.id.in_(ids)).all()}
        return render_card(summary, users)
    except Exception:
        logger.exception("post-match card load failed for match %s", match_id)
        return ""
    finally:
        session.close()


async def announce(bot, chat_id, match_id):
    """Post the post-match card into ``chat_id`` (no-op when there is nothing)."""
    if not chat_id or not match_id:
        return None
    try:
        text = await asyncio.to_thread(_load_card, match_id)
        if not text:
            return None
        return await bot.send_message(chat_id, text, parse_mode="HTML",
                                      disable_web_page_preview=True)
    except Exception:
        logger.exception("post-match announce failed for match %s", match_id)
        return None
