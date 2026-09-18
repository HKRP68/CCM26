# Card font assets

These files are committed, and the card renderers expect them here. The host
filesystem is rebuilt on every deploy, so a font the renderers need has to be in
the repo — an absent one silently degrades every card to DejaVu Sans, which is
what happened to the body face before it was noticed.

Filenames are matched by the candidate tuples in
`services/match_summary_card.py`; renaming a file here without updating those
tuples turns the font off rather than raising.

| File | Used for | Licence |
| --- | --- | --- |
| `Anton-Regular.ttf` | the `MATCH SUMMARY` headline and the big innings scores, sheared for the italic | SIL OFL 1.1 |
| `BebasNeue-Regular.ttf` | condensed display: column headers, micro-caps, metric values | SIL OFL 1.1 |
| `BricolageGrotesque-Regular.ttf` | body: player names, table text | SIL OFL 1.1 |
| `Caveat-Bold.ttf` | the `Game Changer!` script flourish on the POTM strip | SIL OFL 1.1 |
| `Lato-Italic.ttf` | italic body | SIL OFL 1.1 |
| `Geometos.ttf`, `KeepCalm-Medium.ttf`, `RussoOne-Regular.ttf` | older batting/bowling card faces | see each foundry |
| `PRIMETIME ¸ PERSONAL USE ONLY.ttf` | legacy; personal-use licence, so keep it off anything published | personal use only |

The Dockerfile also installs `fonts-dejavu-core` and `fonts-bebas-neue`. DejaVu
is the last-resort fallback and the only face with wide emoji/symbol coverage,
so it stays.
