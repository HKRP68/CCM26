"""Which competition a scorecard belongs to, read off the live match state.

Every card the bot draws resolves its crest at render time, and it used to
resolve it from the team *name* alone. In a tournament that is the wrong
question: a franchise is entered by the admins and merely played by whoever
drew it, so the name matched the manager and the manager's own /setteamlogo
crest went onto a badge they do not own.

The fix is that the payload carries who and what — the owner's user id and the
competition's own row ids — so ``services.card_identity`` can prefer the event's
crest. These pin the reading of that state, which is where it can go quietly
wrong:

  • **a side is found by name, not by innings.** At the end of a chase
    ``bat_team_name`` is the second innings and ``bowl_team_name`` the first,
    and mid-innings it is the other way round. Guessing would brand each side
    with the other's crest.
  • **the maps survive JSON.** The state store round-trips through JSON, which
    turns integer keys into strings, so both spellings have to resolve.
  • **a friendly carries nothing**, which is what keeps a plain /playmatch
    drawing the manager's own crest.
"""

import unittest

from handlers import match


class TeamUserIdTests(unittest.TestCase):
    STATE = {"bat_team_name": "Punjab Kings", "bat_team_id": 11,
             "bowl_team_name": "Mumbai Indians", "bowl_team_id": 22}

    def test_each_side_resolves_to_its_own_owner(self):
        self.assertEqual(match._team_user_id(self.STATE, "Punjab Kings"), 11)
        self.assertEqual(match._team_user_id(self.STATE, "Mumbai Indians"), 22)

    def test_the_lookup_ignores_case_and_padding(self):
        self.assertEqual(match._team_user_id(self.STATE, "  punjab kings "), 11)

    def test_a_side_the_state_does_not_know_is_not_an_error(self):
        self.assertIsNone(match._team_user_id(self.STATE, "Somebody Else"))
        self.assertIsNone(match._team_user_id(self.STATE, ""))
        self.assertIsNone(match._team_user_id(self.STATE, None))


class MappedTeamIdTests(unittest.TestCase):
    def test_an_integer_key_resolves(self):
        state = {"tournament_tteam_by_user": {11: 340}}
        self.assertEqual(
            match._mapped_team_id(state, "tournament_tteam_by_user", 11), 340)

    def test_a_key_that_came_back_from_json_as_a_string_resolves_too(self):
        state = {"tournament_tteam_by_user": {"11": 340}}
        self.assertEqual(
            match._mapped_team_id(state, "tournament_tteam_by_user", 11), 340)

    def test_a_missing_map_is_not_an_error(self):
        self.assertIsNone(match._mapped_team_id({}, "anything", 11))
        self.assertIsNone(
            match._mapped_team_id({"anything": "not a map"}, "anything", 11))
        self.assertIsNone(
            match._mapped_team_id({"anything": {11: 1}}, "anything", None))


class EventIdentityTests(unittest.TestCase):
    BASE = {"bat_team_name": "Punjab Kings", "bat_team_id": 11,
            "bowl_team_name": "Mumbai Indians", "bowl_team_id": 22}

    def test_a_lets_play_fixture_names_its_tournament_side(self):
        state = dict(self.BASE, tournament_id=7,
                     tournament_tteam_by_user={"11": 340, "22": 341})
        self.assertEqual(
            match._event_identity(state, team_name="Punjab Kings"),
            {"tournament_id": 7, "tournament_team_id": 340})

    def test_a_challenge_league_fixture_names_its_franchise(self):
        state = dict(self.BASE, tournament_id=7, league_key="ipl",
                     tournament_team_by_user={11: 91, 22: 92})
        self.assertEqual(
            match._event_identity(state, team_name="Mumbai Indians"),
            {"tournament_id": 7, "league_key": "ipl", "challenge_team_id": 92})

    def test_a_league_match_outside_a_tournament_still_names_its_league(self):
        state = dict(self.BASE, league_key="ipl")
        self.assertEqual(match._event_identity(state, team_name="Punjab Kings"),
                         {"league_key": "ipl"})

    def test_a_friendly_carries_nothing(self):
        """The guard on the whole feature: with no event context the manager's
        own crest is still what a card wears."""
        self.assertEqual(match._event_identity(dict(self.BASE),
                                               team_name="Punjab Kings"), {})

    def test_an_explicit_user_id_is_used_as_given(self):
        """The summary card resolves each side once and passes the id down,
        because the state has already swapped by the time it runs."""
        state = dict(self.BASE, tournament_id=7,
                     tournament_tteam_by_user={11: 340, 22: 341})
        self.assertEqual(match._event_identity(state, user_id=22),
                         {"tournament_id": 7, "tournament_team_id": 341})


if __name__ == "__main__":
    unittest.main()
