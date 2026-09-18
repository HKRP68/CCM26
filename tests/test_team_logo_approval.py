"""Team crests, and the admin approval standing between an upload and a card.

The crest a user uploads is drawn on every scorecard of every match their team
plays, in front of everyone in the chat. So the rules worth locking down are the
ones that keep an unreviewed image off those cards:

  • an upload changes *nothing* until an admin approves it — the team keeps
    whatever crest it had, or none
  • one open request per user; a second upload while one is pending is refused
    rather than queued
  • a rejection carries a reason and drops the bytes
  • a decision is final: two admins pressing at once cannot both decide
  • what the renderers ask for by team name is only ever an approved crest
"""

import io
import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_TG_IDS = itertools.count(71_001)
_TEAM_SEQ = itertools.count(1)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.asset_store",
                 "services.team_logo_service", "services.config_service")


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
        from services import team_logo_service as tls
        self.tls = tls
        self.session = get_session()
        tls.invalidate_cache()
        self._written = []

    def tearDown(self):
        # Clear whatever the tests wrote through asset_store, so a stray file
        # cannot make a later assertion pass for the wrong reason.
        for key in self._written:
            try:
                self.tls._forget(key)
            except Exception:
                pass
        self.session.rollback()
        self.session.close()
        self.tls.invalidate_cache()

    def _user(self, team_name=None):
        from models import User
        user = User(telegram_id=next(_TG_IDS), first_name="Tester",
                    team_name=team_name or f"Team {next(_TEAM_SEQ)}")
        self.session.add(user)
        self.session.commit()
        return user

    def _submit(self, user, raw=None, file_id="FILEID"):
        result = self.tls.submit_logo(self.session, user, raw or _png(),
                                      file_id=file_id)
        if result.get("ok"):
            self.session.commit()
            self._written.append(result["request"].asset_key)
        return result


class ValidationTests(_Base):
    def test_a_good_png_is_accepted(self):
        result = self._submit(self._user())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["request"].status, self.tls.STATUS_PENDING)

    def test_a_tiny_image_is_refused(self):
        result = self._submit(self._user(), raw=_png((64, 64)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid")
        self.assertIn("64", result["message"])

    def test_a_long_thin_image_is_refused(self):
        result = self._submit(self._user(), raw=_png((1200, 240)))
        self.assertFalse(result["ok"])
        self.assertIn("thin", result["message"])

    def test_something_that_is_not_an_image_is_refused(self):
        result = self._submit(self._user(), raw=b"definitely not a png")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid")

    def test_an_oversized_upload_is_refused_before_it_is_decoded(self):
        result = self._submit(self._user(),
                              raw=b"x" * (self.tls.MAX_BYTES + 1))
        self.assertFalse(result["ok"])
        self.assertIn("MB", result["message"])

    def test_a_large_image_is_stored_downscaled(self):
        from PIL import Image
        result = self._submit(self._user(), raw=_png((1400, 1400)))
        self.assertTrue(result["ok"])
        stored = Image.open(io.BytesIO(result["png"]))
        self.assertEqual(max(stored.size), self.tls.STORE_DIM)


class PendingTests(_Base):
    def test_an_upload_changes_nothing_until_it_is_approved(self):
        user = self._user()
        self._submit(user)
        self.assertIsNone(user.team_logo_asset_key)
        self.assertIsNone(user.team_logo_file_id)

    def test_only_one_request_may_be_open_at_a_time(self):
        user = self._user()
        self._submit(user)
        again = self._submit(user)
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"], "pending")

    def test_a_new_upload_is_allowed_once_the_last_was_decided(self):
        user = self._user()
        first = self._submit(user)["request"]
        self.tls.reject_request(self.session, first, reviewer="a", note="no")
        self.session.commit()
        self.assertTrue(self._submit(user)["ok"])


class DecisionTests(_Base):
    def test_approving_puts_the_crest_on_the_team(self):
        user = self._user()
        request = self._submit(user)["request"]
        key = request.asset_key
        result = self.tls.approve_request(self.session, request, reviewer="@boss")
        self.session.commit()
        self.assertTrue(result["ok"])
        self.assertEqual(user.team_logo_asset_key, key)
        self.assertEqual(user.team_logo_file_id, "FILEID")
        self.assertEqual(request.status, self.tls.STATUS_APPROVED)
        self.assertEqual(request.reviewed_by, "@boss")
        self.assertIsNotNone(request.decided_at)

    def test_rejecting_records_the_reason_and_drops_the_bytes(self):
        user = self._user()
        request = self._submit(user)["request"]
        key = request.asset_key
        self.tls.reject_request(self.session, request, reviewer="@boss",
                                note="Not your artwork.")
        self.session.commit()
        self.assertEqual(request.status, self.tls.STATUS_REJECTED)
        self.assertEqual(request.review_note, "Not your artwork.")
        self.assertIsNone(user.team_logo_asset_key)
        self.assertIsNone(self.tls.logo_bytes_for_key(key))

    def test_a_reason_is_truncated_rather_than_overflowing_the_column(self):
        request = self._submit(self._user())["request"]
        self.tls.reject_request(self.session, request, note="x" * 500)
        self.session.commit()
        self.assertEqual(len(request.review_note), 300)

    def test_a_decided_request_cannot_be_decided_again(self):
        """Every admin gets their own copy of the review card, so two of them
        pressing at once has to resolve to one decision."""
        request = self._submit(self._user())["request"]
        self.assertTrue(self.tls.approve_request(self.session, request)["ok"])
        self.session.commit()
        second = self.tls.reject_request(self.session, request, note="too late")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "not_pending")
        self.assertEqual(request.status, self.tls.STATUS_APPROVED)

    def test_withdrawing_closes_the_request(self):
        request = self._submit(self._user())["request"]
        self.assertTrue(self.tls.cancel_request(self.session, request)["ok"])
        self.session.commit()
        self.assertEqual(request.status, self.tls.STATUS_CANCELLED)

    def test_approving_a_replacement_forgets_the_previous_crest(self):
        user = self._user()
        first = self._submit(user)["request"]
        old_key = first.asset_key
        self.tls.approve_request(self.session, first)
        self.session.commit()
        second = self._submit(user)["request"]
        self.tls.approve_request(self.session, second)
        self.session.commit()
        self.assertEqual(user.team_logo_asset_key, second.asset_key)
        self.assertIsNone(self.tls.logo_bytes_for_key(old_key))

    def test_removing_clears_the_crest_without_review(self):
        user = self._user()
        request = self._submit(user)["request"]
        self.tls.approve_request(self.session, request)
        self.session.commit()
        self.assertTrue(self.tls.remove_logo(self.session, user))
        self.session.commit()
        self.assertIsNone(user.team_logo_asset_key)
        self.assertFalse(self.tls.remove_logo(self.session, user))


class ReasonTests(unittest.TestCase):
    def test_every_preset_reason_maps_to_a_message(self):
        from services import team_logo_service as tls
        for key, label, message in tls.REJECT_REASONS:
            self.assertTrue(key and label and message)
            self.assertEqual(tls.REJECT_REASON_MAP[key], (label, message))

    def test_reason_keys_are_short_enough_for_callback_data(self):
        """They ride in callback_data, which Telegram caps at 64 bytes."""
        from services import team_logo_service as tls
        for key, _label, _message in tls.REJECT_REASONS:
            self.assertLessEqual(len(f"tlogo:r:999999:{key}"), 64)

    def test_the_custom_key_is_not_also_a_preset(self):
        from services import team_logo_service as tls
        self.assertNotIn(tls.CUSTOM_REASON_KEY, tls.REJECT_REASON_MAP)


class LookupTests(_Base):
    def test_the_renderers_only_ever_get_an_approved_crest(self):
        user = self._user(team_name="Rome Gladiators")
        self._submit(user)
        self.assertIsNone(
            self.tls.logo_png_for_team_name(self.session, "Rome Gladiators"))

    def test_an_approved_crest_is_found_by_team_name(self):
        user = self._user(team_name="Mumbai Marathas")
        request = self._submit(user)["request"]
        self.tls.approve_request(self.session, request)
        self.session.commit()
        found = self.tls.logo_png_for_team_name(self.session, "Mumbai Marathas")
        self.assertTrue(found and found[:4] == b"\x89PNG")

    def test_the_name_lookup_ignores_case_and_padding(self):
        user = self._user(team_name="Rome Gladiators II")
        request = self._submit(user)["request"]
        self.tls.approve_request(self.session, request)
        self.session.commit()
        self.assertTrue(
            self.tls.logo_png_for_team_name(self.session, "  rome  gladiators ii "))

    def test_an_unknown_team_is_not_an_error(self):
        self.assertIsNone(
            self.tls.logo_png_for_team_name(self.session, "Nobody At All"))
        self.assertIsNone(self.tls.logo_png_for_team_name(self.session, None))

    def test_a_removed_crest_stops_being_served_immediately(self):
        """The lookup is cached, so a removal has to invalidate it rather than
        wait out the TTL — otherwise a crest an owner just deleted keeps
        appearing on cards for minutes."""
        user = self._user(team_name="Briefly Branded")
        request = self._submit(user)["request"]
        self.tls.approve_request(self.session, request)
        self.session.commit()
        self.assertIsNotNone(
            self.tls.logo_png_for_team_name(self.session, "Briefly Branded"))
        self.tls.remove_logo(self.session, user)
        self.session.commit()
        self.assertIsNone(
            self.tls.logo_png_for_team_name(self.session, "Briefly Branded"))


if __name__ == "__main__":
    unittest.main()
