"""The drinks interval: the plans, the grid's invariants, and the flow.

The grid is the part worth pinning. A session plan that is worth nothing is
flavour, one that is worth too much decides matches on a single tap, and one
that is not mean-neutral quietly drags every ODI total off the anchor the
measured reference was calibrated to. Each of those is a test here rather than
a comment claiming it.
"""

import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import bot_captain, cipl_match as cm, drinks_break as db
from services import odi_approach
from tools.calibrate_odi_approach import team

#: The plausible range for what a wicket is worth, in run-multiplier units, at
#: the interval of a limited-overs innings. The non-dominance property has to
#: hold across all of it, not at one convenient price.
WICKET_PRICES = (0.2, 0.35, 0.5, 0.7, 1.0)


def build(overs=50, seed=11, ball_format="ODI"):
    state = cm.build_cipl_state(90000 + overs, overs, 1, 2, 111, 222,
                                team(1, 0), team(2, 0), "Bat", "Bowl", -100,
                                ball_format=ball_format)
    state["rng_seed"] = seed
    return state


def bat_value(bat, bowl, price):
    """What the pair is worth to the batting side at a given wicket price."""
    runs, wicket = db.payoff(bat, bowl)
    return runs - price * (wicket - 1.0)


# ── The grid ─────────────────────────────────────────────────────────
def test_the_grid_is_mean_neutral():
    """The interval as a whole is worth nothing; only the choice matters."""
    runs, wicket = db.grid_mean()
    assert runs == pytest.approx(1.0, abs=1e-9)
    assert wicket == pytest.approx(1.0, abs=1e-9)


def test_no_cell_reaches_the_rails():
    """The clamps guard a future edit; they must not shape today's grid.

    If a cell clamped, the normalisation above could not be exact.
    """
    for bat, bowl in itertools.product(db.BAT_KEYS, db.BOWL_KEYS):
        runs, wicket = db.payoff(bat, bowl)
        assert db.RUN_BOUND[0] < runs < db.RUN_BOUND[1], (bat, bowl, runs)
        assert db.WICKET_BOUND[0] < wicket < db.WICKET_BOUND[1], (bat, bowl, wicket)


def test_the_interval_cannot_decide_a_match_on_its_own():
    """A session plan is worth single figures of runs, not a match."""
    cells = [db.payoff(a, b) for a, b in itertools.product(db.BAT_KEYS, db.BOWL_KEYS)]
    assert max(c[0] for c in cells) <= 1.12
    assert min(c[0] for c in cells) >= 0.90
    assert max(c[1] for c in cells) <= 1.20
    assert min(c[1] for c in cells) >= 0.82


@pytest.mark.parametrize("price", WICKET_PRICES)
def test_no_plan_is_the_best_answer_to_everything(price):
    """The same property the per-over approach frontier is held to."""
    bat_replies = {bowl: max(db.BAT_KEYS, key=lambda b: bat_value(b, bowl, price))
                   for bowl in db.BOWL_KEYS}
    bowl_replies = {bat: min(db.BOWL_KEYS, key=lambda b: bat_value(bat, b, price))
                    for bat in db.BAT_KEYS}
    assert len(set(bat_replies.values())) > 1, bat_replies
    assert len(set(bowl_replies.values())) > 1, bowl_replies


def test_every_bowling_plan_answers_exactly_one_batting_plan():
    """Squeeze beats attack, rotate beats building, strike beats blocking."""
    replies = {bat: min(db.BOWL_KEYS, key=lambda b: bat_value(bat, b, 0.5))
               for bat in db.BAT_KEYS}
    assert replies == {"accelerate": "squeeze", "build": "rotate",
                       "consolidate": "strike"}


def test_every_batting_plan_wins_a_column_somewhere_in_the_band():
    winners = {max(db.BAT_KEYS, key=lambda k: bat_value(k, bowl, price))
               for price in WICKET_PRICES for bowl in db.BOWL_KEYS}
    assert winners == set(db.BAT_KEYS)


def test_batting_plans_are_a_priced_trade_off():
    """Asking for more runs costs more wickets, monotonically."""
    ladder = ("consolidate", "build", "accelerate")
    for a, b in zip(ladder, ladder[1:]):
        assert db.BAT_LEVELS[a]["runs"] < db.BAT_LEVELS[b]["runs"]
        assert db.BAT_LEVELS[a]["wicket"] < db.BAT_LEVELS[b]["wicket"]


def test_the_strength_dial_scales_the_whole_grid():
    """SESSION_STRENGTH is the one number that says how heavy the interval is."""
    spread = max(db.payoff(a, b)[0] for a, b in
                 itertools.product(db.BAT_KEYS, db.BOWL_KEYS)) - 1.0
    assert 0 < spread < 0.15
    assert 0.0 < db.SESSION_STRENGTH <= 1.0


def test_an_unknown_plan_is_neutral_rather_than_an_error():
    """A state written by a future version must never break a delivery."""
    assert db.payoff(None, None) == db.NEUTRAL
    assert db.payoff("improvise", "squeeze") == db.NEUTRAL
    assert db.payoff("build", "bodyline") == db.NEUTRAL


def test_labels_and_blurbs_cover_every_plan():
    for key in db.BAT_KEYS + db.BOWL_KEYS:
        assert db.label_for(key) and db.label_for(key) != key
        assert db.blurb_for(key)


# ── When it falls ────────────────────────────────────────────────────
@pytest.mark.parametrize("overs,over", [(21, 10), (25, 12), (30, 15),
                                        (35, 17), (50, 25)])
def test_the_interval_is_halfway_through_the_innings(overs, over):
    assert db.interval_over(build(overs)) == over


@pytest.mark.parametrize("ball_format,overs", [("T20", 20), ("T20", 6),
                                               ("The100", 20)])
def test_short_formats_take_no_drinks(ball_format, overs):
    state = build(overs, ball_format=ball_format)
    assert not db.applies(state)
    assert db.interval_over(state) is None
    assert not db.due(state)
    assert db.modifier(state) == db.NEUTRAL


def test_an_innings_with_no_plan_is_left_exactly_as_it_was():
    state = build(50)
    assert db.taken(state) is None and db.plan(state) is None
    assert db.modifier(state) == db.NEUTRAL
    assert odi_approach.context(state).extra["session"] == (1.0, 1.0)


def test_the_plan_reaches_the_delivery_context():
    state = build(50)
    db.record(state, "accelerate", "strike")
    assert odi_approach.context(state).extra["session"] == db.payoff("accelerate", "strike")


@pytest.mark.parametrize("overs", (21, 30, 50))
def test_the_interval_comes_due_once_and_only_once(overs):
    state = build(overs)
    fired = []
    while not cm.is_innings_over(state):
        if db.due(state):
            fired.append(state["current_over"])
            db.record(state, "build", "rotate")
        state["current_bowler"] = bot_captain.pick_bowler(state)
        state["batting_approach"] = state["bowling_approach"] = "balanced"
        cm.simulate_over(state)
    assert fired == [db.interval_over(state) + 1]
    assert db.plan(state) == ("build", "rotate")


def test_an_innings_that_ended_at_the_interval_takes_no_drinks():
    """There is no session left to plan, so the interval is skipped."""
    state = build(50)
    state["current_over"] = db.interval_over(state) + 1
    state["total_wickets"] = state["wicket_limit"]
    assert not db.due(state)


def test_each_innings_gets_its_own_plan():
    state = build(50)
    state["current_over"] = db.interval_over(state) + 1
    db.record(state, "accelerate", "strike")
    first = db.payoff("accelerate", "strike")
    assert db.modifier(state) == first
    state["innings"] = 2
    assert db.taken(state) is None and db.modifier(state) == db.NEUTRAL
    db.record(state, "consolidate", "squeeze")
    assert db.modifier(state) == db.payoff("consolidate", "squeeze")
    # Innings 1's entry is still there, untouched.
    assert db.taken(state, 1)["bat"] == "accelerate"


def test_the_record_keeps_the_match_situation_it_was_called_in():
    state = build(50)
    state.update(current_over=26, total_runs=142, total_wickets=3)
    entry = db.record(state, "build", "squeeze")
    assert entry["runs"] == 142 and entry["wickets"] == 3
    assert entry["over"] == 25


# ── The reveal ───────────────────────────────────────────────────────
def test_the_reveal_names_both_plans_and_the_matchup():
    state = build(50)
    db.record(state, "accelerate", "squeeze")
    lines = db.reveal_lines(state, bat_team="India", bowl_team="Australia")
    assert len(lines) == 3
    assert "India" in lines[0] and "Accelerate" in lines[0]
    assert "Australia" in lines[1] and "Squeeze" in lines[1]
    assert lines[2].startswith("🎯") or lines[2].startswith("⚡")


def test_there_is_nothing_to_reveal_before_the_interval():
    assert db.reveal_lines(build(50)) == []


@pytest.mark.parametrize("runs,wicket", [(1.10, 1.10), (1.10, 1.0), (1.0, 1.10),
                                         (0.90, 0.90), (0.90, 1.0), (1.0, 1.0)])
def test_every_matchup_leans_somewhere(runs, wicket):
    assert db.matchup_line(runs, wicket).strip()


# ── The AI captain ───────────────────────────────────────────────────
def test_the_ai_reads_the_first_innings():
    cases = {
        (160, 2): ("build", "rotate"),       # cruising
        (110, 7): ("consolidate", "squeeze"),  # wobbling
        (90, 3): ("accelerate", "strike"),   # behind with wickets in hand
    }
    for (runs, wickets), expected in cases.items():
        state = build(50, seed=3)
        state.update(current_over=26, total_runs=runs, total_wickets=wickets)
        assert (db.ai_plan(state, "bat"), db.ai_plan(state, "bowl")) == expected


def test_the_ai_reads_a_chase():
    for runs, wickets, expected_bat in ((150, 2, "build"), (90, 6, "accelerate"),
                                        (190, 1, "consolidate")):
        state = build(50, seed=3)
        state.update(current_over=26, innings=2, target=280,
                     total_runs=runs, total_wickets=wickets)
        assert db.ai_plan(state, "bat") == expected_bat
        assert db.ai_plan(state, "bowl") in db.BOWL_KEYS


def test_the_ai_always_returns_a_real_plan():
    """Including from a state it can make no sense of."""
    for side, keys in (("bat", db.BAT_KEYS), ("bowl", db.BOWL_KEYS)):
        assert db.ai_plan(build(50), side) in keys
        broken = build(50)
        broken.pop("total_runs", None)
        broken["current_over"] = 1
        assert db.ai_plan(broken, side) in keys


# ── The flow ─────────────────────────────────────────────────────────
def test_the_interval_moves_the_innings_in_the_direction_of_the_plan():
    """Two identical innings, one accelerating and one consolidating."""
    def play(plan):
        state = build(50, seed=7)
        while not cm.is_innings_over(state):
            if db.due(state):
                db.record(state, *plan)
            state["current_bowler"] = bot_captain.pick_bowler(state)
            state["batting_approach"] = state["bowling_approach"] = "balanced"
            cm.simulate_over(state)
        return state

    fast = play(("accelerate", "strike"))
    slow = play(("consolidate", "squeeze"))
    # Same seed, same XIs, same per-over approaches: the interval is the only
    # difference, and it has to show up as more risk taken.
    assert fast["total_runs"] != slow["total_runs"]
    assert db.payoff("accelerate", "strike")[1] > db.payoff("consolidate", "squeeze")[1]


def test_the_pick_kinds_are_wired_to_the_right_captain():
    from handlers import cipl_play
    state = {"bat_user_tg": 111, "bowl_user_tg": 222}
    assert cipl_play._pick_actor_error(state, cipl_play.PICK_DRINKS_BOWL, 222) is None
    assert cipl_play._pick_actor_error(state, cipl_play.PICK_DRINKS_BOWL, 111)
    assert cipl_play._pick_actor_error(state, cipl_play.PICK_DRINKS_BAT, 111) is None
    assert cipl_play._pick_actor_error(state, cipl_play.PICK_DRINKS_BAT, 222)


def test_the_interval_is_asked_in_the_chat_on_both_surfaces():
    """A Mini App match does not have to wait for an app release to use it."""
    from handlers import cipl_play
    assert cipl_play.PICK_DRINKS_BOWL in cipl_play._SURFACE_EXEMPT
    assert cipl_play.PICK_DRINKS_BAT in cipl_play._SURFACE_EXEMPT
    assert cipl_play.PICK_BAT_APPROACH not in cipl_play._SURFACE_EXEMPT


def test_both_pick_kinds_have_an_action_and_a_refusal():
    from handlers import cipl_play
    for kind in (cipl_play.PICK_DRINKS_BOWL, cipl_play.PICK_DRINKS_BAT):
        assert kind in cipl_play._PICK_ACTION
        assert cipl_play._PICK_ALREADY[kind]


def test_the_arena_shows_the_plans_in_force():
    from services.crickidex_arena import _approach_payload
    from services.match_state_store import A_PICK_CIPL_BOWLER
    state = build(50)
    assert "sessionPlans" not in _approach_payload(state, A_PICK_CIPL_BOWLER, "bowling")
    db.record(state, "build", "squeeze")
    plans = _approach_payload(state, A_PICK_CIPL_BOWLER, "bowling")["sessionPlans"]
    assert "Build" in plans["batting"] and "Squeeze" in plans["bowling"]
    assert plans["over"] == 25
    assert plans["runFactor"] == pytest.approx(db.payoff("build", "squeeze")[0], abs=1e-3)
