# Scorecard themes

Open **Scorecard Designer** (`/settings/scorecard`) and use **Diwali theme**:

- **Off — Normal:** use the original scorecard designs. This is the default.
- **On — Diwali:** use a navy-and-gold summary with lights, lanterns, fireworks,
  diyas, rangoli, and a Happy Diwali greeting. Batting and bowling cards get a
  matching festive frame. Team colours, crests, scores and player statistics
  keep their usual values.

The previews update before saving. Click **Save Scorecard Settings** to apply
the choice. The bot reads the setting fresh, including when it runs separately
from the admin website; no restart is needed after saving.

The theme applies to normal match summaries, Super Overs and simulated T20,
ODI and Test summaries. Existing databases receive
`game_config.scorecard_diwali_theme BOOLEAN DEFAULT FALSE` on startup through
the normal migration path.

Changing the saved theme invalidates cached Telegram scorecard photo IDs in
the same transaction, so `/lastscorecard` regenerates the selected design.
Stored match payloads, delivery history and messages already posted in chats
are retained. Saving without changing the theme keeps the photo cache.

Renderers accept `theme="normal"` or `theme="diwali"`; missing or unknown values
use normal. `services.card_identity` supplies the live theme to all callers,
including callers that supply their own text settings. Festival artwork is
drawn locally with Pillow and bundled fonts, without remote image downloads.
