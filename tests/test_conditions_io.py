"""Stadium & modifier export/import (services/conditions_io.py), the DB store
(engine/sim/store.py), per-ground modifiers, and the admin /conditions pages."""

import io
import json
import os
import sys
import tempfile
import unittest

import pytest

from engine.sim import config as sim_config, factors, stadium as stadium_mod
from engine.sim.models import Conditions
from services import conditions_io as cio

# ── pure service tests (no database) ───────────────────────────────────

GROUND = {"name": "Perth Stadium", "aliases": ["Optus"], "city": "Perth", "country": "Australia",
          "boundaryM": {"squareLeg": 72, "fineLeg": 68, "longOn": 82, "longOff": 80},
          "outfieldSpeed": 88, "dewFactor": 15, "typicalPitch": "Bouncy",
          "avgFirstInningsScore": 175, "modifiers": {"bounce": 1.15}}


def _rows():
    return [cio.clean_stadium(r)[0] for r in stadium_mod.file_rows()]


def test_json_bundle_round_trips_without_changes():
    bundle = cio.export_bundle(stadium_rows=stadium_mod.file_rows(), cfg=cio.shipped_config())
    assert bundle["format"] == cio.FORMAT and bundle["stadiums"] and bundle["modifiers"]
    plan = cio.plan_import(cio.parse_upload(json.dumps(bundle)), current_rows=_rows(),
                           current_overrides={})
    assert not plan["errors"] and not plan["warnings"]
    assert plan["stadiums"]["added"] == [] and plan["stadiums"]["changed"] == []
    assert plan["modifiers"]["changes"] == [] and plan["modifiers"]["result"] == {}


def test_csv_round_trips_without_changes():
    text = cio.stadiums_to_csv(stadium_mod.file_rows())
    plan = cio.plan_import(cio.parse_upload(text, "s.csv"), current_rows=_rows())
    assert not plan["errors"] and plan["stadiums"]["changed"] == []
    assert plan["stadiums"]["unchanged"] == len(_rows())


def test_csv_is_detected_without_an_extension():
    text = cio.stadiums_to_csv([GROUND])
    parsed = cio.parse_upload(text)
    assert parsed["stadiums"][0]["name"] == "Perth Stadium"


def test_merge_adds_and_updates_keeping_the_rest():
    edited = dict(_rows()[0], dewFactor=99)
    plan = cio.plan_import({"stadiums": [edited, GROUND]}, current_rows=_rows())
    st = plan["stadiums"]
    assert st["added"] == ["Perth Stadium"]
    assert st["changed"][0]["name"] == edited["name"]
    assert ["dewFactor", _rows()[0]["dewFactor"], 99] in st["changed"][0]["fields"]
    assert len(st["result"]) == len(_rows()) + 1 and st["removed"] == []


def test_merge_updates_only_the_fields_in_the_file():
    eden = next(r for r in _rows() if r["name"] == "Eden Gardens")
    plan = cio.plan_import({"stadiums": [{"name": "eden gardens", "dewFactor": 95,
                                          "modifiers": {"sixes": 1.1}}]}, current_rows=_rows())
    assert not plan["warnings"]
    fields = {f[0] for f in plan["stadiums"]["changed"][0]["fields"]}
    assert fields == {"dewFactor", "modifiers.sixes"}
    after = next(r for r in plan["stadiums"]["result"] if r["name"] == "Eden Gardens")
    assert after["aliases"] == eden["aliases"] and after["climate"] == eden["climate"]
    assert after["modifiers"] == dict(eden["modifiers"], sixes=1.1)


def test_replace_removes_what_is_not_in_the_file():
    plan = cio.plan_import({"stadiums": [GROUND]}, mode="replace", current_rows=_rows())
    assert len(plan["stadiums"]["removed"]) == len(_rows())
    assert [s["name"] for s in plan["stadiums"]["result"]] == ["Perth Stadium"]


def test_validation_clamps_fills_and_drops_with_warnings():
    raw = dict(GROUND, outfieldSpeed=250, typicalPitch="concrete",
               boundaryM={"squareLeg": 30, "fineLeg": 70},
               modifiers={"sixes": 9, "spin": 1.0, "nonsense": 2})
    clean, warns = cio.clean_stadium(raw)
    assert clean["outfieldSpeed"] == 100
    assert clean["boundaryM"]["squareLeg"] == 40
    assert clean["boundaryM"]["longOn"] == 55  # filled with the mean of what was given
    assert clean["typicalPitch"] == "Even"
    assert clean["modifiers"] == {"sixes": 2}   # clamped; 1.0 dropped as "no effect"
    text = " ".join(warns)
    for bit in ("outfieldSpeed", "longOn", "concrete", "nonsense", "sixes"):
        assert bit in text


def test_unusable_rows_are_errors_not_crashes():
    plan = cio.plan_import({"stadiums": [{"city": "no name"}, dict(GROUND, dewFactor="lots")]},
                           current_rows=_rows())
    assert len(plan["errors"]) == 2
    with pytest.raises(ValueError):
        cio.apply_plan(plan, session=None)


def test_replace_with_nothing_valid_is_refused():
    plan = cio.plan_import({"stadiums": [{"city": "x"}]}, mode="replace", current_rows=_rows())
    assert any("no stadiums" in e for e in plan["errors"])


def test_modifier_import_is_minimal_and_validated():
    incoming = {"drama": {"dramaSlider": 80}, "pitches": {"Flat": {"battingEase": 7}},
                "bogus": 1, "meta": {"live_strength": "0.4"}}
    plan = cio.plan_import({"modifiers": incoming}, current_overrides={})
    result = plan["modifiers"]["result"]
    assert result["drama"] == {"dramaSlider": 80}
    assert result["pitches"]["Flat"] == {"battingEase": 3.0}      # clamped
    assert result["meta"] == {"live_strength": 0.4}
    assert "bogus" not in result
    assert any("bogus" in w for w in plan["warnings"])
    paths = [c[0] for c in plan["modifiers"]["changes"]]
    assert "drama.dramaSlider" in paths


def test_full_config_import_stores_only_the_difference():
    full = cio.shipped_config()
    full = sim_config.deep_merge(full, {"weather": {"rain": {"basePerOverProb": 0.0}}})
    plan = cio.plan_import({"modifiers": full}, current_overrides={})
    assert plan["modifiers"]["result"] == {"weather": {"rain": {"basePerOverProb": 0.0}}}


def test_modifier_merge_keeps_earlier_overrides():
    plan = cio.plan_import({"modifiers": {"drama": {"dramaSlider": 10}}},
                           current_overrides={"meta": {"live_strength": 0.3}})
    assert plan["modifiers"]["result"]["meta"] == {"live_strength": 0.3}
    replaced = cio.plan_import({"modifiers": {"drama": {"dramaSlider": 10}}}, mode="replace",
                               current_overrides={"meta": {"live_strength": 0.3}})
    assert "meta" not in replaced["modifiers"]["result"]


@pytest.mark.parametrize("text,kind", [
    (json.dumps([GROUND]), "stadiums"),
    (json.dumps(GROUND), "stadiums"),
    (json.dumps({"drama": {"dramaSlider": 1}}), "modifiers"),
])
def test_parse_upload_recognises_shapes(text, kind):
    parsed = cio.parse_upload(text)
    assert parsed[kind] is not None


@pytest.mark.parametrize("text", ["", "{not json", json.dumps({"hello": 1}), json.dumps(3),
                                  json.dumps({"format": cio.FORMAT, "version": 99, "stadiums": []})])
def test_parse_upload_rejects_garbage(text):
    with pytest.raises(ValueError):
        cio.parse_upload(text)


# ── per-ground modifiers in the engine ──────────────────────────────────

def test_ground_modifiers_reach_the_factors():
    cfg = sim_config.build(local_path="")
    plain = stadium_mod.from_dict(dict(GROUND, modifiers={}))
    spiced = stadium_mod.from_dict(dict(GROUND, modifiers={"spin": 1.2, "sixes": 0.8}))
    a = factors.compose(Conditions(stadium=plain), None, cfg)
    b = factors.compose(Conditions(stadium=spiced), None, cfg)
    assert b["spin"] == pytest.approx(a["spin"] * 1.2)
    assert b["six"] == pytest.approx(a["six"] * 0.8)
    assert any(e.key == "ground_spin" for e in b.effects)


def test_ground_modifiers_are_clamped_at_play_time():
    cfg = sim_config.build(local_path="")
    wild = stadium_mod.from_dict(dict(GROUND, modifiers={"spin": 50}))
    plain = stadium_mod.from_dict(dict(GROUND, modifiers={}))
    assert (factors.compose(Conditions(stadium=wild), None, cfg)["spin"]
            == pytest.approx(factors.compose(Conditions(stadium=plain), None, cfg)["spin"] * 2.0))


def test_seeded_grounds_carry_their_character():
    assert stadium_mod.find("Newlands").modifiers
    assert dict(stadium_mod.find("Eden Gardens").modifiers)["spin"] > 1


# ── database store + admin pages ────────────────────────────────────────

_MODULES = ("database", "models", "config", "admin", "engine.sim.store")


class ConditionsAdminTests(unittest.TestCase):
    """End to end against a throwaway SQLite database."""

    @classmethod
    def setUpClass(cls):
        cls._saved = {n: sys.modules.get(n) for n in _MODULES}
        cls._prev_url = os.environ.get("DATABASE_URL")
        for n in _MODULES:
            sys.modules.pop(n, None)
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["DATABASE_URL"] = f"sqlite:///{cls._tmp.name}"
        os.environ.setdefault("BOT_TOKEN", "test-token")
        os.environ.setdefault("ADMIN_PASSWORD", "test")
        os.environ.setdefault("ADMIN_USERNAME", "admin")
        from database import Base, engine
        import models  # noqa: F401  (registers the tables)
        Base.metadata.create_all(bind=engine)
        cls.engine = engine
        try:
            import admin
        except Exception as exc:  # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        admin.app.config["TESTING"] = True
        admin.app.config["WTF_CSRF_ENABLED"] = False
        cls.admin = admin

    @classmethod
    def tearDownClass(cls):
        try:
            cls.engine.dispose()
        except Exception:
            pass
        for n in _MODULES:
            sys.modules.pop(n, None)
        for n, m in cls._saved.items():
            if m is not None:
                sys.modules[n] = m
        if cls._prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls._prev_url
        try:
            os.unlink(cls._tmp.name)
        except OSError:
            pass
        sim_config.reload()
        stadium_mod.reload()

    def setUp(self):
        from database import get_session
        from models import SimEngineSetting
        s = get_session()
        s.query(SimEngineSetting).delete()
        s.commit()
        s.close()
        sim_config.reload()
        stadium_mod.reload()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as fs:
            fs["admin"] = True

    def _import(self, payload, mode="merge", only="all", filename="x.json"):
        r = self.client.post("/conditions/import", data={
            "step": "preview", "mode": mode, "only": only,
            "file": (io.BytesIO(payload.encode()), filename)},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.data[:300])
        return self.client.post("/conditions/import", data={
            "step": "apply", "mode": mode, "only": only, "payload": payload,
            "filename": filename, "note": "test"}, follow_redirects=False)

    def test_list_page_and_exports(self):
        r = self.client.get("/conditions")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Melbourne Cricket Ground", r.data)
        j = self.client.get("/conditions/export?what=all")
        self.assertEqual(j.status_code, 200)
        self.assertIn("attachment", j.headers["Content-Disposition"])
        body = json.loads(j.data)
        self.assertEqual(body["format"], cio.FORMAT)
        c = self.client.get("/conditions/export?fmt=csv")
        self.assertTrue(c.data.decode().startswith("name,aliases"))

    def test_import_preview_then_apply_goes_live_and_is_versioned(self):
        payload = json.dumps({"stadiums": [GROUND], "modifiers": {"drama": {"dramaSlider": 77}}})
        preview = self.client.post("/conditions/import", data={
            "step": "preview", "mode": "merge", "only": "all", "text_content": payload})
        self.assertIn(b"Perth Stadium", preview.data)
        self.assertIn(b"drama.dramaSlider", preview.data)
        self.assertIsNone(stadium_mod.find("Perth Stadium"))        # not saved yet

        r = self._import(payload)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(stadium_mod.find("Optus").name, "Perth Stadium")
        self.assertEqual(sim_config.get_config()["drama"]["dramaSlider"], 77)
        self.assertIsNotNone(stadium_mod.find("MCG"))              # merge kept the rest

        from database import get_session
        from engine.sim import store
        s = get_session()
        hist = store.history("stadiums", s)
        self.assertEqual(len(hist), 1)
        s.close()

    def test_restore_and_reset(self):
        self._import(json.dumps([GROUND]), mode="replace")
        self.assertEqual(len(stadium_mod.all_stadiums()), 1)
        self._import(json.dumps([dict(GROUND, name="Second Ground")]), mode="replace")
        self.assertIsNone(stadium_mod.find("Perth Stadium"))

        from database import get_session
        from engine.sim import store
        s = get_session()
        first = store.history("stadiums", s)[-1]["id"]
        s.close()
        self.client.post(f"/conditions/restore/{first}")
        self.assertIsNotNone(stadium_mod.find("Perth Stadium"))

        self.client.post("/conditions/reset/stadiums")
        self.assertGreater(len(stadium_mod.all_stadiums()), 10)     # back to the file

    def test_edit_rename_and_delete_a_ground(self):
        r = self.client.post("/conditions/stadium", data={
            "original": "Eden Gardens", "name": "Eden Gardens Kolkata", "typicalPitch": "Dry",
            "boundary_squareLeg": 66, "boundary_fineLeg": 64, "boundary_longOn": 72,
            "boundary_longOff": 70, "outfieldSpeed": 90, "dewFactor": 80,
            "altitudeMeters": 9, "avgFirstInningsScore": 165, "mod_spin": "1.25"})
        self.assertEqual(r.status_code, 302)
        g = stadium_mod.find("Eden Gardens Kolkata")
        self.assertEqual(g.outfield_speed, 90)
        self.assertEqual(dict(g.modifiers)["spin"], 1.25)
        self.assertEqual(sum(1 for s in stadium_mod.all_stadiums() if "Eden" in s.name), 1)

        self.client.post("/conditions/stadium/delete", data={"name": "Eden Gardens Kolkata"})
        self.assertIsNone(stadium_mod.find("Eden Gardens Kolkata"))

    def test_bad_import_saves_nothing(self):
        r = self.client.post("/conditions/import", data={
            "step": "apply", "mode": "replace", "only": "all",
            "payload": json.dumps([{"city": "nameless"}]), "filename": "x.json"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"fix these first", r.data)
        self.assertGreater(len(stadium_mod.all_stadiums()), 10)
