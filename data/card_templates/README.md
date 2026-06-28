# Card templates

The bot draws every player's info onto a blank template image when the player has
**no** admin-uploaded custom card. `state.json` here sets `card_style: "template"` so
this is the default output.

## Add your template

Commit the **blank** card frame (static labels/borders only — no player photo, name,
or numbers; those are drawn by the engine) as:

```text
data/card_templates/template.jpeg      # or .png / .webp
```

Requirements:
- Export at **1536×1024** (the engine resizes any template to this; off-ratio images
  get distorted).
- Print only the static parts on it (e.g. "ACTIVE PLAYER", "OVR", "BATTING POWER",
  "BOWLING SPECS", frame). The engine overlays: name, category, OVR, batting/bowling
  ratings, country + flag, batting/bowling style, and an optional player portrait.

Optional per-rarity frames `template_star.*` / `template_legend.*` are used for
Star/Legend versions; without them those players fall back to `template.*`.

## Adjust field positions

Field coordinates live in `state.json` under `settings` (seeded from the v7.1
defaults). Fine-tune them live on the admin card-template page — saving there
overwrites `state.json`, no redeploy needed.
