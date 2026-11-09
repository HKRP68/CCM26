"""Display recorded wallet snapshots without disguising legacy estimates."""


def annotate_balances(activities, current_coins, current_gems):
    """Annotate newest-first rows, including intervening filtered-out rows.

    Old logs recorded deltas only. Missing or archived entries and concurrent
    writes can make backwards estimates inaccurate; stored snapshots provide
    a fresh anchor without rewriting historical evidence.
    """
    coins = current_coins or 0
    gems = current_gems or 0
    for row in activities:
        stored_coins = getattr(row, "coins_balance_after", None)
        stored_gems = getattr(row, "gems_balance_after", None)
        row._coins_recorded = stored_coins is not None
        row._gems_recorded = stored_gems is not None
        row._net_coins = stored_coins if row._coins_recorded else coins
        row._net_gems = stored_gems if row._gems_recorded else gems
        row._coins_before = row._net_coins - (row.coins_change or 0)
        coins = row._coins_before
        gems = row._net_gems - (row.gems_change or 0)
