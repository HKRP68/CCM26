"""Milestone captions and send-method selection in event_media_service.

fire_event_media is called from a live match and must never raise — a broken
celebration must not cost anyone their over. The specific traps:

  * captions are admin-written, so they may contain stray braces. str.format
    would raise on those; the substitution has to be str.replace, exactly as
    services/commentary_service._render does it and for the same reason.
  * rows created before captions existed have caption=None and must behave
    exactly as they always did — send_animation, no caption argument.
  * a caption with no media is a valid configuration (an admin who wants a
    message but no image) and must go out as a plain message.
"""

import asyncio
import unittest
from types import SimpleNamespace

from services import event_media_service as ems


class _Bot:
    def __init__(self):
        self.calls = []

    def _record(self, name):
        async def _fn(**kwargs):
            self.calls.append((name, kwargs))
        return _fn

    def __getattr__(self, name):
        if name.startswith("send_"):
            return self._record(name)
        raise AttributeError(name)


class _Ctx:
    def __init__(self):
        self.bot = _Bot()
        self.bot_data = {}


def _row(**kw):
    base = dict(id=1, event_key="fifty", source_type="url",
                source="http://x/a.gif", caption=None, weight=1,
                enabled=True, media_type=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _fire(ctx, rows, key="fifty", fields=None, cooldown=False):
    """Run fire_event_media against a fixed row set, with no DB."""
    class _Q:
        def filter(self, *a, **k): return self
        def all(self): return rows

    class _Session:
        def query(self, *a, **k): return _Q()
        def close(self): pass

    import sys
    fake_db = SimpleNamespace(get_session=lambda: _Session())
    fake_models = SimpleNamespace(EventMedia=SimpleNamespace(
        event_key=SimpleNamespace(__eq__=lambda s, o: True),
        enabled=SimpleNamespace(__eq__=lambda s, o: True)))
    real_db = sys.modules.get("database")
    real_models = sys.modules.get("models")
    sys.modules["database"] = fake_db
    sys.modules["models"] = fake_models
    try:
        asyncio.run(ems.fire_event_media(ctx, 42, key, fields=fields,
                                         cooldown=cooldown))
    finally:
        if real_db is not None:
            sys.modules["database"] = real_db
        if real_models is not None:
            sys.modules["models"] = real_models
    return ctx.bot.calls


class CaptionRenderTests(unittest.TestCase):
    def test_placeholders_are_filled(self):
        self.assertEqual(
            ems.render_caption("{player} made {runs}!",
                               {"player": "Kohli", "runs": 52}),
            "Kohli made 52!")

    def test_a_stray_brace_does_not_raise(self):
        # str.format would blow up here; str.replace must not.
        self.assertEqual(
            ems.render_caption("50 up {not a field}", {"player": "Kohli"}),
            "50 up {not a field}")

    def test_an_unknown_placeholder_is_left_as_written(self):
        self.assertEqual(ems.render_caption("{mystery}", {"player": "x"}),
                         "{mystery}")

    def test_no_caption_is_empty_not_none(self):
        self.assertEqual(ems.render_caption(None, {"player": "x"}), "")


class SendKindTests(unittest.TestCase):
    def test_a_plain_row_still_sends_as_an_animation(self):
        self.assertEqual(ems._send_kind(_row()), "animation")

    def test_photos_are_detected_from_the_extension(self):
        self.assertEqual(ems._send_kind(_row(source="http://x/a.png")), "photo")

    def test_videos_are_detected_from_the_extension(self):
        self.assertEqual(ems._send_kind(_row(source="http://x/a.mp4")), "video")

    def test_media_type_wins_for_a_telegram_file_id(self):
        self.assertEqual(
            ems._send_kind(_row(source_type="telegram", source="AgACx",
                                media_type="photo")), "photo")


class FireTests(unittest.TestCase):
    def test_a_row_without_a_caption_behaves_exactly_as_before(self):
        calls = _fire(_Ctx(), [_row()])
        self.assertEqual(len(calls), 1)
        name, kwargs = calls[0]
        self.assertEqual(name, "send_animation")
        self.assertNotIn("caption", kwargs)

    def test_a_caption_rides_along_with_the_media(self):
        calls = _fire(_Ctx(), [_row(caption="{player} hits 50")],
                      fields={"player": "Kohli"})
        name, kwargs = calls[0]
        self.assertEqual(name, "send_animation")
        self.assertEqual(kwargs["caption"], "Kohli hits 50")

    def test_a_photo_row_uses_send_photo(self):
        calls = _fire(_Ctx(), [_row(source="http://x/a.jpg", caption="hi")])
        self.assertEqual(calls[0][0], "send_photo")

    def test_a_caption_with_no_media_goes_out_as_a_message(self):
        calls = _fire(_Ctx(), [_row(source="", caption="FIFTY for {player}!")],
                      fields={"player": "Rohit"})
        name, kwargs = calls[0]
        self.assertEqual(name, "send_message")
        self.assertEqual(kwargs["text"], "FIFTY for Rohit!")

    def test_no_configured_media_sends_generated_message(self):
        calls = _fire(_Ctx(), [])
        self.assertEqual(calls[0][0], "send_message")
        self.assertIn("HALF CENTURY", calls[0][1]["text"])

    def test_a_send_failure_is_swallowed(self):
        ctx = _Ctx()

        async def _boom(**kwargs):
            raise RuntimeError("telegram is down")
        ctx.bot.send_animation = _boom
        # Must not propagate — a failed celebration cannot break the over.
        self.assertEqual(_fire(ctx, [_row()]), [])

    def test_the_cooldown_can_be_bypassed_for_milestones(self):
        ctx = _Ctx()
        _fire(ctx, [_row()], cooldown=True)
        _fire(ctx, [_row()], cooldown=True)
        self.assertEqual(len(ctx.bot.calls), 1, "cooldown should suppress the 2nd")
        ctx2 = _Ctx()
        _fire(ctx2, [_row()], cooldown=False)
        _fire(ctx2, [_row()], cooldown=False)
        self.assertEqual(len(ctx2.bot.calls), 2,
                         "milestones must not be swallowed by the anti-spam window")


class EventKeyTests(unittest.TestCase):
    def test_the_original_keys_are_all_still_there(self):
        keys = {k for k, _l, _d in ems.EVENT_KEYS}
        for original in ("dot_ball", "four", "six", "wicket", "wide", "no_ball",
                         "fifty", "century", "implant", "hattrick",
                         "maiden_over"):
            self.assertIn(original, keys)

    def test_the_new_milestone_keys_are_registered(self):
        keys = {k for k, _l, _d in ems.EVENT_KEYS}
        for added in ("three_fer", "five_fer", "team_100", "team_150",
                      "team_200", "partnership_50", "partnership_100",
                      "match_won", "impact_player"):
            self.assertIn(added, keys)

    def test_every_milestone_the_detector_emits_is_configurable(self):
        from services import milestones
        keys = {k for k, _l, _d in ems.EVENT_KEYS}
        emitted = ({k for _n, k in milestones.BAT_MILESTONES}
                   | {k for _n, k in milestones.BOWL_MILESTONES}
                   | {k for _n, k in milestones.PARTNERSHIP_MILESTONES}
                   | {k for _n, k in milestones.TEAM_MILESTONES}
                   | {"hattrick", "match_won"})
        self.assertEqual(emitted - keys, set(),
                         "detector emits a key with no admin UI entry")


if __name__ == "__main__":
    unittest.main()

class AutomaticMilestoneTests(unittest.TestCase):
    def test_century_without_configuration_sends_generated_message(self):
        calls = _fire(_Ctx(), [], key="century", fields={
            "player": "RICHIE RICHARDSON", "runs": 105, "balls": 58,
            "team": "Kushan XI", "opponent": "@zooba07"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "send_message")
        for text in ("CENTURY!", "105 (58)", "181.03", "100 Runs", "Kushan XI", "@zooba07"):
            self.assertIn(text, calls[0][1]["text"])

    def test_disabled_messages_preserve_media(self):
        preference = _row(source_type=ems.MESSAGE_SETTINGS, source="off", enabled=False)
        calls = _fire(_Ctx(), [preference, _row(caption="Custom message")],
                      fields={"player": "Batter", "runs": 50, "balls": 30})
        self.assertEqual(calls[0][0], "send_animation")
        self.assertNotIn("caption", calls[0][1])
        self.assertEqual(_fire(_Ctx(), [preference], fields={"runs": 50}), [])

    def test_generated_values_escape_html(self):
        message = ems.default_milestone_message("century", {"player": "A < B", "balls": 0})
        self.assertIn("A &lt; B", message)
        self.assertNotIn("Strike Rate:", message)  # Unknown stats are omitted.

    def test_each_milestone_has_a_relevant_default(self):
        for key in ems.MILESTONE_MESSAGES:
            self.assertIn("Achievement:", ems.default_milestone_message(key, {}))


class AllEventMessageTests(unittest.TestCase):
    def test_every_selectable_event_has_an_automatic_message(self):
        for key, _, _ in ems.EVENT_KEYS:
            with self.subTest(key=key):
                calls = _fire(_Ctx(), [], key=key)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "send_message")
                self.assertIn("Achievement:", calls[0][1]["text"])

    def test_every_selectable_event_can_be_silenced_independently(self):
        for key, _, _ in ems.EVENT_KEYS:
            with self.subTest(key=key):
                preference = _row(event_key=key, source_type=ems.MESSAGE_SETTINGS,
                                  source="off", enabled=False)
                self.assertEqual(_fire(_Ctx(), [preference], key=key), [])
                preference.source = "on"
                self.assertEqual(_fire(_Ctx(), [preference], key=key)[0][0], "send_message")


class ExpandedPlaceholderTests(unittest.TestCase):
    def test_derived_placeholders_and_unavailable_fields(self):
        fields = ems.caption_values("fifty", {"runs": 52, "balls": 30, "fours": 4, "sixes": 2})
        self.assertEqual(fields["strike_rate"], "173.33")
        self.assertEqual(fields["boundaries"], 6)
        self.assertEqual(fields["achievement"], "50 Runs")
        self.assertEqual(ems.render_caption("{venue}|{innings}|{economy}", fields), "||")

    def test_partnership_and_team_cards_do_not_invent_a_player(self):
        for key in ("partnership_50", "partnership_100", "team_100", "team_150", "team_200"):
            message = ems.default_milestone_message(key, {"team": "Alpha", "partnership": 108})
            self.assertIn("Alpha", message)
            self.assertNotIn("The batter", message)
            self.assertNotIn("Strike Rate", message)
