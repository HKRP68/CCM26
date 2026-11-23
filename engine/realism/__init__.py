"""CCM26 Realism Engine v1 — a data-driven, player-independent ball model.

The engine consumes exactly six things from a player card — name, batting
style, batting rating, bowling style, bowling rating, overall rating — and
reads only the first five, because ``overall_rating`` is derived from the
batting and bowling numbers and counting it again would be a double count
(§1, §7 of the brief). It stores nothing on a player, adds no field to
``data/players.json``, and needs no migration. Two cards with the same
ratings and styles have identical ability distributions in identical
situations, by construction: the only per-player inputs to
:func:`engine.realism.outcome_distribution.build` are those five values.

Everything historical lives in ``data/simulation_reference/<version>/`` as
aggregate, player-independent reference data.

    from engine.realism import BallContext, build_distribution

    dist = build_distribution(BallContext(bat_rating=88, bowl_rating=74,
                                          over=17, innings=2, target=181))

See ``docs/realism_engine_v1.md`` for data provenance, licensing, rollout and
rollback.
"""

from engine.realism.config import (  # noqa: F401
    get_config, realism_enabled, reference_version, shadow_mode_enabled,
)
from engine.realism.context import BallContext, from_engine_call  # noqa: F401
from engine.realism.outcome_distribution import (  # noqa: F401
    RealismUnavailable, available_for, build as build_distribution,
    expected_runs, summarise,
)

__all__ = [
    "BallContext", "from_engine_call", "build_distribution", "summarise",
    "expected_runs", "available_for", "RealismUnavailable",
    "get_config", "realism_enabled", "shadow_mode_enabled", "reference_version",
]
