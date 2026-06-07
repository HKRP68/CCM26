"""Team assembly helpers for the /sim command."""


def base_player_id(player):
    """Return the canonical base-player id for a Player row or variant row."""
    return player.parent_player_id or player.id


def append_distinct_base_players(rows, candidates, target_size=11):
    """Append candidates until rows contain target_size distinct base players."""
    seen = {base_player_id(player) for player in rows}
    for player in candidates:
        player_base_id = base_player_id(player)
        if player_base_id in seen:
            continue
        rows.append(player)
        seen.add(player_base_id)
        if len(rows) >= target_size:
            break
    return rows


def distinct_base_players(players, target_size=11):
    """Return up to target_size players, skipping duplicate versions."""
    return append_distinct_base_players([], players, target_size=target_size)
