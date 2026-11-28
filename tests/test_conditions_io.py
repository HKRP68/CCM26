"""Stadium & modifier export/import (services/conditions_io.py), the DB store
(engine/sim/store.py), per-ground modifiers, and the admin /conditions pages."""

import io
import json
import os
import sys
import tempfile
import unittest

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


def test_legacy_stadium_pitch_in_csv_is_ignored():
    current = _rows()
    upload = cio.parse_upload("name,typicalPitch,dewFactor\nWankhede Stadium,Flat,50\n", "s.csv")
    plan = cio.plan_import(upload, current_rows=current)
    assert not plan["errors"]
    assert plan["stadiums"]["changed"][0]["fields"] == [["dewFactor", 85.0, 50.0]]
    rows = plan["stadiums"]["result"]
    assert all("typicalPitch" not in row for row in rows)
    assert "typicalPitch" not in cio.stadiums_to_csv(rows)


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
    assert "typicalPitch" not in clean
    assert clean["modifiers"] == {"sixes": 2}   # clamped; 1.0 dropped as "no effect"
    text = " ".join(warns)
    for bit in ("outfieldSpeed", "longOn", "nonsense", "sixes"):
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



class ConditionsAdminTests(unittest.TestCase):
    """End to end against a throwaway SQLite database."""

    @classmethod
    def setUpClass(cls):
        """Point the already-imported app at a throwaway database.

        This used to pop ``database``/``models``/``admin`` out of ``sys.modules`` and
        import them again. That builds a *second* declarative registry: 150 more mapped
        classes for the same tables. Importing ``admin`` then forces SQLAlchemy to
        configure mappers while both registries are live, and models.py's 102
        ``relationship()`` strings resolve across the two. Mapper configuration is
        cached, so restoring the modules afterwards does not undo it, and the next
        suite to query one of those tables gets ``FROM players, players`` —
        "ambiguous column name: players.id", a very long way from the cause.

        ``get_session()`` is ``return SessionLocal()`` reading its module global at call
        time, and every caller imports the *function*. So swapping ``SessionLocal``
        redirects the whole app with one registry and no re-import.
        """
        import database
        try:
            import admin
        except Exception as exc:  # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        cls._database = database
        cls._saved_factory = database.SessionLocal
        cls._saved_engine = database.engine
        cls.engine = None
        # Past the first mutation everything is guarded, so a failure here cannot leave
        # the app pointed at a deleted file for the rest of the session.
        try:
            cls.engine = create_engine(f"sqlite:///{cls._tmp.name}",
                                       connect_args={"check_same_thread": False})
            database.Base.metadata.create_all(bind=cls.engine)
            database.SessionLocal = sessionmaker(bind=cls.engine, autoflush=False,
                                                 autocommit=False)
            database.engine = cls.engine
            admin.app.config["TESTING"] = True
            admin.app.config["WTF_CSRF_ENABLED"] = False
            cls.admin = admin
        except BaseException:
            cls._restore()
            raise

    @classmethod
    def _restore(cls):
        """Undo everything setUpClass changed, whatever point it failed at."""
        cls._database.SessionLocal = cls._saved_factory
        cls._database.engine = cls._saved_engine
        if cls.engine is not None:
            try:
                cls.engine.dispose()
            except Exception:
                pass
            cls.engine = None
        tmp = getattr(cls, "_tmp", None)
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            cls._tmp = None
        sim_config.reload()
        stadium_mod.reload()

    @classmethod
    def tearDownClass(cls):
        cls._restore()

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

    def _add_json(self, payload):
        return self.client.post("/conditions/stadium/json", data={"stadiums_json": payload},
                                follow_redirects=True)

    def _ground(self, name):
        from engine.sim import stadium as sim_stadium
        return next((r for r in sim_stadium.live_rows() if r["name"] == name), None)

    def test_pasted_json_adds_grounds_and_merges_onto_an_existing_one(self):
        r = self._add_json(json.dumps({
            "name": "Perth Stadium", "city": "Perth", "country": "Australia",
            "boundaryM": {"squareLeg": 72, "fineLeg": 68, "longOn": 82, "longOff": 80},
            "outfieldSpeed": 88, "dewFactor": 25, "modifiers": {"bounce": 1.15}}))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Added Perth Stadium", r.data.decode())   # not lowercased
        self.assertEqual(self._ground("Perth Stadium")["dewFactor"], 25)

        # A list adds several at once, and a new country becomes its own group.
        import re
        self._add_json(json.dumps([{"name": "Basin Reserve", "city": "Wellington",
                                    "country": "New Zealand"},
                                   {"name": "Gaddafi Stadium", "country": "Pakistan"}]))
        self.assertIsNotNone(self._ground("Basin Reserve"))
        body = self.client.get("/conditions").data.decode()
        self.assertTrue(any(h.strip().endswith("New Zealand")
                            for h in re.findall(r"<summary>([^<]*)", body)), body[:200])

        # A name plus one field edits that field and leaves the rest of the ground alone,
        # even though parse_upload alone would not recognise it as a stadium.
        before = self._ground("Perth Stadium")
        r = self._add_json('{"name": "Perth Stadium", "dewFactor": 70}')
        self.assertIn("Updated Perth Stadium", r.data.decode())
        after = self._ground("Perth Stadium")
        self.assertEqual(after["dewFactor"], 70)
        self.assertEqual(after["boundaryM"], before["boundaryM"])
        self.assertEqual(after["modifiers"], before["modifiers"])
        self.assertEqual(after["outfieldSpeed"], before["outfieldSpeed"])

    def test_pasted_json_reports_what_it_could_not_use(self):
        self.assertIn("not valid JSON", self._add_json("{nope").data.decode())
        self.assertIn("holds no stadiums",
                      self._add_json('{"modifiers": {"stadium": {"dew": {"threshold": 60}}}}').data.decode())
        self.assertIn("Paste a stadium", self._add_json("   ").data.decode())
        # A value out of range is clamped and said so; the untouched grounds stay.
        before = len(self._all_rows())
        r = self._add_json('{"name": "Clamp Test", "outfieldSpeed": 9999}')
        self.assertIn("clamped to 100", r.data.decode())
        self.assertEqual(self._ground("Clamp Test")["outfieldSpeed"], 100)
        self.assertEqual(len(self._all_rows()), before + 1)

    def test_json_file_adds_and_updates_stadiums_without_pasted_text(self):
        existing = self._ground("Eden Gardens")
        payload = json.dumps({"stadiums": [
            {"name": "Parque de Críquet", "country": "Spain"},
            {"name": "Eden Gardens", "dewFactor": 70},
        ]}, ensure_ascii=False).encode("utf-8-sig")
        r = self.client.post("/conditions/stadium/json", data={
            "stadiums_file": (io.BytesIO(payload), "grounds.json"),
        }, follow_redirects=True)

        self.assertEqual(r.status_code, 200)
        self.assertIn("Added Parque de Críquet", r.data.decode())
        self.assertIn("uploaded JSON: +1 ~1", r.data.decode())
        self.assertEqual(self._ground("Parque de Críquet")["country"], "Spain")
        self.assertEqual(self._ground("Eden Gardens"), dict(existing, dewFactor=70))

    def test_bulk_json_file_saves_all_stadiums_and_fits_the_audit_log(self):
        from database import get_session
        from models import AdminLog

        names = {f"International Cricket Stadium {i:02d}" for i in range(94)}
        payload = json.dumps([{"name": name} for name in sorted(names)]).encode()
        r = self.client.post("/conditions/stadium/json", data={
            "stadiums_file": (io.BytesIO(payload), "grounds.json"),
        }, follow_redirects=True)

        with get_session() as db:
            log = (db.query(AdminLog).filter_by(action="conditions_stadium_json")
                   .order_by(AdminLog.id.desc()).first())
            self.assertLessEqual(len(log.target_name), AdminLog.target_name.type.length)
            self.assertEqual(log.detail, "added=94 changed=0")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Added 94 stadiums", r.data)
        self.assertIn(b"uploaded JSON: +94 ~0", r.data)
        self.assertLessEqual(names, {stadium["name"] for stadium in self._all_rows()})

    def test_json_file_takes_priority_over_pasted_json(self):
        r = self.client.post("/conditions/stadium/json", data={
            "stadiums_file": (io.BytesIO(b'{"name": "File Ground", "modifiers": {"bounce": 1.1}}'),
                              "ground.json"),
            "stadiums_json": '{"name": "Pasted Ground"}',
        }, follow_redirects=True)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ground("File Ground")["modifiers"], {"bounce": 1.1})
        self.assertIsNone(self._ground("Pasted Ground"))

    def test_invalid_json_files_do_not_save_stadiums(self):
        before = self._all_rows()
        for payload, message in (
            (b"", "the JSON file is empty"),
            (b"{invalid", "not valid JSON"),
            (b'{"name": "\xff"}', "UTF-8 encoding"),
            (b" " * (self.admin._COND_MAX_UPLOAD + 1), "over 2 MB"),
        ):
            with self.subTest(message=message):
                r = self.client.post("/conditions/stadium/json", data={
                    "stadiums_file": (io.BytesIO(payload), "grounds.json"),
                }, follow_redirects=True)
                self.assertEqual(r.status_code, 200)
                self.assertIn(message, r.data.decode())
                self.assertEqual(self._all_rows(), before)

    def _all_rows(self):
        from engine.sim import stadium as sim_stadium
        return sim_stadium.live_rows()

    def test_list_page_groups_the_grounds_by_country(self):
        import re
        from engine.sim import stadium as sim_stadium

        rows = sim_stadium.live_rows()
        r = self.client.get("/conditions")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        # One collapsible per country, each headed by the country and its count.
        headings = re.findall(r"<summary>([^<]*)", body)
        self.assertTrue(any(head.strip().endswith("India") for head in headings), headings)
        self.assertTrue(any(head.strip().endswith("Australia") for head in headings), headings)
        self.assertEqual(body.count('class="cond-fold cond-country"'),
                         len({(row.get("country") or "").strip().lower() for row in rows}))
        self.assertIn("6 stadiums", body)          # the six Indian grounds that ship
        # Every ground still reaches the page.
        import html as html_module
        plain = html_module.unescape(body)
        for row in rows:
            self.assertIn(row["name"], plain)
        # A search opens the groups it matched rather than hiding them behind a fold.
        found = self.client.get("/conditions?q=india").data.decode()
        self.assertIn('<details class="cond-fold cond-country" open>', found)
        self.assertNotIn("Melbourne Cricket Ground", found)
        # The edit form offers the spellings already in use.
        form = self.client.get("/conditions/stadium?name=Wankhede%20Stadium")
        self.assertIn(b'list="stadium-countries"', form.data)
        self.assertIn(b'<option value="India">', form.data)

    def test_list_page_and_exports(self):
        r = self.client.get("/conditions")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Melbourne Cricket Ground", r.data)
        form = self.client.get("/conditions/stadium?name=Wankhede%20Stadium")
        self.assertEqual(form.status_code, 200)
        self.assertNotIn(b'name="typicalPitch"', form.data)
        j = self.client.get("/conditions/export?what=all")
        self.assertEqual(j.status_code, 200)
        self.assertIn("attachment", j.headers["Content-Disposition"])
        body = json.loads(j.data)
        self.assertEqual(body["format"], cio.FORMAT)
        self.assertTrue(all("typicalPitch" not in ground for ground in body["stadiums"]))
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
