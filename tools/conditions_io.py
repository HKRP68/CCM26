"""Export / import the Conditions Engine's stadiums and modifiers from a shell.

    python -m tools.conditions_io export conditions.json            # everything
    python -m tools.conditions_io export stadiums.csv --what stadiums
    python -m tools.conditions_io import conditions.json            # preview only
    python -m tools.conditions_io import conditions.json --apply    # save to the DB
    python -m tools.conditions_io import tuned.json --mode replace --to-files

``import`` always prints the preview (added / changed / removed, every clamp and
dropped key). ``--apply`` saves to the database exactly as the admin site does
(with history); ``--to-files`` writes the shipped files instead —
``data/stadiums.json`` and ``config/sim_engine.local.json`` — for committing a
balancing pass to git.
"""

import argparse
import json
import os
import sys

from services import conditions_io


def _print_plan(plan):
    st, mods = plan.get("stadiums"), plan.get("modifiers")
    if st is not None:
        print(f"Stadiums: +{len(st['added'])} added, ~{len(st['changed'])} changed, "
              f"-{len(st['removed'])} removed, {st['unchanged']} unchanged "
              f"→ {len(st['result'])} total")
        for n in st["added"]:
            print(f"  + {n}")
        for c in st["changed"]:
            print(f"  ~ {c['name']}")
            for k, old, new in c["fields"]:
                print(f"      {k}: {old!r} → {new!r}")
        for n in st["removed"]:
            print(f"  - {n}")
    if mods is not None:
        print(f"Modifiers: {len(mods['changes'])} value(s) change")
        for k, old, new in mods["changes"]:
            print(f"  {k}: {old!r} → {new!r}")
    for w in plan["warnings"]:
        print(f"  ! {w}")
    for e in plan["errors"]:
        print(f"  ERROR {e}")


def cmd_export(args):
    if args.path.lower().endswith(".csv"):
        body = conditions_io.stadiums_to_csv()
    else:
        body = json.dumps(conditions_io.export_bundle(args.what), indent=1, ensure_ascii=False)
    if args.path == "-":
        sys.stdout.write(body)
    else:
        with open(args.path, "w", encoding="utf-8") as fh:
            fh.write(body)
        print(f"wrote {args.path}")
    return 0


def cmd_import(args):
    with open(args.path, "rb") as fh:
        parsed = conditions_io.parse_upload(fh.read(), args.path)
    if args.only == "stadiums":
        parsed["modifiers"] = None
    elif args.only == "modifiers":
        parsed["stadiums"] = None
    current_rows = None
    current_overrides = None
    if args.to_files:
        # Diff against the files themselves, not whatever the database holds.
        from engine.sim import stadium
        from engine.sim.config import LOCAL_PATH
        current_rows = stadium.file_rows()
        current_overrides = {}
        if os.path.exists(LOCAL_PATH):
            with open(LOCAL_PATH, encoding="utf-8") as fh:
                current_overrides = json.load(fh)
    plan = conditions_io.plan_import(parsed, mode=args.mode, current_rows=current_rows,
                                     current_overrides=current_overrides)
    _print_plan(plan)
    if plan["errors"]:
        return 2
    if args.to_files:
        return _write_files(plan)
    if not args.apply:
        print("\n(preview only — add --apply to save to the database, or --to-files)")
        return 0
    from database import get_session
    db = get_session()
    try:
        done = conditions_io.apply_plan(plan, db, created_by="cli", note=args.note)
        db.commit()
    finally:
        db.close()
    print(f"saved {' and '.join(done)} to the database")
    return 0


def _write_files(plan):
    from engine.sim.config import LOCAL_PATH
    from engine.sim.stadium import DB_PATH
    if plan.get("stadiums") is not None:
        with open(DB_PATH, encoding="utf-8") as fh:
            readme = json.load(fh).get("_readme", "")
        out = '{\n  "_readme": ' + json.dumps(readme, ensure_ascii=False) + ',\n  "stadiums": [\n'
        out += ",\n".join("    " + json.dumps(s, ensure_ascii=False) for s in plan["stadiums"]["result"])
        out += "\n  ]\n}\n"
        with open(DB_PATH, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"wrote {DB_PATH}")
    if plan.get("modifiers") is not None:
        with open(LOCAL_PATH, "w", encoding="utf-8") as fh:
            json.dump(plan["modifiers"]["result"], fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {LOCAL_PATH}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Conditions Engine stadiums & modifiers export/import")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write a bundle (.json) or the stadiums (.csv)")
    e.add_argument("path", help="output file, or - for stdout")
    e.add_argument("--what", choices=["all", "stadiums", "modifiers"], default="all")
    i = sub.add_parser("import", help="preview (and optionally apply) an import")
    i.add_argument("path")
    i.add_argument("--mode", choices=["merge", "replace"], default="merge")
    i.add_argument("--only", choices=["all", "stadiums", "modifiers"], default="all")
    i.add_argument("--apply", action="store_true", help="save to the database")
    i.add_argument("--to-files", action="store_true",
                   help="write data/stadiums.json and config/sim_engine.local.json instead")
    i.add_argument("--note", default=None)
    args = ap.parse_args(argv)
    try:
        return cmd_export(args) if args.cmd == "export" else cmd_import(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
