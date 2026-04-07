"""Country name → flag emoji mapping."""

FLAGS = {
    "India": "🇮🇳", "Australia": "🇦🇺", "England": "🇬🇧", "Pakistan": "🇵🇰",
    "South Africa": "🇿🇦", "New Zealand": "🇳🇿", "Sri Lanka": "🇱🇰",
    "Bangladesh": "🇧🇩", "Afghanistan": "🇦🇫", "West Indies": "🏴",
    "Zimbabwe": "🇿🇼", "Ireland": "🇮🇪", "Netherlands": "🇳🇱",
    "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "UAE": "🇦🇪", "Nepal": "🇳🇵", "Oman": "🇴🇲",
    "USA": "🇺🇸", "Canada": "🇨🇦", "Kenya": "🇰🇪", "Namibia": "🇳🇦",
    "Papua New Guinea": "🇵🇬", "Hong Kong": "🇭🇰",
}


def get_flag(country: str) -> str:
    return FLAGS.get(country, "🏳️")
