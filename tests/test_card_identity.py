"""Who a team is, and what it looks like, resolved in one place.

This exists because the crest and the colour used to be resolved inside
``scorecard_delivery``, so every mode that called a card generator directly —
/cipl, the Super Over, /sim — silently drew unbranded cards and nothing said
so. The rules worth locking down are the ones that made that possible:

  • a **user id beats a team name**, because names are not reliable keys: a
    /sim side can be called "🤖 Sim XI", "@someone" or "Someone's XI"
  • a **ChallengeTeam** franchise resolves too, since CIPL sides are not user
    teams
  • **an event crest beats the manager's own**, because in a tournament nobody
    is playing their team — they are playing a franchise the admins entered,
    and a personal crest on it is the wrong badge
  • **nothing raises.** A card with no crest is a detail; a card that fails to
    render is the card.
"""

import io
import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_TG_IDS = itertools.count(83_001)
_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.asset_store",
                 "services.card_identity", "services.team_logo_service",
                 "services.config_service")


def _png(size=(320, 320), color=(200, 30, 40, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from services import card_identity, team_logo_service
        self.ci = card_identity
        self.tls = team_logo_service
        self.session = get_session()
        team_logo_service.invalidate_cache()
        self._keys = []

    def tearDown(self):
        for key in self._keys:
            try:
                self.tls._forget(self.session, key)
            except Exception:
                pass
        self.session.rollback()
        self.session.close()
        self.tls.invalidate_cache()

    def _user(self, *, team_name=None, colour=None, with_logo=False):
        from models import User
        user = User(telegram_id=next(_TG_IDS), first_name="Tester",
                    team_name=team_name or f"Team {next(_SEQ)}",
                    team_colour=colour)
        self.session.add(user)
        self.session.commit()
        if with_logo:
            result = self.tls.submit_logo(self.session, user, _png(),
                                          file_id="F", ignore_limits=True)
            self.assertTrue(result["ok"], result)
            self.tls.approve_request(self.session, result["request"])
            self.session.commit()
            self._keys.append(user.team_logo_asset_key)
        return user

    def _player(self, name, rating=85):
        from models import Player
        player = Player(name=name, rating=rating, category="Batsman",
                        country="India", bat_hand="RHB", bowl_hand="R",
                        bowl_style="Medium", bat_rating=rating,
                        bowl_rating=40)
        self.session.add(player)
        self.session.commit()
        return player


class ColourResolutionTests(_Base):
    def test_a_users_own_colour_wins(self):
        user = self._user(colour="#aa001b")
        self.assertEqual(
            self.ci.team_colour(self.session, user_id=user.id), "#aa001b")

    def test_a_team_with_no_colour_returns_none(self):
        user = self._user()
        self.assertIsNone(self.ci.team_colour(self.session, user_id=user.id))

    def test_the_name_resolves_when_no_id_is_given(self):
        user = self._user(team_name="Rome Gladiators", colour="#aa001b")
        del user
        self.assertEqual(
            self.ci.team_colour(self.session, team_name="Rome Gladiators"),
            "#aa001b")

    def test_a_stored_junk_colour_is_ignored_rather_than_drawn(self):
        user = self._user(colour="not-a-colour")
        self.assertIsNone(self.ci.team_colour(self.session, user_id=user.id))

    def test_a_challenge_team_colour_resolves(self):
        """CIPL sides are franchises, not user teams — and ChallengeTeam has
        carried a primary_color all along that no card ever drew."""
        from models import ChallengeLeague, ChallengeMode, ChallengeTeam
        mode = ChallengeMode(name=f"Mode {next(_SEQ)}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_SEQ)}")
        self.session.add(league)
        self.session.flush()
        team = ChallengeTeam(league_id=league.id, name=f"Franchise {next(_SEQ)}",
                             primary_color="#0065b3")
        self.session.add(team)
        self.session.commit()
        self.assertEqual(
            self.ci.team_colour(self.session, team_name=team.name), "#0065b3")

    def test_an_unknown_team_is_not_an_error(self):
        self.assertIsNone(self.ci.team_colour(self.session, team_name="Nobody"))
        self.assertIsNone(self.ci.team_colour(self.session))


class LogoResolutionTests(_Base):
    def test_an_approved_logo_resolves_by_user_id(self):
        user = self._user(with_logo=True)
        found = self.ci.team_logo_png(self.session, user_id=user.id)
        self.assertTrue(found and found[:4] == b"\x89PNG")

    def test_it_resolves_by_name_when_no_id_is_given(self):
        user = self._user(team_name="Mumbai Marathas", with_logo=True)
        del user
        found = self.ci.team_logo_png(self.session, team_name="Mumbai Marathas")
        self.assertTrue(found and found[:4] == b"\x89PNG")

    def test_the_id_wins_over_a_name_that_belongs_to_somebody_else(self):
        """The reason the id is preferred: /sim and /cipl pass display names
        that may belong to a different team entirely."""
        mine = self._user(team_name="Mine", with_logo=True)
        self._user(team_name="Theirs", with_logo=True)
        found = self.ci.team_logo_png(self.session, user_id=mine.id,
                                      team_name="Theirs")
        self.assertEqual(found, self.tls.logo_bytes_for_key(mine.team_logo_asset_key))

    def test_a_team_with_no_logo_returns_none(self):
        user = self._user()
        self.assertIsNone(self.ci.team_logo_png(self.session, user_id=user.id))

    def test_an_unknown_team_is_not_an_error(self):
        self.assertIsNone(self.ci.team_logo_png(self.session, team_name="Nobody"))
        self.assertIsNone(self.ci.team_logo_png(self.session))


class PortraitTests(_Base):
    def test_an_unknown_player_is_not_an_error(self):
        self.assertIsNone(
            self.ci.potm_portrait_png(self.session, player_id=999_999))

    def test_no_identity_at_all_returns_none(self):
        self.assertIsNone(self.ci.potm_portrait_png(self.session))

    def test_a_missing_session_is_not_an_error(self):
        self.assertIsNone(self.ci.potm_portrait_png(None, player_id=1))


class PlayerCardTests(_Base):
    """The award winner's collectible card, which goes out as a second photo.

    The summary card has already landed by the time this is asked for, so every
    way of not finding a player has to read as "no second photo" rather than as
    an exception on a finished match.
    """

    def test_an_unknown_player_is_not_an_error(self):
        self.assertIsNone(self.ci.potm_card_png(self.session, player_id=999_999))

    def test_an_unknown_name_is_not_an_error(self):
        self.assertIsNone(
            self.ci.potm_card_png(self.session, name="Nobody At All"))

    def test_no_identity_at_all_returns_none(self):
        self.assertIsNone(self.ci.potm_card_png(self.session))

    def test_a_missing_session_is_not_an_error(self):
        self.assertIsNone(self.ci.potm_card_png(None, player_id=1))

    def test_a_render_that_blows_up_returns_none(self):
        """``generate_card`` reaches template config, asset storage and PIL. If
        any of that fails the match must not notice."""
        import services.card_generator as cg
        player = self._player("Exploding Batter")
        original = cg.generate_card
        cg.generate_card = lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIsNone(
                self.ci.potm_card_png(self.session, player_id=player.id))
        finally:
            cg.generate_card = original

    def test_a_known_player_is_rendered(self):
        player = self._player("Card Holder")
        png = self.ci.potm_card_png(self.session, player_id=player.id)
        self.assertTrue(png, "no card came back for a real player")
        self._assert_image(png)

    def test_the_name_resolves_when_no_id_is_given(self):
        self._player("Named Only")
        png = self.ci.potm_card_png(self.session, name="  named only  ")
        self.assertTrue(png, "a POTM known only by name got no card")
        self._assert_image(png)

    def _assert_image(self, blob):
        from PIL import Image
        image = Image.open(io.BytesIO(blob))
        # The card at the size people collect it. The POTM strip draws its own
        # scaled-down copy; this is the source it scales, so it must not come
        # back already shrunk. The exact dimensions depend on which of
        # generate_card's three paths ran
        # (custom art, website template, or — here, with no template
        # configured — the procedural tier card), so this only pins the floor.
        self.assertGreaterEqual(image.width, 320)
        self.assertGreaterEqual(image.height, 320)


class EventCrestTests(_Base):
    """The tournament's crest, not the manager's.

    This is the bug the precedence exists for: a CIPL or tournament side always
    has a user id behind it (somebody is playing it), so trying the user first
    meant every league card wore whatever that person had set with
    /setteamlogo — their own team's crest, on a franchise they do not own.
    """

    def setUp(self):
        super().setUp()
        self._files = []

    def tearDown(self):
        for path in self._files:
            try:
                os.unlink(path)
            except OSError:
                pass
        super().tearDown()

    def _logo_url(self, colour=(10, 200, 90, 255)):
        """A crest on disk where the website would have put one."""
        from services import asset_store
        directory = os.path.join(asset_store.PROJECT_ROOT, "static",
                                 "challenge_leagues")
        os.makedirs(directory, exist_ok=True)
        name = f"test_{next(_SEQ)}_{os.getpid()}.png"
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(_png(color=colour))
        self._files.append(path)
        return f"/static/challenge_leagues/{name}", path

    def _challenge_team(self, name, *, logo_url=None, colour=None):
        from models import ChallengeLeague, ChallengeMode, ChallengeTeam
        mode = ChallengeMode(name=f"Mode {next(_SEQ)}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_SEQ)}")
        self.session.add(league)
        self.session.flush()
        team = ChallengeTeam(league_id=league.id, name=name,
                             logo_url=logo_url, primary_color=colour)
        self.session.add(team)
        self.session.commit()
        return team

    def _tournament_team(self, name, *, logo_url=None,
                         challenge_team_id=None, user_tg_id=None):
        from models import Tournament, TournamentTeam
        tour = Tournament(name=f"Tournament {next(_SEQ)}")
        self.session.add(tour)
        self.session.flush()
        team = TournamentTeam(tournament_id=tour.id, name=name,
                              logo_url=logo_url,
                              challenge_team_id=challenge_team_id,
                              user_tg_id=user_tg_id)
        self.session.add(team)
        self.session.commit()
        return tour, team

    def test_a_tournament_crest_beats_the_managers_own(self):
        user = self._user(team_name="Franchise XI", with_logo=True)
        url, _path = self._logo_url()
        tour, team = self._tournament_team("Franchise XI", logo_url=url)
        found = self.ci.team_logo_png(
            self.session, user_id=user.id, team_name="Franchise XI",
            tournament_id=tour.id, tournament_team_id=team.id)
        self.assertEqual(found, _png(color=(10, 200, 90, 255)))

    def test_a_tournament_colour_beats_the_managers_own(self):
        """The colour comes off the franchise behind the tournament row —
        ``TournamentTeam`` carries a crest of its own but not a colour — and it
        has to follow the same precedence as the crest. A card branded with the
        franchise's badge and the manager's colour names two different sides.
        """
        user = self._user(team_name="Colour XI", colour="#aa001b")
        franchise = self._challenge_team("Colour XI", colour="#0065b3")
        tour, team = self._tournament_team("Colour XI",
                                           challenge_team_id=franchise.id)
        self.assertEqual(
            self.ci.team_colour(self.session, user_id=user.id,
                                team_name="Colour XI", tournament_id=tour.id,
                                tournament_team_id=team.id),
            "#0065b3")

    def test_a_challenge_team_id_is_enough_on_its_own(self):
        """What /cipl passes: the franchise id out of the match state."""
        user = self._user(team_name="League XI", with_logo=True)
        url, _path = self._logo_url(colour=(200, 10, 90, 255))
        team = self._challenge_team("League XI", logo_url=url)
        found = self.ci.team_logo_png(
            self.session, user_id=user.id, team_name="League XI",
            challenge_team_id=team.id)
        self.assertEqual(found, _png(color=(200, 10, 90, 255)))

    def test_a_tournament_row_with_no_crest_falls_through_to_its_franchise(self):
        """A TournamentTeam copies the franchise's logo at entry, but a row
        entered before that copy existed still has the franchise behind it."""
        url, _path = self._logo_url(colour=(30, 30, 200, 255))
        franchise = self._challenge_team("Inherited XI", logo_url=url)
        tour, team = self._tournament_team("Inherited XI",
                                           challenge_team_id=franchise.id)
        found = self.ci.team_logo_png(self.session, team_name="Inherited XI",
                                      tournament_id=tour.id,
                                      tournament_team_id=team.id)
        self.assertEqual(found, _png(color=(30, 30, 200, 255)))

    def test_a_lets_play_side_resolves_by_its_telegram_id(self):
        """A Lets Play team *is* a user, so the tournament row is keyed on the
        Telegram id rather than on a franchise."""
        user = self._user(team_name="Solo XI", with_logo=True)
        url, _path = self._logo_url(colour=(240, 190, 20, 255))
        tour, _team = self._tournament_team("Something Else", logo_url=url,
                                            user_tg_id=user.telegram_id)
        found = self.ci.team_logo_png(self.session, user_id=user.id,
                                      team_name="Solo XI",
                                      tournament_id=tour.id)
        self.assertEqual(found, _png(color=(240, 190, 20, 255)))

    def test_without_event_context_the_manager_keeps_their_crest(self):
        """The guard on the whole feature: a friendly is untouched."""
        user = self._user(team_name="Friendly XI", with_logo=True)
        url, _path = self._logo_url()
        self._tournament_team("Friendly XI", logo_url=url)
        found = self.ci.team_logo_png(self.session, user_id=user.id,
                                      team_name="Friendly XI")
        self.assertEqual(found,
                         self.tls.logo_bytes_for_key(user.team_logo_asset_key))

    def test_an_event_with_no_crest_falls_back_to_the_managers(self):
        user = self._user(team_name="Crestless XI", with_logo=True)
        tour, team = self._tournament_team("Crestless XI")
        found = self.ci.team_logo_png(
            self.session, user_id=user.id, team_name="Crestless XI",
            tournament_id=tour.id, tournament_team_id=team.id)
        self.assertEqual(found,
                         self.tls.logo_bytes_for_key(user.team_logo_asset_key))

    def test_a_crest_wiped_off_disk_comes_back(self):
        """The deploy case. The file is gone; the row still points at it."""
        from services import asset_store
        url, path = self._logo_url(colour=(90, 20, 160, 255))
        asset_store.put(path)
        os.unlink(path)
        tour, team = self._tournament_team("Durable XI", logo_url=url)
        found = self.ci.team_logo_png(self.session, team_name="Durable XI",
                                      tournament_id=tour.id,
                                      tournament_team_id=team.id)
        self.assertEqual(found, _png(color=(90, 20, 160, 255)))
        asset_store.drop(path)

    def test_an_unknown_tournament_is_not_an_error(self):
        self.assertIsNone(self.ci.team_logo_png(self.session, team_name="Ghost",
                                                tournament_id=999_999))


class SummaryVisualsTests(_Base):
    def test_it_returns_every_key_the_card_takes(self):
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="A", inn2_team="B", include_style=False)
        self.assertEqual(
            set(visuals),
            {"inn1_color", "inn2_color", "inn1_logo_png", "inn2_logo_png",
             "potm_photo_png"})

    def test_the_potm_card_is_only_fetched_when_asked_for(self):
        """The card is a full render, so it is not drawn for a caller that is
        not going to composite it."""
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="A", include_style=False)
        self.assertNotIn("potm_card_png", visuals)
        player = self._player("Showcase Batter")
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="A", potm_player_id=player.id,
            potm_card=True, include_style=False)
        self.assertIn("potm_card_png", visuals)
        self.assertTrue(visuals["potm_card_png"])

    def test_an_event_brands_both_sides(self):
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="A", inn2_team="B", tournament_id=999_999,
            include_style=False)
        self.assertEqual(
            set(visuals),
            {"inn1_color", "inn2_color", "inn1_logo_png", "inn2_logo_png",
             "potm_photo_png"})

    def test_style_is_included_on_request(self):
        visuals = self.ci.summary_visuals(self.session, inn1_team="A")
        self.assertIn("text_settings", visuals)
        self.assertIn("dynamic_flourish", visuals)

    def test_two_teams_that_chose_the_same_colour_are_separated(self):
        one = self._user(team_name="Clash One", colour="#aa001b")
        two = self._user(team_name="Clash Two", colour="#aa001b")
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="Clash One", inn2_team="Clash Two",
            inn1_user_id=one.id, inn2_user_id=two.id, include_style=False)
        self.assertEqual(visuals["inn1_color"], "#aa001b")
        self.assertNotEqual(visuals["inn2_color"], "#aa001b")

    def test_distinct_choices_are_both_honoured(self):
        one = self._user(team_name="Red Side", colour="#aa001b")
        two = self._user(team_name="Blue Side", colour="#0065b3")
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="Red Side", inn2_team="Blue Side",
            inn1_user_id=one.id, inn2_user_id=two.id, include_style=False)
        self.assertEqual(visuals["inn1_color"], "#aa001b")
        self.assertEqual(visuals["inn2_color"], "#0065b3")

    def test_the_card_renders_from_what_it_returns(self):
        """The contract that matters: whatever comes back has to be spreadable
        straight into the generator."""
        from services.match_summary_card import generate_match_summary
        one = self._user(team_name="Render One", colour="#aa001b", with_logo=True)
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="Render One", inn2_team="Render Two",
            inn1_user_id=one.id, include_style=False)
        png = generate_match_summary(
            inn1_team="Render One", inn1_runs=156, inn1_wickets=7, inn1_overs="20",
            inn2_team="Render Two", inn2_runs=158, inn2_wickets=4, inn2_overs="16.5",
            winner_name="Render Two", win_margin_text="by 6 wickets",
            overs_total=20, **visuals)
        self.assertTrue(png and png[:4] == b"\x89PNG")


class InningsVisualsTests(_Base):
    def test_it_returns_every_key_the_innings_cards_take(self):
        visuals = self.ci.innings_visuals(
            self.session, team_name="A", include_style=False)
        self.assertEqual(set(visuals), {"accent_hex", "team_logo_png"})

    def test_a_team_colour_becomes_the_accent(self):
        user = self._user(team_name="Accent XI", colour="#aa001b")
        visuals = self.ci.innings_visuals(
            self.session, team_name="Accent XI", user_id=user.id,
            include_style=False)
        self.assertEqual(visuals["accent_hex"], "#aa001b")


if __name__ == "__main__":
    unittest.main()
