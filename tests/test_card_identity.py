"""Who a team is, and what it looks like, resolved in one place.

This exists because the crest and the colour used to be resolved inside
``scorecard_delivery``, so every mode that called a card generator directly —
/cipl, the Super Over, /sim — silently drew unbranded cards and nothing said
so. The rules worth locking down are the ones that made that possible:

  • a **user id beats a team name**, because names are not reliable keys: a
    /sim side can be called "🤖 Sim XI", "@someone" or "Someone's XI"
  • a **ChallengeTeam** franchise resolves too, since CIPL sides are not user
    teams
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
        # The card at the size people collect it, not the thumbnail that
        # compositing it into the 125px POTM strip would have produced. The
        # exact dimensions depend on which of generate_card's three paths ran
        # (custom art, website template, or — here, with no template
        # configured — the procedural tier card), so this only pins the floor.
        self.assertGreaterEqual(image.width, 320)
        self.assertGreaterEqual(image.height, 320)


class SummaryVisualsTests(_Base):
    def test_it_returns_every_key_the_card_takes(self):
        visuals = self.ci.summary_visuals(
            self.session, inn1_team="A", inn2_team="B", include_style=False)
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
