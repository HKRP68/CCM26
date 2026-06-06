from services.miniapp_activity import build_message


def test_gspin_activity_message_escapes_user_and_reward():
    text = build_message(
        "gspin",
        "A&B <XI>",
        reward={"type": "player", "player_name": "Virat <King>", "player_rating": 99},
    )

    assert text == (
        "🎡 <b>A&amp;B &lt;XI&gt;</b> spun the GSpin and won "
        "<b>Virat &lt;King&gt;</b> (99 OVR)!"
    )


def test_open_pack_activity_highlights_highest_rated_pull():
    text = build_message(
        "open_pack",
        "Rohit",
        pack_name="Mega Pack",
        players=[
            {"name": "Player A", "rating": 82},
            {"name": "Player B", "rating": 91},
            {"name": "Player C", "rating": 88},
        ],
    )

    assert text == (
        "📦 <b>Rohit</b> opened a <b>Mega Pack</b> and pulled "
        "<b>Player B</b> (91 OVR)!"
    )


def test_activity_messages_cover_requested_action_types():
    cases = [
        ("daily", {"coins": 5000, "gems": 10, "streak": 3}),
        ("buy_pack", {"pack_name": "Premium Pack"}),
        ("buy_player", {"player_name": "MS Dhoni", "rating": 95, "price": 100000}),
        ("sell_player", {"player_name": "MS Dhoni", "rating": 95, "sell": 50000}),
        ("upgrade_player", {"player_name": "MS Dhoni", "version": "Gold", "rating": 97}),
        ("apply_trait", {"player_name": "MS Dhoni", "trait_name": "Finisher", "trait_emoji": "🔥"}),
    ]

    for action, ctx in cases:
        assert build_message(action, "Captain", **ctx)
