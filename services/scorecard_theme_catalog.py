"""One authoritative selection and visual identity for every scorecard theme."""

from dataclasses import dataclass

NORMAL = "normal"
DIWALI = "diwali"
WINTER = "winter"
CHRISTMAS = "christmas"
NEW_YEAR = "new_year"
SANKRANTI = "makar_sankranti"
REPUBLIC_DAY = "republic_day"


@dataclass(frozen=True)
class ScorecardTheme:
    key: str
    name: str
    edition: str
    description: str
    greeting: str
    accent: tuple
    pale: tuple
    ink: tuple
    band: tuple
    css: str
    snowy: bool = False
    foreground_top: float = .70

    @property
    def artwork(self):
        return f"/static/scorecard_themes/{self.key}.webp" if self.key != NORMAL else None


THEMES = (
    ScorecardTheme(NORMAL, "Normal", "THE ORIGINAL", "Clean paper, team colours, classic match presentation.",
                   "Game Changer!", (199, 146, 44), (233, 201, 132), (7, 26, 52),
                   ((7, 27, 53), (14, 48, 89)), "#f0f3f7"),
    ScorecardTheme(DIWALI, "Diwali", "FESTIVAL OF LIGHTS", "Glowing diyas, ornate lanterns, rangoli and a velvet night sky.",
                   "Happy Diwali", (255, 194, 66), (255, 230, 171), (255, 255, 255),
                   ((19, 12, 39), (55, 20, 50)), "#281332"),
    ScorecardTheme(WINTER, "Winter", "THE WINTER EDITION", "Moonlit alpine peaks, frosted pines and crystalline snow.",
                   "Winter Edition", (132, 220, 255), (226, 247, 255), (255, 255, 255),
                   ((4, 30, 60), (10, 61, 94)), "#17496b", snowy=True),
    ScorecardTheme(CHRISTMAS, "Christmas", "A FESTIVE CLASSIC", "Evergreen garlands, ruby ribbons, a snowy village and warm lights.",
                   "Merry Christmas", (240, 193, 99), (255, 233, 177), (255, 255, 255),
                   ((5, 28, 40), (12, 57, 51)), "#13352f", snowy=True, foreground_top=.58),
    ScorecardTheme(NEW_YEAR, "New Year", "THE MIDNIGHT EDITION", "Champagne fireworks, a golden skyline and luminous reflections.",
                   "Happy New Year", (255, 201, 83), (255, 237, 190), (255, 255, 255),
                   ((8, 14, 35), (37, 25, 54)), "#201a36"),
    ScorecardTheme(SANKRANTI, "Makar Sankranti", "THE KITE FESTIVAL", "Silk kites, a golden sunset, brass bowls and festival sweets.",
                   "Happy Makar Sankranti", (255, 199, 74), (255, 232, 167), (255, 255, 255),
                   ((30, 21, 61), (68, 30, 67)), "#663658"),
    ScorecardTheme(REPUBLIC_DAY, "Republic Day", "THE PRIDE OF INDIA", "Tricolour ribbons, sandstone landmarks and a ceremonial flypast.",
                   "Republic Day", (245, 130, 36), (255, 224, 170), (7, 31, 61),
                   ((4, 34, 64), (6, 61, 72)), "#e7f1f4", foreground_top=.50),
)
THEME_BY_KEY = {theme.key: theme for theme in THEMES}


def normalize_theme(value):
    key = str(value or "").strip().lower()
    return key if key in THEME_BY_KEY else NORMAL


def theme_from_config(config):
    """A stored selection wins; an old Diwali switch survives the upgrade."""
    selected = config.get("scorecard_theme")
    if selected is not None:
        return normalize_theme(selected)
    return DIWALI if config.get("scorecard_diwali_theme") else NORMAL


def theme_definition(value):
    return THEME_BY_KEY[normalize_theme(value)]
