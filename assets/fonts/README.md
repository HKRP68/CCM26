# Summary-card font assets

Binary font files are intentionally not committed here. If deployment bundles
custom summary-card fonts, place them in this directory using the filenames
listed by `services/match_summary_card.py`:

- `BebasNeue-Regular.ttf` for the display face.
- Body candidates such as `BricolageGrotesque-SemiBold.ttf` or
  `BricolageGrotesque-Bold.ttf`.
- An italic-capable body face such as `Lato-RegularItalic.ttf` if player names
  should render in italics instead of falling back to DejaVu Oblique.
