# Scorecard themes

Open **Scorecard Designer** (`/settings/scorecard`) and choose a look from the
theme collection. **Exactly one theme is selected at a time**. Choose **Normal**
to turn seasonal artwork off and use the standard broadcast design.

| Theme | Presentation |
| --- | --- |
| Normal | Floodlit stadium, midnight navy, brushed gold, bright team tables and a framed Player of the Match showcase. |
| Spacial | The supplied original CMU HTML match summary, with angled panels, team colours, gold result banner and slim POTM ticker. |
| Diwali | Ornate lanterns, glowing oil lamps, marigolds, rangoli and gold trim. |
| Winter | Alpine peaks, frosted pines, a snowy cricket ball, ice rims and icicles. |
| Christmas | Garlands, warm fairy lights, ruby ribbons, presents and a snowy village. |
| New Year | Champagne fireworks, a golden skyline, an hourglass and the celebration year. |
| Makar Sankranti | Silk kites, sunset rooftops, brass bowls, festival sweets and a kite reel. |
| Republic Day | Tricolour ribbons, civic landmarks, a flypast and a 24-spoke Chakra. |

Select **Spacial** in the Scorecard tab to use the original HTML match summary
globally, including outside tournaments. The existing tournament graphics switch
can also enable it for one tournament. Batting, bowling and four-innings Test
cards keep their broadcast layouts. PNG export requires Chromium, included in
the Docker setup. See [Original HTML tournament summaries](tournament-summary-graphics.md).

The base and six seasonal designs use locally bundled illustrated artwork,
shaded metallic frames and their own palette. The batting/bowling cards, match summary,
result bar and Player of the Match strip follow the same selection. Text,
scores, crests, collectible cards and player statistics are always rendered
from live match values; none are baked into the background artwork.

The previews update before saving. Click **Save Scorecard Settings** to apply
the choice. The bot reads the setting fresh, including when it runs separately
from the admin website; no restart is needed after saving.

The theme applies to normal match summaries, Super Overs and simulated T20,
ODI and Test summaries. A tall Test card preserves the proportions of the
header and foreground artwork while extending the middle for its innings.

Changing the saved theme invalidates cached Telegram scorecard photo IDs in
the same transaction, so `/lastscorecard` regenerates the selected design.
Stored match payloads, delivery history and messages already posted in chats
are retained. Saving without changing the theme keeps the photo cache.

The broadcast base upgrade refreshes cached scorecard photo IDs once on startup,
including innings cards with the corrected statistics layout. This keeps archived
match values and delivery history; subsequent starts retain newly rendered IDs.
Statistics now fit into separate title, value, label and note bands. Oversized
text is fitted within its band, while explicit X/Y controls still reposition it.
Unused batting and bowling rows show muted dashes instead of padding zeroes.

`game_config.scorecard_theme` is the authoritative single selection. Its values
are `normal`, `spacial`, `diwali`, `winter`, `christmas`, `new_year`, `makar_sankranti` and
`republic_day`. The normal startup migration adds this nullable column. A null
selection falls back to the old `scorecard_diwali_theme` flag, preserving an
existing Diwali installation. Saving synchronizes that legacy flag with the
single choice; it cannot enable a second theme. Invalid or multiple selections
are rejected without changing the saved setting. Resetting settings selects
Normal.

Renderers accept the same theme keys; missing or unknown values draw Normal.
`services.card_identity` supplies the current selection to all callers,
including callers that supply their own text settings. New Year shows the
upcoming year from October onward and the current year earlier in the year.

Artwork lives in `static/scorecard_themes/*.webp`, with complete demo scorecard
thumbnails beside it. No network access or image-generation API is needed at
runtime. The catalog is in `services/scorecard_theme_catalog.py`; the rendering
helpers are in `services/scorecard_themes.py`. Missing artwork degrades to the
theme's readable palette rather than dropping the scorecard.
