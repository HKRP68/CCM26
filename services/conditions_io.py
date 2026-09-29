"""Export and import the Conditions Engine's stadiums and modifiers.

Used by the admin site (``/conditions``) and by ``python -m tools.conditions_io``.

**Export** — one JSON *bundle*::

    {"format": "ccm26-conditions", "version": 1, "exported_at": "...",
     "stadiums": [ ...every ground, with its own modifiers... ],
     "modifiers": { ...the full effective config/sim_engine.json... }}

or the stadiums alone as CSV (one row per ground, spreadsheet-friendly).

**Import** accepts any of: a bundle, a bare stadium list, a single stadium, a
(partial or full) ``sim_engine.json``-shaped dict, or the CSV. Nothing is
applied blind: :func:`plan_import` validates every value, clamps what is out of
range, drops unknown keys (each with a warning), and returns a diff — added,
changed field by field, removed — for the admin to confirm. :func:`apply_plan`
then saves through ``engine.sim.store``, which keeps every previous version for
one-click restore.

Modes: ``merge`` adds new grounds and updates known ones *field by field* (a
file that only sets Eden Gardens' dewFactor changes only that) and keeps
everything else; ``replace`` makes the file the whole list (stadiums) or the
whole set of overrides (modifiers).

Modifier overrides are stored *minimal*: only the values that differ from the
shipped ``config/sim_engine.json``, so a later change to a shipped default
still reaches everything the admin never touched.
"""

import copy
import csv
import io
import json
from datetime import datetime

from engine import pitch_registry
from engine.sim import config as sim_config, stadium as stadium_mod, store
from engine.sim.models import BOUNDARY_REGIONS

FORMAT = "ccm26-conditions"
VERSION = 1

# field: (lo, hi, default)
_NUM = {
    "altitudeMeters": (0, 5000, 0),
    "outfieldSpeed": (0, 100, 65),
    "dewFactor": (0, 100, 0),
    "avgFirstInningsScore": (60, 350, 170),
}
_BOUNDARY = (40, 100)
_CLIMATE = {
    "cloudCover": (0, 100), "humidity": (0, 100), "rainChance": (0, 100),
    "temperatureC": (-5, 50), "windKPH": (0, 80),
}
_TEXT = ("city", "country")


# ── validation ──────────────────────────────────────────────────────────

def _num(v):
    if isinstance(v, bool):
        raise ValueError("not a number")
    if isinstance(v, str):
        v = v.strip()
    return float(v)


def _clean_number(value, lo, hi, label, warns):
    try:
        v = _num(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: {value!r} is not a number")
    if v != v:  # NaN
        raise ValueError(f"{label}: not a number")
    c = max(lo, min(hi, v))
    if c != v:
        warns.append(f"{label} {v:g} clamped to {c:g}")
    return int(c) if float(c).is_integer() else round(c, 3)


def _bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def modifier_bounds(cfg=None):
    b = ((cfg or sim_config.get_config()).get("stadium") or {}).get("modifierBounds") or {}
    return float(b.get("min", 0.5)), float(b.get("max", 2.0))


def clean_stadium(raw, cfg=None):
    """Validate one stadium dict. Returns ``(clean, warnings)``; raises
    ``ValueError`` when it cannot be used at all (no name, garbage numbers)."""
    if not isinstance(raw, dict):
        raise ValueError("a stadium must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("stadium without a name")
    if len(name) > 80:
        raise ValueError(f"{name[:40]}…: name longer than 80 characters")
    label = name
    warns = []
    out = {"name": name}

    aliases = raw.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a for a in (x.strip() for x in aliases.split("|")) if a]
    out["aliases"] = sorted({str(a).strip()[:60] for a in aliases if str(a).strip()} - {name})
    for f in _TEXT:
        out[f] = str(raw.get(f) or "").strip()[:60]

    for f, (lo, hi, dflt) in _NUM.items():
        if raw.get(f) in (None, ""):
            out[f] = dflt
            if f != "altitudeMeters":
                warns.append(f"{label}: {f} missing, using {dflt}")
        else:
            out[f] = _clean_number(raw[f], lo, hi, f"{label}: {f}", warns)
    out["avgFirstInningsScore"] = int(out["avgFirstInningsScore"])

    b = raw.get("boundaryM") or {}
    if not isinstance(b, dict):
        raise ValueError(f"{label}: boundaryM must be an object of the four regions")
    given = {r: _clean_number(b[r], *_BOUNDARY, f"{label}: boundary {r}", warns)
             for r in BOUNDARY_REGIONS if b.get(r) not in (None, "")}
    fill = round(sum(given.values()) / len(given), 1) if given else 70
    for r in BOUNDARY_REGIONS:
        if r not in given:
            warns.append(f"{label}: boundary {r} missing, using {fill}")
    out["boundaryM"] = {r: given.get(r, fill) for r in BOUNDARY_REGIONS}
    for k in b:
        if k not in BOUNDARY_REGIONS:
            warns.append(f"{label}: unknown boundary region {k!r} ignored")

    pitch = str(raw.get("typicalPitch") or "Even").strip()
    match = next((p for p in pitch_registry.PITCHES if p.lower() == pitch.lower()), None)
    if match is None:
        warns.append(f"{label}: unknown pitch {pitch!r}, using Even")
        match = "Even"
    out["typicalPitch"] = match
    out["slope"] = _bool(raw.get("slope", False))

    climate = raw.get("climate") or {}
    if not isinstance(climate, dict):
        raise ValueError(f"{label}: climate must be an object")
    out["climate"] = {}
    for k, (lo, hi) in _CLIMATE.items():
        if climate.get(k) not in (None, ""):
            out["climate"][k] = _clean_number(climate[k], lo, hi, f"{label}: climate {k}", warns)
    for k in climate:
        if k not in _CLIMATE:
            warns.append(f"{label}: unknown climate key {k!r} ignored")

    lo, hi = modifier_bounds(cfg)
    mods = raw.get("modifiers") or {}
    if not isinstance(mods, dict):
        raise ValueError(f"{label}: modifiers must be an object")
    out["modifiers"] = {}
    for k, v in mods.items():
        if k not in stadium_mod.MODIFIER_CHANNELS:
            warns.append(f"{label}: unknown modifier {k!r} ignored "
                         f"(known: {', '.join(stadium_mod.MODIFIER_CHANNELS)})")
            continue
        if v in (None, ""):
            continue
        val = _clean_number(v, lo, hi, f"{label}: modifier {k}", warns)
        if abs(val - 1.0) > 1e-9:          # 1.0 is "no effect" — not stored
            out["modifiers"][k] = val
    return out, warns


# ── modifiers (config overrides) ────────────────────────────────────────

def shipped_config():
    """``config/sim_engine.json`` as shipped (no local file, no admin edits)."""
    return sim_config.build(local_path="", use_store=False)


def _strip(d):
    return {k: v for k, v in d.items() if not k.startswith("_")} if isinstance(d, dict) else d


def minimise(overrides, base):
    """Only the parts of *overrides* that differ from *base* (recursively)."""
    out = {}
    for k, v in (overrides or {}).items():
        if k.startswith("_"):
            continue
        b = base.get(k) if isinstance(base, dict) else None
        if isinstance(v, dict) and isinstance(b, dict):
            sub = minimise(v, b)
            if sub:
                out[k] = sub
        elif v != b:
            out[k] = copy.deepcopy(v)
    return out


def _check_modifiers(incoming, base, path, warns):
    """Keep only keys the engine knows, with the same kind of value."""
    out = {}
    for k, v in incoming.items():
        where = f"{path}.{k}" if path else k
        if k.startswith("_"):
            continue
        if not isinstance(base, dict) or k not in base:
            # New pitch profiles are the one open-ended table.
            if path == "pitches" and isinstance(v, dict):
                out[k] = v
                warns.append(f"modifiers: new pitch profile {k!r} (not selectable until the bot knows it)")
            else:
                warns.append(f"modifiers: unknown key {where!r} ignored")
            continue
        b = base[k]
        if isinstance(b, dict):
            if not isinstance(v, dict):
                warns.append(f"modifiers: {where} must be an object, ignored")
                continue
            sub = _check_modifiers(v, b, where, warns)
            if sub:
                out[k] = sub
        elif isinstance(b, bool) or b is None:
            out[k] = v if (b is None or isinstance(v, bool)) else _bool(v)
        elif isinstance(b, (int, float)):
            try:
                n = _num(v)
                out[k] = int(n) if isinstance(b, int) and float(n).is_integer() else n
            except (TypeError, ValueError):
                warns.append(f"modifiers: {where}={v!r} is not a number, ignored")
        elif isinstance(b, list):
            if isinstance(v, list):
                out[k] = v
            else:
                warns.append(f"modifiers: {where} must be a list, ignored")
        else:
            out[k] = v
    return out


def _flatten(d, prefix=""):
    flat = {}
    for k, v in (d or {}).items():
        if str(k).startswith("_"):
            continue
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            flat.update(_flatten(v, p))
        else:
            flat[p] = v
    return flat


# ── export ──────────────────────────────────────────────────────────────

def live_modifier_overrides():
    saved = store.current("modifiers")
    return saved if isinstance(saved, dict) else {}


def export_bundle(what="all", stadium_rows=None, cfg=None):
    """The JSON bundle. ``what`` = ``all`` | ``stadiums`` | ``modifiers``."""
    out = {"format": FORMAT, "version": VERSION,
           "exported_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z"}
    if what in ("all", "stadiums"):
        rows = stadium_rows if stadium_rows is not None else stadium_mod.live_rows()
        out["stadiums"] = [clean_stadium(r)[0] for r in rows]
    if what in ("all", "modifiers"):
        c = cfg or sim_config.get_config()
        out["modifiers"] = {k: copy.deepcopy(v) for k, v in c.items()
                            if k not in ("_warnings", "_broken")}
    return out


CSV_COLUMNS = (
    ["name", "aliases", "city", "country", "altitudeMeters"]
    + [f"boundary_{r}" for r in BOUNDARY_REGIONS]
    + ["outfieldSpeed", "dewFactor", "typicalPitch", "avgFirstInningsScore", "slope"]
    + [f"climate_{k}" for k in _CLIMATE]
    + [f"mod_{k}" for k in stadium_mod.MODIFIER_CHANNELS]
)


def stadiums_to_csv(rows=None):
    rows = rows if rows is not None else stadium_mod.live_rows()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for raw in rows:
        s, _ = clean_stadium(raw)
        row = {"name": s["name"], "aliases": "|".join(s["aliases"]), "city": s["city"],
               "country": s["country"], "altitudeMeters": s["altitudeMeters"],
               "outfieldSpeed": s["outfieldSpeed"], "dewFactor": s["dewFactor"],
               "typicalPitch": s["typicalPitch"], "avgFirstInningsScore": s["avgFirstInningsScore"],
               "slope": "yes" if s["slope"] else "no"}
        for r in BOUNDARY_REGIONS:
            row[f"boundary_{r}"] = s["boundaryM"][r]
        for k in _CLIMATE:
            row[f"climate_{k}"] = s["climate"].get(k, "")
        for k in stadium_mod.MODIFIER_CHANNELS:
            row[f"mod_{k}"] = s["modifiers"].get(k, "")
        w.writerow(row)
    return buf.getvalue()


def csv_to_stadiums(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip() for f in reader.fieldnames]:
        raise ValueError("CSV needs a header row with at least a 'name' column")
    rows = []
    for line in reader:
        line = {(k or "").strip(): (v or "").strip() for k, v in line.items()}
        if not line.get("name"):
            continue
        d = {"name": line["name"], "aliases": line.get("aliases", ""),
             "boundaryM": {}, "climate": {}, "modifiers": {}}
        for k, v in line.items():
            if v == "":
                continue
            if k.startswith("boundary_"):
                d["boundaryM"][k[9:]] = v
            elif k.startswith("climate_"):
                d["climate"][k[8:]] = v
            elif k.startswith("mod_"):
                d["modifiers"][k[4:]] = v
            elif k not in ("name", "aliases"):
                d[k] = v
        rows.append(d)
    return rows


# ── import ──────────────────────────────────────────────────────────────

def parse_upload(content, filename=""):
    """Work out what an uploaded file holds.

    Returns ``{"stadiums": list|None, "modifiers": dict|None}``; raises
    ``ValueError`` with a readable message when it is none of the formats.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    text = (content or "").strip()
    if not text:
        raise ValueError("the file is empty")
    is_csv = filename.lower().endswith(".csv")
    if not is_csv:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            if text.lstrip().startswith(("{", "[")):
                raise ValueError(f"not valid JSON: {exc}")
            is_csv = True
    if is_csv:
        return {"stadiums": csv_to_stadiums(text), "modifiers": None}

    if isinstance(data, list):
        return {"stadiums": data, "modifiers": None}
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object or list")
    # One stadium on its own (checked first: a ground has its own "modifiers").
    if data.get("name") and ("boundaryM" in data or "outfieldSpeed" in data):
        return {"stadiums": [data], "modifiers": None}
    if data.get("format") == FORMAT or "stadiums" in data or "modifiers" in data:
        if data.get("format") == FORMAT and int(data.get("version", 1)) > VERSION:
            raise ValueError(f"bundle version {data.get('version')} is newer than this bot understands")
        st = data.get("stadiums")
        mods = data.get("modifiers")
        if st is not None and not isinstance(st, list):
            raise ValueError("'stadiums' must be a list")
        if mods is not None and not isinstance(mods, dict):
            raise ValueError("'modifiers' must be an object")
        if st is None and mods is None:
            raise ValueError("the bundle has neither 'stadiums' nor 'modifiers'")
        return {"stadiums": st, "modifiers": mods}
    known = set(_strip(shipped_config()))
    if set(k for k in data if not k.startswith("_")) & known:
        return {"stadiums": None, "modifiers": data}
    raise ValueError("could not tell what this file is — expected a conditions bundle, "
                     "a stadium list, a stadium CSV, or a sim_engine.json-style object")


def _stadium_changes(old, new):
    fo, fn = _flatten(old), _flatten(new)
    return [(k, fo.get(k), fn.get(k)) for k in sorted(set(fo) | set(fn)) if fo.get(k) != fn.get(k)]


def plan_import(parsed, mode="merge", current_rows=None, current_overrides=None):
    """Validate and diff an upload. Nothing is saved.

    Returns a JSON-serialisable plan::

        {"mode", "errors": [...], "warnings": [...],
         "stadiums": None | {"added", "changed": [{"name", "fields": [[k, old, new]]}],
                             "removed", "unchanged", "result": [rows]},
         "modifiers": None | {"changes": [[path, old, new]], "result": {overrides}}}
    """
    if mode not in ("merge", "replace"):
        raise ValueError("mode must be 'merge' or 'replace'")
    plan = {"mode": mode, "errors": [], "warnings": [], "stadiums": None, "modifiers": None}
    base_cfg = shipped_config()

    if parsed.get("stadiums") is not None:
        current = [clean_stadium(r, base_cfg)[0]
                   for r in (current_rows if current_rows is not None else stadium_mod.live_rows())]
        by_name = {s["name"].lower(): s for s in current}
        incoming, seen = [], set()
        for i, raw in enumerate(parsed["stadiums"], 1):
            # Merge updates a known ground field by field: what the file leaves
            # out keeps its current value instead of falling back to a default.
            if mode == "merge" and isinstance(raw, dict):
                old = by_name.get(str(raw.get("name") or "").strip().lower())
                if old is not None:
                    raw = dict(sim_config.deep_merge(old, raw), name=old["name"])
            try:
                clean, w = clean_stadium(raw, base_cfg)
            except ValueError as exc:
                plan["errors"].append(f"stadium #{i}: {exc}")
                continue
            key = clean["name"].lower()
            if key in seen:
                plan["warnings"].append(f"{clean['name']}: listed twice, the last one wins")
                incoming = [s for s in incoming if s["name"].lower() != key]
            seen.add(key)
            plan["warnings"].extend(w)
            incoming.append(clean)
        inc_names = {s["name"].lower() for s in incoming}
        added, changed, unchanged = [], [], 0
        for s in incoming:
            old = by_name.get(s["name"].lower())
            if old is None:
                added.append(s["name"])
            else:
                diff = _stadium_changes(old, s)
                if diff:
                    changed.append({"name": s["name"], "fields": [list(d) for d in diff]})
                else:
                    unchanged += 1
        if mode == "replace":
            removed = [s["name"] for s in current if s["name"].lower() not in inc_names]
            result = incoming
        else:
            removed = []
            result = [s for s in current if s["name"].lower() not in inc_names] + incoming
            order = {s["name"].lower(): i for i, s in enumerate(current)}
            result.sort(key=lambda s: order.get(s["name"].lower(), len(order)))
        if not result:
            plan["errors"].append("the import would leave no stadiums at all")
        plan["stadiums"] = {"added": added, "changed": changed, "removed": removed,
                            "unchanged": unchanged, "result": result}

    if parsed.get("modifiers") is not None:
        warns = []
        incoming = _check_modifiers(parsed["modifiers"], base_cfg, "", warns)
        cur = current_overrides if current_overrides is not None else live_modifier_overrides()
        merged_overrides = incoming if mode == "replace" else sim_config.deep_merge(cur, incoming)
        effective = sim_config.deep_merge(base_cfg, merged_overrides)
        clamps = sim_config.validate(effective)
        warns.extend(f"modifiers: {c}" for c in clamps)
        result = minimise(effective, base_cfg)
        before = _flatten(sim_config.deep_merge(base_cfg, cur))
        after = _flatten(effective)
        changes = [[k, before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
                   if before.get(k) != after.get(k)]
        plan["warnings"].extend(warns)
        plan["modifiers"] = {"changes": changes, "result": result}
    return plan


def apply_plan(plan, session, created_by=None, note=None):
    """Save a plan from :func:`plan_import` (the caller commits). Returns what changed."""
    if plan.get("errors"):
        raise ValueError("the import has errors: " + "; ".join(plan["errors"]))
    done = []
    st = plan.get("stadiums")
    if st is not None:
        store.save("stadiums", st["result"], session, created_by=created_by,
                   note=note or f"import ({plan['mode']}): +{len(st['added'])} "
                                f"~{len(st['changed'])} -{len(st['removed'])}")
        done.append("stadiums")
    mods = plan.get("modifiers")
    if mods is not None:
        store.save("modifiers", mods["result"] or None, session, created_by=created_by,
                   note=note or f"import ({plan['mode']}): {len(mods['changes'])} value(s) changed")
        done.append("modifiers")
    return done


# ── single-ground edits (admin form) ───────────────────────────────────

def upsert_stadium(raw, session, original_name=None, created_by=None):
    """Add or edit one ground (renaming allowed). Returns ``(clean, warnings)``."""
    clean, warns = clean_stadium(raw)
    rows = [clean_stadium(r)[0] for r in stadium_mod.live_rows()]
    target = (original_name or clean["name"]).lower()
    if (original_name and clean["name"].lower() != target
            and any(r["name"].lower() == clean["name"].lower() for r in rows)):
        raise ValueError(f"a stadium called {clean['name']!r} already exists")
    idx = next((i for i, r in enumerate(rows) if r["name"].lower() == target), None)
    if idx is None:
        rows.append(clean)
        verb = "added"
    else:
        rows[idx] = clean
        verb = "edited"
    store.save("stadiums", rows, session, created_by=created_by, note=f"{verb} {clean['name']}")
    return clean, warns


def delete_stadium(name, session, created_by=None):
    rows = [clean_stadium(r)[0] for r in stadium_mod.live_rows()]
    keep = [r for r in rows if r["name"].lower() != str(name).lower()]
    if len(keep) == len(rows):
        raise ValueError(f"no stadium called {name!r}")
    if not keep:
        raise ValueError("cannot delete the last stadium")
    store.save("stadiums", keep, session, created_by=created_by, note=f"deleted {name}")


def ground_report(raw, cfg=None):
    """A few derived numbers for the admin list: what the formulas make of it."""
    cfg = cfg or sim_config.get_config()
    s = stadium_mod.from_dict(raw)
    six = stadium_mod.six_multiplier(s, cfg, stadium_mod.altitude_six_distance(s.altitude_m, cfg))
    return {"six": round(six, 2), "avg_boundary": round(s.avg_boundary, 1),
            "altitude": s.altitude_m > cfg["stadium"]["altitude"]["threshold"],
            "dew_full": s.dew_factor >= cfg["stadium"]["dew"]["threshold"]}
