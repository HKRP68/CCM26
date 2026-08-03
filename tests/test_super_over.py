"""End-to-end test for the tied-match Super Over (handlers/super_over.py).

Drives a full Super Over with fake Telegram objects: tie kickoff → player
selection (owner-gated) → interactive ball-by-ball innings (delivery/length/shot
picks) → result, including the tied-Super-Over replay loop. Asserts the special
rules hold (≤6 legal balls, ≤2 wickets per innings, a winner is always reached).
"""

import asyncio
import random
from types import SimpleNamespace

import handlers.super_over as so_mod
from handlers.super_over import (
    start_super_over, _get,
    so_bat_callback, so_batok_callback, so_bowl_callback, so_bowlok_callback,
    so_deliv_callback, so_len_callback, so_shot_callback,
    _eligible_batters, _eligible_bowlers,
)

CHAT = -100123
HOST_TG = 111      # batted 1st in main (bowls first in SO1)
GUEST_TG = 222     # batted 2nd in main (bats first in SO1)
HOST_UID = 1
GUEST_UID = 2

so_mod._BALL_PAUSE = 0  # no broadcast delay in tests

# The ball engine reads the admin sim-tuning config on every delivery. In
# production that is one cached dict lookup; with no database behind the tests
# every ball would open its own session and fall back, which is both slow and
# leaves the outcome at the mercy of whatever config happens to be present.
# Prime the cache with the defaults so the tests measure the engine itself.
try:
    import services.config_service as _config_service
    if _config_service._CACHE.get("data") is None:
        _config_service._CACHE["data"] = dict(_config_service.DEFAULTS)
except Exception:  # pragma: no cover - defensive
    pass


# ── Fakes ────────────────────────────────────────────────────────────

class FakeBot:
    def __init__(self):
        self._mid = 5000
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None,
                           reply_markup=None, **kwargs):
        self._mid += 1
        m = SimpleNamespace(message_id=self._mid, text=text,
                            reply_markup=reply_markup, chat_id=chat_id)
        self.messages.append(m)
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                parse_mode=None, reply_markup=None, **kwargs):
        m = SimpleNamespace(message_id=message_id, text=text,
                            reply_markup=reply_markup, chat_id=chat_id)
        self.messages.append(m)
        return m

    async def send_photo(self, chat_id, photo=None, caption=None,
                         parse_mode=None, reply_markup=None, **kwargs):
        self._mid += 1
        m = SimpleNamespace(message_id=self._mid, text=caption,
                            reply_markup=reply_markup, chat_id=chat_id,
                            photo=photo, is_photo=True)
        self.messages.append(m)
        return m

    async def edit_message_reply_markup(self, chat_id=None, message_id=None,
                                        reply_markup=None, **kwargs):
        self.messages.append(SimpleNamespace(
            message_id=message_id, text=None, reply_markup=reply_markup,
            chat_id=chat_id))
        return None

    async def delete_message(self, *a, **k):
        pass


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.bot_data = {}


class FakeQuery:
    def __init__(self, data, from_id):
        self.data = data
        self.from_user = SimpleNamespace(id=from_id)
        self.message = SimpleNamespace(chat_id=CHAT)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        return SimpleNamespace(message_id=1, text=text, reply_markup=reply_markup)


def _upd(q):
    return SimpleNamespace(callback_query=q)


# ── Fixtures: a tied cipl-style match state ───────────────────────────

def _player(rid, name, bat, bowl, style="Fast", category="All-rounder"):
    return {
        "roster_id": rid, "player_id": rid, "name": name,
        "rating": max(bat, bowl), "category": category,
        "bat_rating": bat, "bowl_rating": bowl,
        "bowl_style": style, "bowl_hand": "Right", "bat_hand": "Right",
    }


def _xi(prefix, base):
    # 5 players each: enough for 3-batter / 1-bowler picks plus restrictions.
    styles = ["Fast", "Off Spinner", "Medium Pacer", "Leg Spinner", "Fast"]
    return [
        _player(base + i, f"{prefix}{i}", 70 + i, 60 + i, styles[i])
        for i in range(5)
    ]


def _tied_state():
    host_xi = _xi("H", 100)    # host batted 1st (bowl side now)
    guest_xi = _xi("G", 200)   # guest batted 2nd (bat side now)
    return {
        "match_id": 9001,
        "chat_id": CHAT,
        "overs": 20,
        "pitch_type": "Hard",
        "is_letsplay": False,
        # post-innings-2 roles: bat side = team that batted 2nd
        "bat_team_id": GUEST_UID, "bowl_team_id": HOST_UID,
        "bat_user_tg": GUEST_TG, "bowl_user_tg": HOST_TG,
        "bat_team_name": "Guest XI", "bowl_team_name": "Host XI",
        "bat_team_code": "GUE", "bowl_team_code": "HOS",
        "bat_team_emoji": "🟦", "bowl_team_emoji": "🟥",
        "bat_xi": guest_xi, "bowl_xi": host_xi,
        "inn1_bat_team": "Host XI",
        "inn1_runs": 150, "inn1_wickets": 6,
        "total_runs": 150, "total_wickets": 8,
    }


# ── Drivers ───────────────────────────────────────────────────────────

async def _do_selection(ctx, mid):
    """Both owners select their players for the current innings."""
    so = _get(ctx, mid)
    bat_uid, bowl_uid = so["bat_uid"], so["bowl_uid"]
    bat_tg = so["teams"][bat_uid]["tg"]
    bowl_tg = so["teams"][bowl_uid]["tg"]

    batters = _eligible_batters(so, bat_uid)
    need = min(3, len(batters))
    for p in batters[:need]:
        rid = int(p["roster_id"])
        await so_bat_callback(_upd(FakeQuery(f"so_bat_{mid}_{rid}", bat_tg)), ctx)
    await so_batok_callback(_upd(FakeQuery(f"so_batok_{mid}", bat_tg)), ctx)

    bowler = _eligible_bowlers(so, bowl_uid)[0]
    brid = int(bowler["roster_id"])
    await so_bowl_callback(_upd(FakeQuery(f"so_bowl_{mid}_{brid}", bowl_tg)), ctx)
    await so_bowlok_callback(_upd(FakeQuery(f"so_bowlok_{mid}", bowl_tg)), ctx)


async def _bowl_one_ball(ctx, mid):
    """Drive one delivery → (length) → shot, asserting the special rules."""
    so = _get(ctx, mid)
    inn = so["inn"]
    bat_tg = so["teams"][so["bat_uid"]]["tg"]
    bowl_tg = so["teams"][so["bowl_uid"]]["tg"]

    assert inn["legal"] <= 6
    assert inn["wickets"] <= 2

    # Delivery (always)
    await so_deliv_callback(_upd(FakeQuery(f"so_dv_{mid}_0", bowl_tg)), ctx)
    so = _get(ctx, mid)
    if not so or "inn" not in so:
        return
    if so["inn"]["stage"] == "LEN":
        await so_len_callback(_upd(FakeQuery(f"so_ln_{mid}_0", bowl_tg)), ctx)
    # Shot (resolves the ball)
    await so_shot_callback(_upd(FakeQuery(f"so_sh_{mid}_0", bat_tg)), ctx)


async def _play_match(ctx, mid):
    guard = 0
    while True:
        guard += 1
        assert guard < 4000, "Super Over did not terminate"
        so = _get(ctx, mid)
        if so is None:
            break  # finalised + cleaned up
        if not (so.get("bat_confirmed") and so.get("bowl_confirmed")):
            await _do_selection(ctx, mid)
            continue
        if "inn" in so:
            # rule invariants every ball
            assert so["inn"]["legal"] <= 6
            assert so["inn"]["wickets"] <= 2
            await _bowl_one_ball(ctx, mid)


# ── Tests ─────────────────────────────────────────────────────────────

def _patch_finalize(monkeypatch_target):
    """Replace DB-touching bits so the test runs without a database."""
    captured = {}

    def fake_persist(mid, state):
        captured["persisted"] = True

    async def fake_send(*a, **k):
        return None

    so_mod._persist_main_stats = fake_persist

    # Patch the reward + Match-row work inside _finalize via a stub get_session.
    class _Q:
        def get(self, _):
            return SimpleNamespace()

    class _S:
        def query(self, *a, **k):
            return _Q()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    so_mod.get_session = lambda: _S()

    # award_match_rewards_core + cleanup_state are imported lazily inside funcs;
    # neutralise them through the modules they come from.
    import services.match_rewards as mr
    mr.award_match_rewards_core = lambda *a, **k: (0, 0, 0, 0)
    import services.match_state_store as mss
    mss.cleanup_state = lambda *a, **k: None
    # Skip real PIL scorecard rendering by default (fast); the dedicated image
    # test re-stubs these to return bytes to assert the send flow.
    import handlers.cipl_play as cipl_play
    cipl_play._build_cipl_summary_image = lambda state, result: None
    so_mod._build_super_over_card = lambda so: None
    return captured


def test_super_over_full_flow_decides_a_winner():
    random.seed(7)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]

    started = asyncio.run(start_super_over(ctx, mid, state))
    assert started is True
    so = _get(ctx, mid)
    assert so is not None
    # Team that batted 2nd in the main match bats first in the Super Over.
    assert so["first_bat_uid"] == GUEST_UID
    assert so["bat_uid"] == GUEST_UID and so["bowl_uid"] == HOST_UID

    asyncio.run(_play_match(ctx, mid))

    # On completion the Super Over state is cleaned up and a Match row finalised.
    assert _get(ctx, mid) is None
    # A winner/result was announced.
    texts = "\n".join(m.text for m in ctx.bot.messages if m.text)
    assert "SUPER OVER RESULT" in texts
    assert "Match Winner" in texts
    # Result delivered via the main scorecard image caption, or the text
    # fallback when image rendering is unavailable.
    assert ("won the match by" in texts) or ("MATCH RESULT" in texts)


def test_owner_gating_blocks_wrong_user():
    random.seed(1)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))
    so = _get(ctx, mid)

    # The bowling owner (HOST) must not be able to pick the batting side's batters.
    bat_uid = so["bat_uid"]
    rid = int(so["teams"][bat_uid]["xi"][0]["roster_id"])
    asyncio.run(so_bat_callback(
        _upd(FakeQuery(f"so_bat_{mid}_{rid}", HOST_TG)), ctx))
    assert so["sel_batters"] == []  # rejected — wrong owner


def test_a_batter_who_already_batted_cannot_be_reselected():
    random.seed(3)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))
    so = _get(ctx, mid)

    bat_uid = so["bat_uid"]                       # GUEST
    bat_tg = so["teams"][bat_uid]["tg"]
    # Pretend this batter came to the crease in an earlier Super Over of this
    # tie — they are spent and cannot bat again.
    gone = int(so["teams"][bat_uid]["xi"][0]["roster_id"])
    so["batted"][bat_uid].add(gone)

    # A stale button tap for the used player must be rejected.
    asyncio.run(so_bat_callback(
        _upd(FakeQuery(f"so_bat_{mid}_{gone}", bat_tg)), ctx))
    assert gone not in so["sel_batters"]

    # An eligible player is still accepted.
    ok_rid = int(so["teams"][bat_uid]["xi"][1]["roster_id"])
    asyncio.run(so_bat_callback(
        _upd(FakeQuery(f"so_bat_{mid}_{ok_rid}", bat_tg)), ctx))
    assert ok_rid in so["sel_batters"]


def test_ineligible_bowler_tap_is_rejected():
    from handlers.super_over import so_bowl_callback, _eligible_bowlers
    random.seed(4)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))
    so = _get(ctx, mid)

    bowl_uid = so["bowl_uid"]                      # HOST
    bowl_tg = so["teams"][bowl_uid]["tg"]
    # Mark a bowler who has already bowled a Super Over in this tie.
    restricted = int(so["teams"][bowl_uid]["xi"][0]["roster_id"])
    so["used_bowlers"][bowl_uid].add(restricted)

    # A stale button for the restricted bowler must be rejected.
    asyncio.run(so_bowl_callback(
        _upd(FakeQuery(f"so_bowl_{mid}_{restricted}", bowl_tg)), ctx))
    assert so["sel_bowler"] is None

    # An eligible bowler is accepted.
    ok = int(_eligible_bowlers(so, bowl_uid)[0]["roster_id"])
    asyncio.run(so_bowl_callback(
        _upd(FakeQuery(f"so_bowl_{mid}_{ok}", bowl_tg)), ctx))
    assert so["sel_bowler"] == ok


def test_double_tap_shot_resolves_one_ball():
    random.seed(5)
    _patch_finalize(so_mod)
    # Force every ball to a dot so the innings can't end on the first delivery.
    orig = so_mod._super_over_ball
    so_mod._super_over_ball = lambda *a, **k: {
        "type": "run", "runs": 0, "wicket_type": None,
        "is_extra": False, "batter_out": False, "traits_activated": []}
    try:
        ctx = FakeContext()
        state = _tied_state()
        mid = state["match_id"]
        asyncio.run(start_super_over(ctx, mid, state))
        asyncio.run(_do_selection(ctx, mid))   # innings 1 starts, stage DELIV
        so = _get(ctx, mid)
        bat_tg = so["teams"][so["bat_uid"]]["tg"]
        bowl_tg = so["teams"][so["bowl_uid"]]["tg"]

        # Advance to the SHOT stage.
        asyncio.run(so_deliv_callback(_upd(FakeQuery(f"so_dv_{mid}_0", bowl_tg)), ctx))
        if so["inn"]["stage"] == "LEN":
            asyncio.run(so_len_callback(_upd(FakeQuery(f"so_ln_{mid}_0", bowl_tg)), ctx))
        assert so["inn"]["stage"] == "SHOT"

        before = so["inn"]["deliveries"]
        # Two taps on the same shot button — only one must resolve.
        asyncio.run(so_shot_callback(_upd(FakeQuery(f"so_sh_{mid}_0", bat_tg)), ctx))
        asyncio.run(so_shot_callback(_upd(FakeQuery(f"so_sh_{mid}_0", bat_tg)), ctx))
        assert so["inn"]["deliveries"] == before + 1
    finally:
        so_mod._super_over_ball = orig


def test_resume_super_over_reposts_prompt():
    from handlers.super_over import find_super_over_in_chat, resume_super_over
    random.seed(2)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))

    # Found by chat id (what /resume and /rcl use).
    assert find_super_over_in_chat(ctx.bot_data, CHAT) == mid

    # During selection: resume re-sends the selection message.
    n_before = len(ctx.bot.messages)
    assert asyncio.run(resume_super_over(ctx, mid)) is True
    assert len(ctx.bot.messages) > n_before
    so = _get(ctx, mid)
    assert so["sel_msg_id"] is not None

    # During an innings: resume re-posts the current ball prompt and doesn't
    # advance the ball.
    asyncio.run(_do_selection(ctx, mid))
    so = _get(ctx, mid)
    assert "inn" in so
    deliveries_before = so["inn"]["deliveries"]
    assert asyncio.run(resume_super_over(ctx, mid)) is True
    assert so["inn"]["deliveries"] == deliveries_before  # no extra ball
    assert so["inn"]["stage"] in ("DELIV", "LEN", "SHOT")


def test_super_over_buttons_are_shared_for_both_captains():
    # The owner-access middleware must treat every Super Over button as a shared
    # prompt (each callback validates the clicker itself), otherwise the side
    # that didn't trigger the send gets "This button is not for you".
    from services.button_access import is_shared_callback_data
    for data in (f"so_bat_{1}_{2}", "so_batok_1", "so_bowl_1_2", "so_bowlok_1",
                 "so_dv_1_0", "so_ln_1_0", "so_sh_1_0"):
        assert is_shared_callback_data(data), data


# ── What actually decides a Super Over: ratings, then traits ──────────
#
# The Super Over is a 1-over /playmatch, so it runs on the /playmatch per-ball
# engine. These tests pin down the three properties that make it a fair decider
# rather than a lottery: ratings dominate, traits apply (as they do in the
# /letsplay match that produced the tie), and nobody gets a handicap.

def _bat_one_over(bat, bowl, n=4000, pitch="Hard"):
    """Mean runs from n single deliveries — a cheap proxy for a Super Over."""
    from handlers.super_over import _super_over_ball
    from services.sim_match import _adapt_player
    total = 0
    for _ in range(n):
        oc = _super_over_ball(
            _adapt_player(bat), _adapt_player(bowl), pitch,
            shot="Drive", variation="Stock Ball", length="Good Length")
        if oc.get("type") == "wicket":
            total -= 6          # a dismissal is the cost that matters most
        else:
            total += int(oc.get("runs", 0))
    return total / n


def _so_player(name, bat, bowl, traits=None):
    return {"roster_id": 1, "player_id": 1, "name": name, "rating": max(bat, bowl),
            "category": "All-rounder", "bat_rating": bat, "bowl_rating": bowl,
            "bowl_style": "Fast", "bowl_hand": "Right", "bat_hand": "Right",
            "traits": traits or []}


def test_a_better_batter_scores_more_in_the_super_over():
    # The old super-over matrix blended ratings so weakly that a 90-rated batter
    # scored the same as a 60-rated one. Rating must visibly decide it.
    bowler = _so_player("Bowl", 30, 75)
    random.seed(3)
    weak = _bat_one_over(_so_player("Weak", 55, 30), bowler)
    random.seed(3)
    strong = _bat_one_over(_so_player("Strong", 92, 30), bowler)
    assert strong > weak + 0.5, f"weak={weak:.2f} strong={strong:.2f}"


def test_a_better_bowler_concedes_less_in_the_super_over():
    batter = _so_player("Bat", 78, 30)
    random.seed(4)
    weak = _bat_one_over(batter, _so_player("Weak", 30, 45))
    random.seed(4)
    strong = _bat_one_over(batter, _so_player("Strong", 30, 95))
    assert strong < weak - 0.5, f"weak={weak:.2f} strong={strong:.2f}"


def test_traits_apply_in_the_super_over():
    # /letsplay XIs carry traits; they must work in the Super Over exactly as
    # they do in the twenty overs that produced the tie.
    power = [{"effect_key": "bat_power_hitter", "level": 5,
              "display_name": "Power Hitter", "emoji": "💥"}]
    bowler = _so_player("Bowl", 30, 70)
    plain = _so_player("Plain", 75, 30)
    random.seed(5)
    without = _bat_one_over(plain, bowler)

    from handlers.super_over import _super_over_ball
    from services.sim_match import _adapt_player
    random.seed(5)
    total = 0
    for _ in range(4000):
        oc = _super_over_ball(
            _adapt_player(plain), _adapt_player(bowler), "Hard",
            bat_traits=power, shot="Drive", variation="Stock Ball",
            length="Good Length")
        total += -6 if oc.get("type") == "wicket" else int(oc.get("runs", 0))
    with_trait = total / 4000
    assert with_trait > without, f"without={without:.2f} with={with_trait:.2f}"


def test_cipl_xis_without_traits_still_resolve():
    # Challenge League players carry no traits — the same call must work and
    # simply decide the ball on ratings.
    from handlers.super_over import _super_over_ball
    from services.sim_match import _adapt_player
    random.seed(6)
    oc = _super_over_ball(
        _adapt_player(_so_player("A", 80, 30)),
        _adapt_player(_so_player("B", 30, 80)), "Hard",
        shot="Drive", variation="Stock Ball", length="Good Length")
    assert oc["type"] in ("run", "wicket", "extra")
    assert oc["traits_activated"] == []


def test_neither_side_gets_a_batting_handicap():
    # The Super Over used to hand the side batting first a 1.25x scoring edge.
    # Identical players must now produce identical expectations.
    a = _so_player("A", 75, 70)
    b = _so_player("B", 75, 70)
    random.seed(8)
    first = _bat_one_over(a, b, n=6000)
    random.seed(8)
    second = _bat_one_over(b, a, n=6000)
    assert abs(first - second) < 0.15, f"first={first:.3f} second={second:.3f}"


# ── Deciding a level Super Over ───────────────────────────────────────

def _so_for_decision(r1, w1, six1, four1, r2, w2, six2, four2):
    return {
        "score": {1: (r1, w1), 2: (r2, w2)},
        "so_innings": [
            {"team_uid": 1, "batters": [{"sixes": six1, "fours": four1}]},
            {"team_uid": 2, "batters": [{"sixes": six2, "fours": four2}]},
        ],
    }


def test_runs_decide_the_super_over():
    from handlers.super_over import _decide_super_over
    so = _so_for_decision(15, 1, 1, 2, 12, 0, 2, 3)
    assert _decide_super_over(so, 1, 2) == (1, 2, "runs")
    so = _so_for_decision(12, 1, 2, 3, 15, 0, 1, 2)
    assert _decide_super_over(so, 1, 2) == (2, 1, "runs")


def test_a_completed_chase_wins_regardless_of_countback():
    from handlers.super_over import _decide_super_over
    so = _so_for_decision(15, 1, 3, 0, 16, 1, 0, 1)
    assert _decide_super_over(so, 1, 2, chase_won=True) == (2, 1, "runs")


def test_level_scores_go_to_sixes_then_fours():
    from handlers.super_over import _decide_super_over
    # Same runs, more sixes for the side batting second.
    so = _so_for_decision(14, 1, 1, 2, 14, 1, 2, 0)
    assert _decide_super_over(so, 1, 2) == (2, 1, "sixes")
    # Same runs and sixes → fours separate them.
    so = _so_for_decision(14, 1, 1, 3, 14, 0, 1, 1)
    assert _decide_super_over(so, 1, 2) == (1, 2, "fours")


def test_wickets_are_not_a_countback_rung():
    # Rewarding the side that lost fewer wickets would pay players to block out
    # a Super Over, so a true mirror image is replayed instead.
    from handlers.super_over import _decide_super_over
    so = _so_for_decision(14, 0, 1, 2, 14, 2, 1, 2)
    assert _decide_super_over(so, 1, 2) == (None, None, "tie")


def test_only_a_perfect_mirror_forces_another_super_over():
    from handlers.super_over import _decide_super_over
    so = _so_for_decision(11, 1, 1, 1, 11, 1, 1, 1)
    winner, loser, how = _decide_super_over(so, 1, 2)
    assert winner is None and loser is None and how == "tie"


# ── Presentation ──────────────────────────────────────────────────────

def test_score_card_shows_the_chase_equation_and_pressure():
    random.seed(25)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))
    asyncio.run(_do_selection(ctx, mid))
    so = _get(ctx, mid)
    so["innings_no"] = 2
    so["target"] = 16
    so["inn"]["runs"] = 13
    so["inn"]["legal"] = 5

    card = so_mod._score_card(so)
    assert "3 needed off the LAST BALL" in card
    assert "Pressure:" in card
    assert "▓" in card


def test_super_over_mvp_prefers_the_winning_side_on_a_tie():
    from handlers.super_over import _super_over_mvp
    so = {
        "so_innings": [
            {"team": "Guest XI", "team_uid": 2, "opp_uid": 1,
             "batters": [{"name": "G1", "runs": 10, "balls": 4,
                          "fours": 0, "sixes": 0}],
             "bowlers": [{"name": "H5", "wickets": 0, "runs": 15}]},
            {"team": "Host XI", "team_uid": 1, "opp_uid": 2,
             "batters": [{"name": "H1", "runs": 10, "balls": 3,
                          "fours": 0, "sixes": 0}],
             "bowlers": [{"name": "G5", "wickets": 0, "runs": 15}]},
        ],
    }
    name, line = _super_over_mvp(so, winner_uid=1)
    assert name == "H1"
    assert "10 (3b)" in line


def test_super_over_mvp_can_be_a_bowler():
    from handlers.super_over import _super_over_mvp
    so = {
        "so_innings": [
            {"team": "Guest XI", "team_uid": 2, "opp_uid": 1,
             "batters": [{"name": "G1", "runs": 4, "balls": 6,
                          "fours": 1, "sixes": 0}],
             "bowlers": [{"name": "H5", "wickets": 2, "runs": 4}]},
        ],
    }
    name, line = _super_over_mvp(so, winner_uid=1)
    assert name == "H5"
    assert "2-4" in line


def test_scorecard_images_sent_tie_superover_and_winner():
    import handlers.cipl_play as cipl_play
    random.seed(11)
    _patch_finalize(so_mod)
    # Stub the image builders so we don't need PIL/fonts — just bytes — and the
    # Mini App spectate button so every card carries it.
    orig_main = cipl_play._build_cipl_summary_image
    orig_so = so_mod._build_super_over_card
    orig_row = cipl_play._miniapp_row
    cipl_play._build_cipl_summary_image = lambda state, result: b"IMG"
    so_mod._build_super_over_card = lambda so: b"IMG"
    cipl_play._miniapp_row = lambda state: [["VIEW_MATCH_BTN"]]
    try:
        ctx = FakeContext()
        state = _tied_state()
        mid = state["match_id"]
        asyncio.run(start_super_over(ctx, mid, state))
        asyncio.run(_play_match(ctx, mid))

        photos = [m for m in ctx.bot.messages if getattr(m, "is_photo", False)]
        caps = "\n".join(m.text for m in photos if m.text)
        # 1) main "Match Tied" card, 2) Super Over card, 3) main winner card.
        assert "Match Tied" in caps
        assert "Super Over" in caps
        # The winner card reads either "won the match by N runs/wickets" or,
        # when a level score went to the boundary countback, "won on sixes/fours
        # hit (scores level) — and the match". Both are winner captions.
        assert "won the match by" in caps or "and the match" in caps
        assert len(photos) >= 3
        # Every scorecard image carries the main-match spectate button.
        assert all(getattr(p, "reply_markup", None) is not None for p in photos)
        # The reward message is sent even though the images rendered, so the
        # coins/gems aren't applied silently.
        text_msgs = "\n".join(
            m.text for m in ctx.bot.messages
            if m.text and not getattr(m, "is_photo", False))
        assert "MATCH RESULT" in text_msgs
        assert "Prizes" in text_msgs
    finally:
        cipl_play._build_cipl_summary_image = orig_main
        so_mod._build_super_over_card = orig_so
        cipl_play._miniapp_row = orig_row


def test_super_over_miniapp_innings_shape():
    from handlers.super_over import _super_over_miniapp_innings
    so = {
        "teams": {1: {"name": "Host XI"}, 2: {"name": "Guest XI"}},
        "so_innings": [
            {"team": "Guest XI", "team_uid": 2, "opp_uid": 1,
             "runs": 15, "wickets": 1, "overs": "1.0",
             "batters": [{"name": "G1", "runs": 12, "balls": 5, "fours": 1,
                          "sixes": 1, "out": False, "how_out": "not out"}],
             "bowlers": [{"name": "H5", "wickets": 1, "runs": 15, "overs": "1.0",
                          "balls": 6}]},
            {"team": "Host XI", "team_uid": 1, "opp_uid": 2,
             "runs": 14, "wickets": 2, "overs": "1.0",
             "batters": [{"name": "H1", "runs": 8, "balls": 4, "fours": 0,
                          "sixes": 1, "out": True, "how_out": "Bowled"}],
             "bowlers": [{"name": "G5", "wickets": 2, "runs": 14, "overs": "1.0",
                          "balls": 6}]},
        ],
    }
    inns = _super_over_miniapp_innings(so)
    assert len(inns) == 2
    assert inns[0]["label"] == "Super Over - Innings 1"
    assert inns[1]["label"] == "Super Over - Innings 2"
    assert inns[0]["number"] == 3 and inns[1]["number"] == 4
    assert all(i["super_over"] for i in inns)
    assert inns[0]["bat_team"] == "Guest XI" and inns[0]["bowl_team"] == "Host XI"
    assert inns[0]["runs"] == 15 and inns[0]["wickets"] == 1
    assert inns[0]["batting"][0]["name"] == "G1"
    assert inns[0]["bowling"][0]["wickets"] == 1


def test_finalize_persists_super_over_innings():
    import services.match_webapp_service as mws
    random.seed(13)
    _patch_finalize(so_mod)
    captured = {}

    def fake_save(session, match_id, result_text=None, extra_innings=None,
                  super_over=None):
        captured["result_text"] = result_text
        captured["extra_innings"] = extra_innings
        captured["super_over"] = super_over
        return True

    orig = mws.save_final_scorecard
    mws.save_final_scorecard = fake_save
    try:
        ctx = FakeContext()
        state = _tied_state()
        mid = state["match_id"]
        asyncio.run(start_super_over(ctx, mid, state))
        asyncio.run(_play_match(ctx, mid))
        assert captured.get("extra_innings") is not None
        assert len(captured["extra_innings"]) == 2  # two Super Over innings
        assert "Super Over" in (captured.get("result_text") or "")
        assert "won the match by" in (captured.get("result_text") or "")
        # Compact summary for the Mini App result screen.
        so = captured.get("super_over") or {}
        assert so.get("winner")
        assert len(so.get("innings") or []) == 2
        assert so["innings"][0]["label"] == "Super Over Innings 1"
    finally:
        mws.save_final_scorecard = orig


def _simulate_super_over(bat, bowl, pitch, target=None):
    """Headless Super Over innings using the real ball engine + the real rules
    (6 legal balls, 2 wickets, chase ends on the target)."""
    from handlers.super_over import _super_over_ball
    from services.sim_match import _adapt_player
    bowler = _adapt_player(bowl)
    runs = wickets = legal = deliveries = 0
    sixes = fours = 0
    free_hit = False
    bat_runs = bat_balls = 0
    while legal < 6 and wickets < 2 and deliveries < 24:
        oc = _super_over_ball(
            _adapt_player(bat), bowler, pitch, batter_runs=bat_runs,
            balls_faced=bat_balls, free_hit=free_hit, legal=legal,
            target=target, runs=runs, shot="Drive",
            variation="Stock Ball", length="Good Length")
        r = int(oc.get("runs", 0))
        otype, etype = oc.get("type"), oc.get("extra_type")
        free_hit = False
        is_legal = True
        if otype == "extra" and etype in ("Wide", "No Ball"):
            is_legal = False
            runs += r
            free_hit = (etype == "No Ball")
        elif otype == "extra":
            runs += r
            bat_balls += 1
        elif otype == "wicket":
            wickets += 1
            runs += r
            bat_balls += 1
        else:
            runs += r
            bat_runs += r
            bat_balls += 1
            if r == 6:
                sixes += 1
            elif r == 4:
                fours += 1
        if is_legal:
            legal += 1
        deliveries += 1
        if target is not None and runs >= target:
            break
    return runs, wickets, sixes, fours


def test_super_over_balance_scoring_fairness_and_replay_rate():
    """The three balance properties, measured rather than asserted by eye.

    Kept to 1,200 Super Overs so it stays a fast test; the thresholds are wide
    enough to absorb that sample's noise while still catching a real regression
    (an inflated matrix, a reinstated batting-first edge, or a countback that
    stops working).
    """
    from handlers.super_over import _decide_super_over
    n = 1200
    a = _so_player("A", 75, 70)
    b = _so_player("B", 75, 70)
    first_runs = []
    first_wins = second_wins = replays = 0
    random.seed(42)
    for _ in range(n):
        pitch = random.choice(["Hard", "Flat", "Green", "Dry", "Dead"])
        r1, w1, s1, f1 = _simulate_super_over(a, b, pitch)
        r2, w2, s2, f2 = _simulate_super_over(b, a, pitch, target=r1 + 1)
        first_runs.append(r1)
        so = {"score": {1: (r1, w1), 2: (r2, w2)},
              "so_innings": [
                  {"team_uid": 1, "batters": [{"sixes": s1, "fours": f1}]},
                  {"team_uid": 2, "batters": [{"sixes": s2, "fours": f2}]}]}
        winner, _loser, _how = _decide_super_over(so, 1, 2)
        if winner is None:
            replays += 1
        elif winner == 1:
            first_wins += 1
        else:
            second_wins += 1

    mean_runs = sum(first_runs) / n
    # 1) Realistic scoring — a Super Over is ~8-14, not the 17+ the old
    #    boundary-inflated matrix produced.
    assert 7.0 <= mean_runs <= 15.0, f"mean Super Over score {mean_runs:.2f}"
    # 2) Equal chance — identical sides must not favour batting first or second.
    assert abs(first_wins - second_wins) / n < 0.10, (
        f"bat-first {first_wins} vs bat-second {second_wins} of {n}")
    # 3) Another Super Over stays rare (~2%); the countback does the rest.
    assert replays / n < 0.06, f"replay rate {replays / n:.3%}"


def test_many_seeds_always_terminate_with_a_winner():
    for seed in range(15):
        random.seed(seed)
        _patch_finalize(so_mod)
        ctx = FakeContext()
        state = _tied_state()
        mid = state["match_id"]
        assert asyncio.run(start_super_over(ctx, mid, state)) is True
        asyncio.run(_play_match(ctx, mid))
        assert _get(ctx, mid) is None, f"seed {seed} did not finish"


# ── Rule enforcement, ball by ball ────────────────────────────────────
#
# The tests above drive the real engine, so what happens on any given ball is up
# to the dice. These script the outcome instead, so the wicket limit, the crease
# and the extras arithmetic can be asserted exactly.

WIDE = {"type": "extra", "is_extra": True, "extra_type": "Wide", "runs": 0,
        "batter_out": False, "wicket_type": None}
BOWLED = {"type": "wicket", "is_extra": False, "runs": 0, "batter_out": True,
          "wicket_type": "Bowled", "extra_type": ""}


def _live_innings():
    """A Super Over kicked off and taken through selection, ready to bowl."""
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))
    asyncio.run(_do_selection(ctx, mid))
    return ctx, mid


def _bowl_scripted(ctx, mid, outcome):
    """Bowl one ball whose outcome is fixed rather than rolled."""
    so = _get(ctx, mid)
    bat_tg = so["teams"][so["bat_uid"]]["tg"]
    bowl_tg = so["teams"][so["bowl_uid"]]["tg"]
    orig = so_mod._super_over_ball
    so_mod._super_over_ball = lambda *a, **k: dict(outcome)
    try:
        asyncio.run(so_deliv_callback(_upd(FakeQuery(f"so_dv_{mid}_0", bowl_tg)), ctx))
        so = _get(ctx, mid)
        if so and "inn" in so and so["inn"]["stage"] == "LEN":
            asyncio.run(so_len_callback(_upd(FakeQuery(f"so_ln_{mid}_0", bowl_tg)), ctx))
        asyncio.run(so_shot_callback(_upd(FakeQuery(f"so_sh_{mid}_0", bat_tg)), ctx))
    finally:
        so_mod._super_over_ball = orig


def test_a_wide_costs_a_run_and_is_re_bowled():
    ctx, mid = _live_innings()
    _bowl_scripted(ctx, mid, WIDE)
    inn = _get(ctx, mid)["inn"]
    # A wide is one run to the batting side and is NOT a legal ball — it used to
    # be scored as nothing at all, making it a free re-bowl for the bowler.
    assert inn["runs"] == 1
    assert inn["bowl_runs"] == 1
    assert inn["legal"] == 0
    assert inn["deliveries"] == 1


def test_a_no_ball_carries_the_penalty_run_and_the_batter_keeps_the_six():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    striker = int(so["inn"]["striker"])
    _bowl_scripted(ctx, mid, {"type": "extra", "is_extra": True,
                              "extra_type": "No Ball", "runs": 6,
                              "batter_out": False, "wicket_type": None})
    inn = _get(ctx, mid)["inn"]
    assert inn["runs"] == 7                     # 6 off the bat + 1 penalty
    assert inn["bat"][striker]["r"] == 6        # the six is the batter's
    assert inn["bat"][striker]["6"] == 1        # ...and counts on the countback
    assert inn["legal"] == 0
    assert inn["free_hit"] is True


def test_the_innings_is_over_the_moment_the_second_wicket_falls():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    bat_uid = so["bat_uid"]
    _bowl_scripted(ctx, mid, BOWLED)
    assert _get(ctx, mid)["inn"]["wickets"] == 1     # backup walks in
    _bowl_scripted(ctx, mid, BOWLED)

    so = _get(ctx, mid)
    assert so["score"][bat_uid] == (0, 2)
    assert so["innings_no"] == 2                     # rolled straight on to the chase
    assert so["bat_uid"] != bat_uid                  # roles swapped
    assert so["inn"]["legal"] == 2                   # nobody batted a 3rd time


def test_no_shot_is_accepted_once_two_wickets_are_down():
    ctx, mid = _live_innings()
    _bowl_scripted(ctx, mid, BOWLED)
    _bowl_scripted(ctx, mid, BOWLED)

    so = _get(ctx, mid)
    inn = so["inn"]                                  # the finished innings
    assert inn["wickets"] == 2
    # Simulate a prompt that was still on screen when the innings closed: the
    # shot button must be refused rather than bowling a 3rd ball at a side that
    # is already all out.
    inn["stage"] = "SHOT"
    inn["pending"] = {"delivery": "Bouncer", "length": "Short"}
    before = (inn["runs"], inn["legal"], inn["deliveries"])
    asyncio.run(so_shot_callback(
        _upd(FakeQuery(f"so_sh_{mid}_0", so["teams"][so["bat_uid"]]["tg"])), ctx))
    assert (inn["runs"], inn["legal"], inn["deliveries"]) == before


def test_a_run_out_takes_the_dismissed_batter_off_whichever_end_they_are_at():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    inn = so["inn"]
    opener, partner, backup = inn["striker"], inn["non_striker"], inn["backup"]
    # One completed run, then the run out: the batters crossed, so the man who
    # is out is standing at the NON-striker's end.
    _bowl_scripted(ctx, mid, {"type": "wicket", "is_extra": False, "runs": 1,
                              "batter_out": True, "wicket_type": "Run Out",
                              "extra_type": ""})
    inn = _get(ctx, mid)["inn"]
    assert inn["wickets"] == 1
    assert opener not in (inn["striker"], inn["non_striker"])
    assert {inn["striker"], inn["non_striker"]} == {partner, backup}
    assert inn["bowl_wkts"] == 0                # a run out is not the bowler's


def test_a_dismissal_off_a_no_ball_still_costs_a_wicket():
    ctx, mid = _live_innings()
    _bowl_scripted(ctx, mid, {"type": "extra", "is_extra": True,
                              "extra_type": "No Ball", "runs": 0,
                              "batter_out": True, "wicket_type": "Run Out"})
    inn = _get(ctx, mid)["inn"]
    assert inn["runs"] == 1                     # the penalty run still stands
    assert inn["wickets"] == 1                  # and the wicket is not lost
    assert inn["legal"] == 0                    # a no-ball is still re-bowled


def test_used_batters_are_not_re_offered_while_fresh_ones_remain():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    uid = so["bat_uid"]
    xi = so["teams"][uid]["xi"]                 # five players
    so["batted"][uid] = {int(p["roster_id"]) for p in xi[:3]}

    names = [p["name"] for p in _eligible_batters(so, uid)]
    # Two fresh batters left, so the pool is topped back up to three — and the
    # two who never batted are both in it.
    assert len(names) == 3
    assert xi[3]["name"] in names and xi[4]["name"] in names


def test_the_next_batter_is_announced_when_a_wicket_falls():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    from handlers.super_over import _name
    backup = so["inn"]["backup"]
    backup_name = _name(so, so["bat_uid"], backup)

    _bowl_scripted(ctx, mid, BOWLED)

    inn = _get(ctx, mid)["inn"]
    assert backup in (inn["striker"], inn["non_striker"])   # they really came in
    # ...and the chat says so, rather than silently swapping a name on the
    # two-batter line where nobody notices it. Crucially it must survive into
    # the NEXT prompt: the ball message is overwritten a second later, so an
    # announcement that lives only there is one nobody sees.
    latest = ctx.bot.messages[-1].text
    assert f"{backup_name}</b> walks out to the middle" in latest
    assert f"🆕 {backup_name}" in latest      # badged until they face a ball


def test_only_the_batters_who_walked_out_are_spent_by_a_replay():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    bat_uid = so["bat_uid"]
    inn = so["inn"]
    openers = {int(inn["striker"]), int(inn["non_striker"])}
    backup = int(inn["backup"])

    # Six dot balls: the reserve never leaves the dressing room.
    for _ in range(6):
        _bowl_scripted(ctx, mid, {"type": "run", "is_extra": False, "runs": 0,
                                  "batter_out": False, "wicket_type": None})

    so = _get(ctx, mid)
    assert so["batted"][bat_uid] == openers
    assert backup not in so["batted"][bat_uid]
    # So the next Super Over may still pick them.
    assert backup in {int(p["roster_id"]) for p in _eligible_batters(so, bat_uid)}


def test_a_bowler_who_has_bowled_a_super_over_cannot_bowl_another():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    bowl_uid = so["bowl_uid"]
    bowler = int(so["inn"]["bowler_rid"])

    for _ in range(6):
        _bowl_scripted(ctx, mid, {"type": "run", "is_extra": False, "runs": 0,
                                  "batter_out": False, "wicket_type": None})

    so = _get(ctx, mid)
    assert bowler in so["used_bowlers"][bowl_uid]
    assert bowler not in {int(p["roster_id"])
                          for p in _eligible_bowlers(so, bowl_uid)}


def test_bowling_the_same_ball_over_and_over_is_charged_for():
    ctx, mid = _live_innings()
    so = _get(ctx, mid)
    # The delivery + length pair is what the engine's spam layer counts.
    _bowl_scripted(ctx, mid, WIDE)
    assert _get(ctx, mid)["inn"]["repeat"] == 1
    _bowl_scripted(ctx, mid, WIDE)
    assert _get(ctx, mid)["inn"]["repeat"] == 2


def test_the_super_over_leaks_more_extras_than_an_ordinary_over():
    from handlers.super_over import _super_over_ball, SO_EXTRAS_MULT
    import services.probability_engine as pe

    seen = {}
    orig = pe.calculate_outcome

    def spy(*a, **k):
        seen["extras_mult"] = k.get("extras_mult")
        return orig(*a, **k)

    pe.calculate_outcome = spy
    try:
        _super_over_ball({"batting_rating": 75}, {"bowling_rating": 70},
                         "Flat", shot="Drive", variation="Seam Up", length="Good")
    finally:
        pe.calculate_outcome = orig
    assert seen["extras_mult"] == SO_EXTRAS_MULT > 1.0
