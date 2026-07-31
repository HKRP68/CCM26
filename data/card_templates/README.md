# Card templates

The bot draws every player's info onto a blank template image when the player has
**no** admin-uploaded custom card. Committing a `template.*` image here (below) makes
the website **template** style the default output automatically — no admin save and
no committed `state.json`, so admin-saved layout (stored in the Telegram-pinned state
on storage-backed deploys) is never shadowed on redeploy. An explicit `card_style`
saved from the admin page always wins.

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

Each rarity keeps its **own** layout — cutout box, flag, and every text coordinate — so
tuning the Star card never moves anything on the Base or Legend card. Fine-tune them
live on the admin card-template page: pick a rarity tab, edit its numbers, and press
that tab's own **Save … Card** button (the button at the bottom of the page saves all
three at once). Saving persists to the runtime state (local `state.json` +
Telegram-pinned state on storage-backed deploys), no redeploy needed.

Layouts live under `variant_settings` in the state file:

```json
{"variant_settings": {"base": {...}, "star": {...}, "legend": {...}}}
```

A rarity with no saved layout of its own falls back to the legacy shared `settings`
block, which itself defaults to the v7.1 layout (`DEFAULT_TEMPLATE_SETTINGS` in
`services/card_template_service.py`). State written before layouts were split per rarity
therefore keeps rendering all three cards exactly as it did.
