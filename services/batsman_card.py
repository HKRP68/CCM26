"""Generate CMU S1 batsman stats cards for in-match batting arrivals."""

from services.cmu_stats_card_service import generate_batting_stats_card


def generate_batsman_card(name, rating, bat_rating, stats, bat_hand="Right",
                          bowl_hand="Right", bowl_style="Medium Pacer") -> bytes | None:
    return generate_batting_stats_card(
        name=name,
        rating=rating,
        bat_rating=bat_rating,
        stats=stats,
        bat_hand=bat_hand,
        bowl_hand=bowl_hand,
        bowl_style=bowl_style,
    )
