"""Uploads survive a redeploy, so nothing has to be uploaded twice.

The app runs on a host that rebuilds the container on every deploy, wiping
``data/``. That used to lose every card template, font, country flag and wizard
image the admin had uploaded. They are kept in ``stored_assets`` now, and come
back two ways: refilled at boot, and re-materialised on demand when something
reads a file that is missing.

These tests wipe the directories for real and assert the files come back.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.asset_store",
                 "services.card_template_service", "services.country_flag_service",
                 "services.player_portrait_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401

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
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


def _png_bytes(size=(64, 48), colour=(255, 140, 0, 255)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGBA", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def _card_bytes():
    """A blank big enough to pass the card-template validator."""
    return _png_bytes(size=(1536, 1024), colour=(10, 30, 60, 255))


class KeyScopeTests(unittest.TestCase):
    """The store must only ever touch the upload directories."""

    def test_upload_directories_are_accepted(self):
        from services.asset_store import _relative_key, PROJECT_ROOT
        path = os.path.join(PROJECT_ROOT, "data", "card_templates", "template.png")
        self.assertEqual(_relative_key(path), "data/card_templates/template.png")

    def test_paths_outside_the_project_are_refused(self):
        from services.asset_store import _relative_key
        self.assertIsNone(_relative_key("/etc/passwd"))
        self.assertIsNone(_relative_key("../../secrets.env"))

    def test_directories_that_are_not_uploads_are_refused(self):
        from services.asset_store import _relative_key, PROJECT_ROOT
        for relative in ("bot.py", "services/asset_store.py", "data/players.json"):
            path = os.path.join(PROJECT_ROOT, relative)
            self.assertIsNone(_relative_key(path), f"{relative} must not be stored")

    def test_runtime_state_and_docs_are_not_adopted(self):
        from services.asset_store import _relative_key, PROJECT_ROOT
        for name in ("state.json", "README.md", "notes.txt"):
            path = os.path.join(PROJECT_ROOT, "data", "card_templates", name)
            self.assertIsNone(_relative_key(path), f"{name} must not be stored")

    def test_oversized_files_are_refused_rather_than_bloating_the_database(self):
        from services import asset_store
        path = os.path.join(asset_store.PROJECT_ROOT, "data", "card_templates",
                            "huge.png")
        oversized = b"x" * (asset_store.MAX_ASSET_BYTES + 1)
        self.assertFalse(asset_store.put(path, oversized))


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        from services import asset_store
        self.store = asset_store
        self.key = "data/card_templates/roundtrip_test.png"
        self.path = asset_store.absolute_path(self.key)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.data = _png_bytes()
        # put() mirrors a file the caller has already written, so match real
        # usage: the upload lands on disk first, then gets stored.
        with open(self.path, "wb") as handle:
            handle.write(self.data)

    def tearDown(self):
        self.store.drop(self.path)
        if os.path.isfile(self.path):
            os.remove(self.path)

    def test_a_stored_file_comes_back_after_the_disk_is_wiped(self):
        self.assertTrue(self.store.put(self.path, self.data))
        os.remove(self.path)
        self.assertFalse(os.path.isfile(self.path))
        self.assertTrue(self.store.ensure(self.path))
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), self.data)

    def test_re_uploading_replaces_rather_than_duplicates(self):
        from models import StoredAsset
        from database import get_session
        self.store.put(self.path, self.data)
        self.store.put(self.path, _png_bytes(colour=(0, 200, 100, 255)))
        session = get_session()
        try:
            rows = (session.query(StoredAsset)
                    .filter(StoredAsset.key == self.key).count())
        finally:
            session.close()
        self.assertEqual(rows, 1)

    def test_a_deleted_file_is_not_resurrected(self):
        self.store.put(self.path, self.data)
        os.remove(self.path)
        self.store.drop(self.path)
        self.assertFalse(self.store.ensure(self.path))
        self.assertFalse(os.path.isfile(self.path))

    def test_ensure_leaves_a_file_that_is_already_there_alone(self):
        with open(self.path, "wb") as handle:
            handle.write(b"on disk already")
        self.assertTrue(self.store.ensure(self.path))
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"on disk already")

    def test_usage_reports_what_is_held(self):
        self.store.put(self.path, self.data)
        usage = self.store.usage()
        self.assertGreaterEqual(usage["count"], 1)
        self.assertGreaterEqual(usage["bytes"], len(self.data))
        self.assertIn("data/card_templates", usage["by_root"])


class RedeploySurvivalTests(unittest.TestCase):
    """The real scenario: upload, wipe data/, and expect it all back."""

    def setUp(self):
        from services import asset_store, card_template_service, country_flag_service
        self.store = asset_store
        self.templates = card_template_service
        self.flags = country_flag_service
        self.roots = [
            os.path.join(asset_store.PROJECT_ROOT, "data", "card_templates"),
            os.path.join(asset_store.PROJECT_ROOT, "data", "country_flags"),
        ]
        self.backups = {}
        for root in self.roots:
            if os.path.isdir(root):
                backup = tempfile.mkdtemp()
                shutil.copytree(root, os.path.join(backup, "copy"))
                self.backups[root] = backup

    def tearDown(self):
        # Put the working tree back exactly as it was, committed files included.
        for root in self.roots:
            backup = self.backups.get(root)
            if os.path.isdir(root):
                shutil.rmtree(root)
            if backup:
                shutil.copytree(os.path.join(backup, "copy"), root)
                shutil.rmtree(backup, ignore_errors=True)
            # A root that did not exist before this test must not be left behind.
        self.store.drop_prefix("data/card_templates/redeploy_")
        self.store.drop_prefix("data/country_flags/")
        for name in ("template_star", "font"):
            for ext in ("png", "ttf"):
                path = os.path.join(self.store.PROJECT_ROOT, "data",
                                    "card_templates", f"{name}.{ext}")
                if os.path.isfile(path) and name not in self.backups:
                    self.store.drop(path)

    def _wipe_data_dirs(self):
        """Exactly what the host does on deploy: the directories cease to exist."""
        for root in self.roots:
            if os.path.isdir(root):
                shutil.rmtree(root)

    def test_a_card_template_survives_a_redeploy(self):
        ok, message, path = self.templates.save_template_image(
            _card_bytes(), "star.png", variant="star")
        self.assertTrue(ok, message)
        self._wipe_data_dirs()
        self.assertFalse(os.path.isfile(path))

        restored = self.store.restore_missing()
        self.assertGreaterEqual(restored, 1)
        self.assertTrue(os.path.isfile(path),
                        "the uploaded template must be back on disk")

    def test_a_country_flag_survives_a_redeploy(self):
        ok, message = self.flags.save_country_flag("Testland", _png_bytes(),
                                                   "testland.png")
        self.assertTrue(ok, message)
        self._wipe_data_dirs()
        self.store.restore_missing()
        self.assertIn("Testland", self.flags.list_country_flags())
        self.assertIsNotNone(self.flags.get_country_flag("Testland"))

    def test_a_template_lookup_heals_itself_without_a_restart(self):
        """A container that booted before the upload must still find the file."""
        ok, _message, _path = self.templates.save_template_image(
            _card_bytes(), "star.png", variant="star")
        self.assertTrue(ok)
        self._wipe_data_dirs()
        # No restore_missing() here — the lookup itself has to recover.
        self.templates._RESTORE_ATTEMPTED = False
        found = self.templates.template_image_path(None, "star",
                                                   fallback_to_base=False)
        self.assertTrue(found and os.path.isfile(found),
                        "template_image_path should re-materialise on a miss")

    def test_the_database_is_consulted_at_most_once_per_process(self):
        """Card rendering is latency-sensitive; a miss must not query every time."""
        self.templates._RESTORE_ATTEMPTED = False
        calls = []
        original = self.store.ensure_dir

        def counting_ensure_dir(root):
            calls.append(root)
            return original(root)

        self.store.ensure_dir = counting_ensure_dir
        try:
            for _ in range(5):
                self.templates._restore_root()
        finally:
            self.store.ensure_dir = original
        self.assertEqual(len(calls), 1, "the restore guard should hold after the first try")

    def test_an_upload_reopens_the_restore_guard(self):
        """A file added after start-up must still be findable."""
        self.templates._RESTORE_ATTEMPTED = True
        self.templates._remember(
            os.path.join(self.store.PROJECT_ROOT, "data", "card_templates",
                         "redeploy_probe.png"),
            _png_bytes())
        self.assertFalse(self.templates._RESTORE_ATTEMPTED)

    def test_existing_files_are_adopted_so_nothing_needs_re_uploading(self):
        """Files already on disk before the store existed are picked up."""
        root = os.path.join(self.store.PROJECT_ROOT, "data", "card_templates")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "redeploy_adopted.png")
        with open(path, "wb") as handle:
            handle.write(_png_bytes())
        self.store.drop(path)

        self.store.adopt_existing()
        os.remove(path)
        self.assertTrue(self.store.ensure(path),
                        "an adopted file should be restorable")


if __name__ == "__main__":
    unittest.main()
