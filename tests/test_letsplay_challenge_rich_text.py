"""Lets Play and the Challenge League, rendered as rich messages.

Both flows drew every setup card as a padded HTML string — a numbered XI whose
ratings only lined up when every name happened to be the same width, a fixture
recap made of ``═════`` rules, a pitch menu as a bulleted list. Telegram renders
all of it in a proportional font, so none of it ever lined up.

They are tables, and now they are drawn as tables. Both renderings stay live —
``rich_message`` sends the HTML whenever a rich send is refused — so the pair
have to agree on everything a captain acts on. That is what this file checks:

  • **the numbers match.** The slot a captain types at ``/change`` is the slot
    the table shows, and the bench keeps numbering from 12 in both renderings.
  • **the verdict matches.** Whether the match counts towards career stats is
    the one thing on the XI card somebody acts on, and two renderers deriving
    it separately is two cards that can disagree about it.
  • **a renderer bug costs the rendering, not the card.** Every builder answers
    None rather than raising, because a captain is waiting on each of these to
    tap something.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _text_of(node):
    """Every string inside a block tree, flattened — for "is it on the card?"."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(_text_of(n) for n in node)
    if isinstance(node, dict):
        return " ".join(_text_of(v) for k, v in node.items()
                        if k in ("text", "caption", "summary", "blocks",
                                 "cells", "items", "label"))
    return str(node)


def _tables(blocks):
    return [b for b in (blocks or []) if b.get("type") == "table"]


def _details(blocks):
    return [b for b in (blocks or []) if b.get("type") == "details"]


# ══════════════════════════════════════════════════════════════════════
# Lets Play
# ══════════════════════════════════════════════════════════════════════

class LetsPlayCardTests(unittest.TestCase):

    def setUp(self):
        from services import letsplay_rich
        self.lpr = letsplay_rich
        self.draft = {
            "host": {"name": "Ranjan", "tg_id": 111, "user_id": 1},
            "guest": {"name": "Amit", "tg_id": 222, "user_id": 2},
            "pitch_type": "Green", "lpt": None,
        }
        self.xi = [(f"Player {i}", str(90 - i), "🏏 BAT", "") for i in range(1, 12)]
        self.bench = [(f"Sub {i}", str(70 - i), "🎯 BOWL", "") for i in range(1, 4)]

    # ── the invitation ────────────────────────────────────────────────
    def test_the_invitation_names_both_captains(self):
        blocks = self.lpr.invite_blocks(self.draft)
        body = _text_of(blocks)
        self.assertIn("Ranjan", body)
        self.assertIn("Amit", body)

    def test_the_guest_is_a_real_mention_not_just_a_name(self):
        # A card that names the guest without reaching them is a prompt nobody
        # is told about.
        blocks = self.lpr.invite_blocks(self.draft)
        links = re.findall(r"tg://user\?id=(\d+)", str(blocks))
        self.assertIn("222", links)

    def test_a_tournament_fixture_says_the_result_counts(self):
        self.draft["lpt"] = {"tournament_name": "Summer Cup",
                             "host_team_name": "MI", "guest_team_name": "CSK"}
        blocks = self.lpr.invite_blocks(self.draft)
        self.assertIn("Summer Cup", _text_of(blocks))
        quotes = [b for b in blocks if b.get("type") == "pullquote"]
        self.assertTrue(quotes, "the card must say the result counts")
        self.assertIn("points table", _text_of(quotes))

    def test_a_friendly_makes_no_such_claim(self):
        blocks = self.lpr.invite_blocks(self.draft)
        self.assertNotIn("points table", _text_of(blocks))

    # ── the pitch ─────────────────────────────────────────────────────
    def test_the_pitch_badge_is_the_one_the_html_prints(self):
        from handlers import letsplay
        self.assertIs(letsplay._PITCH_EMOJI, self.lpr.PITCH_EMOJI)
        self.assertIn(self.lpr.PITCH_EMOJI["Green"],
                      _text_of(self.lpr.pitch_locked_blocks("Green")))

    def test_an_unknown_pitch_still_renders(self):
        self.assertIn("Moon Dust",
                      _text_of(self.lpr.pitch_locked_blocks("Moon Dust")))

    # ── the Playing XI ────────────────────────────────────────────────
    def build_xi(self, **kw):
        options = dict(vs_bot=False, pitch="Green", host_label="Ranjan",
                       guest_label="Amit (Guest)", host_xi=self.xi,
                       guest_xi=self.xi, host_bench=self.bench,
                       guest_bench=[], trait_status=None, fairness=[],
                       start_prompt=None)
        options.update(kw)
        return self.lpr.playing_xi_blocks(**options)

    def test_both_line_ups_are_numbered_one_to_eleven(self):
        blocks = self.build_xi()
        tables = _tables(blocks)
        self.assertGreaterEqual(len(tables), 2)
        for table in tables[:2]:
            ranks = [row[0]["text"] for row in table["cells"][1:]]
            self.assertEqual(ranks, [str(i) for i in range(1, 12)])

    def test_the_bench_keeps_numbering_from_twelve(self):
        # The number on the card is the number typed at /change, so 12 is not
        # cosmetic — reading it wrong swaps in the wrong player.
        blocks = self.build_xi()
        bench = _details(blocks)
        self.assertTrue(bench)
        rows = bench[0]["blocks"][0]["cells"][1:]
        self.assertEqual([row[0]["text"] for row in rows], ["12", "13", "14"])

    def test_an_empty_bench_says_so_rather_than_collapsing_nothing(self):
        blocks = self.build_xi()
        self.assertIn("none (exactly 11 players)", _text_of(blocks))

    def test_a_side_with_no_bench_loaded_grows_no_bench_section(self):
        # host_bench=None means "not loaded", which is different from "empty".
        blocks = self.build_xi(host_bench=None, guest_bench=None)
        self.assertNotIn("Bench", _text_of(blocks))

    def test_the_change_instructions_survive_onto_the_card(self):
        body = _text_of(self.build_xi())
        self.assertIn("/change <a> <b>", body)
        self.assertIn("/change 2 13", body)

    # ── the fairness verdict ──────────────────────────────────────────
    def test_a_wide_gap_is_called_out_where_it_cannot_be_missed(self):
        blocks = self.lpr.fairness_blocks(
            host_label="Host", guest_label="Guest", host_ovr=900,
            guest_ovr=800, gap_limit=40)
        quotes = [b for b in blocks if b.get("type") == "pullquote"]
        self.assertTrue(quotes)
        self.assertIn("WON'T count", _text_of(quotes))

    def test_a_fair_match_says_the_stats_count(self):
        blocks = self.lpr.fairness_blocks(
            host_label="Host", guest_label="Guest", host_ovr=900,
            guest_ovr=890, gap_limit=40)
        self.assertIn("Stats will count", _text_of(blocks))
        self.assertNotIn("WON'T count", _text_of(blocks))

    def test_a_practice_match_is_unranked_whatever_the_gap(self):
        blocks = self.lpr.fairness_blocks(
            host_label="You", guest_label="Bot", host_ovr=900, guest_ovr=500,
            gap_limit=40, vs_bot=True)
        body = _text_of(blocks)
        self.assertIn("Practice match", body)
        self.assertNotIn("WON'T count", body)

    # ── the toss ──────────────────────────────────────────────────────
    def test_the_toss_prompt_addresses_the_caller(self):
        blocks = self.lpr.toss_call_blocks(self.draft["guest"])
        self.assertIn("Amit", _text_of(blocks))
        self.assertIn("222", str(blocks))

    def test_the_result_names_the_coin_and_the_winner(self):
        blocks = self.lpr.toss_result_blocks("heads", "tails", "guest",
                                             self.draft["host"])
        body = _text_of(blocks)
        self.assertIn("HEADS", body)
        self.assertIn("TAILS", body)
        self.assertIn("Ranjan", body)

    def test_the_election_says_which_way_round(self):
        self.assertIn("BAT", _text_of(
            self.lpr.toss_elected_blocks(self.draft["host"], "bat")))
        self.assertIn("BOWL", _text_of(
            self.lpr.toss_elected_blocks(self.draft["host"], "bowl")))

    def test_the_bot_winning_is_named_but_never_linked(self):
        blocks = self.lpr.toss_elected_blocks(None, "bat", bot_won=True)
        self.assertIn("Bot", _text_of(blocks))
        self.assertNotIn("tg://user", str(blocks))

    # ── a broken builder must not cost the card ───────────────────────
    def test_a_renderer_bug_answers_none_instead_of_raising(self):
        # A draft missing the pieces the card is built from: the flow behind it
        # is a live match setup, so the answer is "send the HTML", never a
        # traceback out of a handler that was about to post a prompt.
        self.assertIsNone(self.lpr.invite_blocks(None))
        self.assertIsNone(self.lpr.playing_xi_blocks(
            vs_bot=False, pitch="Green", host_label="A", guest_label="B",
            host_xi=[("only", "three", "fields")], guest_xi=[]))


# ══════════════════════════════════════════════════════════════════════
# Lets Play — the two renderings have to agree
# ══════════════════════════════════════════════════════════════════════

class LetsPlayRenderersAgreeTests(unittest.TestCase):
    """Both renderings are live, so they must not describe different matches."""

    class _Player:
        def __init__(self, name, rating, category="Batsman"):
            self.name, self.rating, self.category = name, rating, category

    class _Entry:
        def __init__(self, rid):
            self.id = rid

    def pairs(self, count, base=80):
        return [(self._Entry(i), self._Player(f"Player {i}", base + i))
                for i in range(1, count + 1)]

    def setUp(self):
        from handlers import letsplay
        self.lp = letsplay
        self.draft = {"host": {"name": "Ranjan", "tg_id": 111},
                      "guest": {"name": "Amit", "tg_id": 222},
                      "pitch_type": "Hard", "invite_id": 1}

    def test_the_same_eleven_names_in_the_same_order(self):
        pairs = self.pairs(11)
        html_card = self.lp._show_xi_text(self.draft, pairs, pairs)
        rows = self.lp._xi_rows(pairs)
        for position, (name, _rating, _role, _badge) in enumerate(rows, 1):
            self.assertIn(name, html_card)
            self.assertEqual(name, f"Player {position}")

    def test_both_renderings_reach_the_same_fairness_verdict(self):
        # One side stacked, the other not: the HTML says the match won't count,
        # and the facts the blocks are built from have to say the same.
        strong = self.pairs(11, base=90)
        weak = self.pairs(11, base=20)
        html_card = self.lp._stats_fairness_note(strong, weak)
        facts = self.lp._fairness_facts(strong, weak)
        self.assertEqual("WON'T count" in html_card,
                         facts["gap"] >= facts["gap_limit"])

    def test_a_level_match_counts_in_both(self):
        pairs = self.pairs(11)
        html_card = self.lp._stats_fairness_note(pairs, pairs)
        facts = self.lp._fairness_facts(pairs, pairs)
        self.assertNotIn("WON'T count", html_card)
        self.assertLess(facts["gap"], facts["gap_limit"])

    def test_the_block_card_builds_from_a_real_draft(self):
        pairs = self.pairs(11)
        blocks = self.lp._show_xi_blocks(self.draft, pairs, pairs,
                                         host_bench=[], guest_bench=[])
        self.assertIsNotNone(blocks)
        self.assertIn("Ranjan", _text_of(blocks))
        self.assertIn("Amit", _text_of(blocks))

    def test_html_is_stripped_rather_than_printed_as_angle_brackets(self):
        self.assertEqual(self.lp._strip_html("<b>Trait Boost:</b> 84 &amp; up"),
                         "Trait Boost: 84 & up")
        self.assertIsNone(self.lp._strip_html(""))
        self.assertIsNone(self.lp._strip_html("<i></i>"))


# ══════════════════════════════════════════════════════════════════════
# The Challenge League's setup cards
# ══════════════════════════════════════════════════════════════════════

class ChallengeCardTests(unittest.TestCase):

    def setUp(self):
        from services import challenge_rich
        self.chr_ = challenge_rich
        self.host = {"name": "Ranjan", "tg_id": 111}
        self.target = {"name": "Amit", "tg_id": 222}

    def test_the_team_picker_lists_who_has_already_chosen(self):
        blocks = self.chr_.team_picker_blocks(
            title="IPL Battle", player=self.target, league_name="IPL",
            status_lines=["✅ <b>Ranjan</b> selected <b>Mumbai Indians</b>."])
        body = _text_of(blocks)
        self.assertIn("Mumbai Indians", body)
        # The tick came in as HTML; a block carries its own formatting.
        self.assertNotIn("<b>", body)

    def test_the_picker_addresses_whoever_is_on_the_clock(self):
        blocks = self.chr_.team_picker_blocks(
            title="IPL Battle", player=self.target, league_name="IPL")
        self.assertIn("Amit", _text_of(blocks))
        self.assertIn("222", str(blocks))

    def test_the_pitch_menu_is_a_table_of_surfaces(self):
        blocks = self.chr_.pitch_prompt_blocks(
            title="IPL Battle", host=self.host, host_team="MI",
            target_team="CSK",
            pitches=[("Green", "seamers dominate"), ("Flat", "run-fest")])
        table = _tables(blocks)[0]
        self.assertEqual(len(table["cells"]), 3)       # header + two surfaces
        body = _text_of(blocks)
        self.assertIn("seamers dominate", body)
        self.assertIn("run-fest", body)

    def test_a_surface_with_no_description_still_gets_a_row(self):
        blocks = self.chr_.pitch_prompt_blocks(
            title="IPL Battle", host=self.host, host_team="MI",
            target_team="CSK", pitches=[("Mystery", None)])
        self.assertIn("Mystery", _text_of(blocks))

    def test_the_locked_pitch_card_carries_its_report(self):
        blocks = self.chr_.pitch_locked_blocks(
            title="IPL Battle", host_team="MI", target_team="CSK",
            pitch="Green", description="seamers dominate",
            report="<b>Pitch Report</b>\nOvercast, grass left on.",
            locked_note="<i>Fixed by the fixture.</i>")
        body = _text_of(blocks)
        self.assertIn("Overcast", body)
        self.assertIn("Fixed by the fixture", body)
        self.assertNotIn("<i>", body)

    def test_the_created_card_pairs_each_captain_with_their_franchise(self):
        blocks = self.chr_.created_blocks(
            title="IPL — CHALLENGE", host=self.host, target=self.target,
            host_team="Mumbai Indians", target_team="Chennai Kings",
            host_code="MI", target_code="CSK")
        rows = _tables(blocks)[0]["cells"]
        self.assertIn("Ranjan", _text_of(rows[0]))
        self.assertIn("Mumbai Indians", _text_of(rows[0]))
        self.assertIn("Amit", _text_of(rows[1]))
        self.assertIn("Chennai Kings", _text_of(rows[1]))

    def test_the_bots_xi_is_collapsed_rather_than_pushing_the_buttons_away(self):
        blocks = self.chr_.created_blocks(
            title="IPL — CHALLENGE", host=self.host, target=self.target,
            host_team="MI", target_team="CSK",
            bot_xi=[f"{i}. Bot Player {i}" for i in range(1, 12)])
        collapsed = _details(blocks)
        self.assertTrue(collapsed, "eleven names belong behind a tap")
        self.assertIn("Bot Player 11", _text_of(collapsed))

    def test_a_human_match_grows_no_bot_section(self):
        blocks = self.chr_.created_blocks(
            title="IPL — CHALLENGE", host=self.host, target=self.target,
            host_team="MI", target_team="CSK")
        self.assertFalse(_details(blocks))

    def test_a_renderer_bug_answers_none_instead_of_raising(self):
        self.assertIsNone(self.chr_.pitch_prompt_blocks(
            title="x", host={}, host_team="a", target_team="b",
            pitches=[("only-one-field",)]))

    def test_plain_unwraps_html_without_losing_the_words(self):
        self.assertEqual(self.chr_.plain("<b>Hard</b> &amp; true"), "Hard & true")
        self.assertIsNone(self.chr_.plain(None))


if __name__ == "__main__":
    unittest.main()
