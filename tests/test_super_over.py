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


# ── Fakes ────────────────────────────────────────────────────────────

class FakeBot:
    def __init__(self):
        self._mid = 5000
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._mid += 1
        m = SimpleNamespace(message_id=self._mid, text=text,
                            reply_markup=reply_markup, chat_id=chat_id)
        self.messages.append(m)
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                parse_mode=None, reply_markup=None):
        m = SimpleNamespace(message_id=message_id, text=text,
                            reply_markup=reply_markup, chat_id=chat_id)
        self.messages.append(m)
        return m

    async def send_photo(self, chat_id, photo=None, caption=None,
                         parse_mode=None, reply_markup=None):
        self._mid += 1
        m = SimpleNamespace(message_id=self._mid, text=caption,
                            reply_markup=reply_markup, chat_id=chat_id,
                            photo=photo, is_photo=True)
        self.messages.append(m)
        return m

    async def edit_message_reply_markup(self, chat_id=None, message_id=None,
                                        reply_markup=None):
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


def test_dismissed_batter_cannot_be_reselected():
    random.seed(3)
    _patch_finalize(so_mod)
    ctx = FakeContext()
    state = _tied_state()
    mid = state["match_id"]
    asyncio.run(start_super_over(ctx, mid, state))
    so = _get(ctx, mid)

    bat_uid = so["bat_uid"]                       # GUEST
    bat_tg = so["teams"][bat_uid]["tg"]
    # Pretend this batter was dismissed in an earlier Super Over of this tie.
    gone = int(so["teams"][bat_uid]["xi"][0]["roster_id"])
    so["dismissed"][bat_uid].add(gone)

    # A stale button tap for the dismissed player must be rejected.
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
    # Mark this team's previous-Super-Over bowler — now restricted.
    restricted = int(so["teams"][bowl_uid]["xi"][0]["roster_id"])
    so["last_bowler"][bowl_uid] = restricted

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
    orig = so_mod.calculate_super_over_outcome
    so_mod.calculate_super_over_outcome = lambda *a, **k: {
        "type": "run", "runs": 0, "description": "dot",
        "wicket_type": None, "is_extra": False, "batter_out": False}
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
        so_mod.calculate_super_over_outcome = orig


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


def test_first_batting_team_gets_the_edge():
    random.seed(0)
    so_mod_outcome = so_mod.calculate_super_over_outcome
    # Boundary boost lifts 4s/6s; the batting edge cuts the favoured side's
    # wicket chance. Compare large samples with identical players.
    b = {"name": "A", "batting_rating": 80, "batting_hand": "Right"}
    bw = {"name": "B", "bowling_rating": 70, "fielding_rating": 65,
          "bowling_type": "Fast", "bowling_hand": "Right"}

    def sample(n=8000, **kw):
        random.seed(1)
        bdry = wkt = 0
        for _ in range(n):
            o = so_mod_outcome(b, bw, "Hard", {"boundaries": 0}, 0, 0, **kw)
            if o["type"] == "run" and o["runs"] in (4, 6):
                bdry += 1
            elif o["type"] == "wicket":
                wkt += 1
        return bdry / n, wkt / n

    base_b, base_w = sample()
    boost_b, _ = sample(boundary_boost=1.6)
    edge_b, edge_w = sample(boundary_boost=1.6, edge=1.25)
    drama_b, _ = sample(boundary_boost=1.6, edge=1.25, last_ball=True)

    assert boost_b > base_b              # more boundaries with the boost
    assert edge_w < base_w               # edge → fewer wickets for the bat side
    assert drama_b > edge_b              # last-ball drama → even more boundaries


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
        assert "won the match by" in caps
        assert len(photos) >= 3
        # Every scorecard image carries the main-match spectate button.
        assert all(getattr(p, "reply_markup", None) is not None for p in photos)
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
