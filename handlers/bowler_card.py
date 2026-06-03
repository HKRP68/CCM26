"""Generate CMU S1 bowler stats cards for new-bowler arrivals."""

from services.cmu_stats_card_service import generate_bowling_stats_card


def generate_bowler_card(name, rating, bowl_rating, stats, bat_hand="Right",
                         bowl_hand="Right", bowl_style="Medium Pacer") -> bytes | None:
    return generate_bowling_stats_card(
        name=name,
        rating=rating,
        bowl_rating=bowl_rating,
        stats=stats,
        bat_hand=bat_hand,
        bowl_hand=bowl_hand,
        bowl_style=bowl_style,
    )
