# Original HTML tournament summaries

Open **Tournament → Manage → Match-summary graphics**, choose **Special
tournament graphics · Original HTML**, and save. Challenge League and Lets
Play tournaments have independent switches; the default remains standard.

You can also select **Spacial** in **Scorecard Designer** to use this original
HTML summary globally, including matches outside tournaments.

**Spacial Type 2** is a separate Scorecard theme and tournament graphics option.
It uses `templates/graphics/CMU_S5_Match_Summary_Type2.html`, preserved unchanged
from the supplied file, at its native **1536 × 1024** size. Type 2 displays
**runs-wickets** (e.g. `186-6`), team-colour headers, scores and body panels,
readable header text, and highlights actual Impact Players and the POTM in
either team's batting or bowling rows. The footer includes recorded batting
and bowling figures. Missing crests use initials rather than the sample logos.
Long team and player names fit their original slots. The tournament's explicit
design choice takes precedence over the global Scorecard theme. Changing
between types invalidates cached tournament summaries for replay.

The supplied file is preserved unchanged at
`templates/graphics/CMU_S5_Match_Summary.html`, including its embedded stadium
artwork, angled innings panels, gold result banner and slim POTM ticker.
Special summaries render this HTML at its original **1536 × 864** dimensions.
The standard renderer's custom text positions and seasonal backgrounds do not
override the original HTML design.

A separate binding layer fills the existing elements with tournament and match
values. The score keeps the HTML's **wickets/runs** format, e.g. `6/195`.
Team colours drive the existing metallic gradients; black or white text and
gradient shades keep header text at a minimum contrast ratio of 4.5:1.
Player rows retain not-out stars, the gold POTM crown and the green Impact badge.
Missing crests use initials, and missing data stays as dashes.

**Preview special graphics with your teams** displays the actual HTML in the
browser with clearly labelled sample values. **Open editable HTML preview**
retains click-to-edit fields. **Open PNG preview** exports the same HTML.
Preview edits are local to that page and never change tournament results.

PNG export uses Playwright and Chromium. The Docker image installs both. For
other hosts, install requirements and Chromium (`python -m playwright install
--with-deps chromium`), or set `SCORECARD_CHROMIUM_EXECUTABLE` to an installed
Chromium/Chrome binary. The HTML preview itself needs no server-side browser.
All artwork is embedded; exporting requires no external network requests.

Changing the design clears only this tournament's cached summary photo IDs, so
`/lastscorecard` can redraw it. Stored match data and delivery history remain.
Photos already posted to Telegram keep their original pixels.
