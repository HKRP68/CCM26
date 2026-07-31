import unittest

from services.maintenance_service import (
    is_command_update,
    is_miniapp_path,
    should_block_miniapp_path,
    should_block_update,
    should_reply_with_maintenance,
)


MAINTENANCE_ON = {"is_maintenance": True, "maintenance_bypass_ids": ""}


class DummyUser:
    def __init__(self, user_id):
        self.id = user_id


class DummyMessage:
    def __init__(self, text):
        self.text = text


class DummyUpdate:
    def __init__(self, text=None, user_id=123, callback_query=None):
        self.message = DummyMessage(text) if text is not None else None
        self.effective_user = DummyUser(user_id) if user_id is not None else None
        self.callback_query = callback_query


class MaintenanceServiceTests(unittest.TestCase):
    def test_command_messages_are_blocked_with_maintenance_reply(self):
        update = DummyUpdate("/playmatch")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertTrue(is_command_update(update))
        self.assertTrue(should_reply_with_maintenance(update))

    def test_group_chatter_is_blocked_silently_during_maintenance(self):
        update = DummyUpdate("hello everyone")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertFalse(is_command_update(update))
        self.assertFalse(should_reply_with_maintenance(update))

    def test_commands_addressed_to_bot_are_treated_as_commands(self):
        update = DummyUpdate("/playmatch@CricketCardBot")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertTrue(is_command_update(update, "CricketCardBot"))
        self.assertTrue(should_reply_with_maintenance(update, "CricketCardBot"))

    def test_commands_addressed_to_other_bot_are_blocked_silently(self):
        update = DummyUpdate("/help@OtherBot")

        self.assertTrue(should_block_update(update, MAINTENANCE_ON))
        self.assertFalse(is_command_update(update, "CricketCardBot"))
        self.assertFalse(should_reply_with_maintenance(update, "CricketCardBot"))

    def test_commands_addressed_to_this_bot_match_case_insensitively(self):
        update = DummyUpdate("/help@cricketcardbot")

        self.assertTrue(is_command_update(update, "CricketCardBot"))
        self.assertTrue(should_reply_with_maintenance(update, "CricketCardBot"))

    def test_bypassed_user_is_not_blocked(self):
        update = DummyUpdate("/playmatch", user_id=999)
        cfg = {"is_maintenance": True, "maintenance_bypass_ids": "999"}

        self.assertFalse(should_block_update(update, cfg))

    def test_callback_queries_are_not_blocked(self):
        update = DummyUpdate(text=None, callback_query=object())

        self.assertFalse(should_block_update(update, MAINTENANCE_ON))


MAINTENANCE_OFF = {"is_maintenance": False, "maintenance_bypass_ids": ""}


class MiniAppMaintenanceTests(unittest.TestCase):
    """The Mini App is locked by the same switch that blocks bot commands."""

    def test_miniapp_features_are_blocked_during_maintenance(self):
        for path in ("/api/webapp/init", "/api/webapp/spin", "/api/webapp/daily",
                     "/api/webapp/packs/open", "/api/webapp/market/buy/7",
                     "/api/webapp/quests/claim_all", "/api/webapp/xi/reorder",
                     "/api/webapp/quickmatch/start", "/api/fantasy/save",
                     "/api/adsgram/reward", "/api/ipl16/share"):
            with self.subTest(path=path):
                self.assertTrue(should_block_miniapp_path(path, 123, MAINTENANCE_ON))

    def test_live_match_endpoints_stay_open_so_matches_can_finish(self):
        for path in ("/api/webapp/match/init", "/api/webapp/match/deliver",
                     "/api/webapp/match/play-shot", "/api/webapp/match/scorecard",
                     "/api/webapp/quickmatch/phase",
                     "/api/webapp/quickmatch/abandon"):
            with self.subTest(path=path):
                self.assertFalse(should_block_miniapp_path(path, 123, MAINTENANCE_ON))

    def test_non_miniapp_paths_are_never_blocked_here(self):
        # The bot's own REST match API, the admin panel, and static assets are
        # outside this gate: /api/match/* is live-match play, and the admin
        # panel must stay reachable to turn maintenance back off.
        for path in ("/api/match/state", "/api/match/action", "/webapp",
                     "/webapp/player-card/12", "/card-template", "/cricket",
                     "/api/event-media/3", "/health"):
            with self.subTest(path=path):
                self.assertFalse(is_miniapp_path(path))
                self.assertFalse(should_block_miniapp_path(path, 123, MAINTENANCE_ON))

    def test_nothing_is_blocked_when_maintenance_is_off(self):
        self.assertFalse(should_block_miniapp_path("/api/webapp/spin", 123,
                                                   MAINTENANCE_OFF))

    def test_bypassed_admin_keeps_full_miniapp_access(self):
        cfg = {"is_maintenance": True, "maintenance_bypass_ids": "999, 1000"}

        self.assertFalse(should_block_miniapp_path("/api/webapp/spin", 999, cfg))
        self.assertTrue(should_block_miniapp_path("/api/webapp/spin", 123, cfg))

    def test_unidentified_caller_is_blocked(self):
        # No verifiable initData → no bypass claim → blocked.
        self.assertTrue(should_block_miniapp_path("/api/webapp/spin", None,
                                                  MAINTENANCE_ON))


if __name__ == "__main__":
    unittest.main()
