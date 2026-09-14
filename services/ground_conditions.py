"""Pitch metadata for the auto-sim (/sim) — an adapter, not a second opinion.

This module used to read its own copy of the pitch model from
``data/ground_conditions.yaml``, a stale five-surface file whose numbers had
diverged from ``config/ground_conditions.yaml`` (the one the match engine
actually loads) — different scoring matrices, a different Green run factor, and
no Dusty, Bouncy or Even at all, so three of the seven surfaces a host can pick
had no metadata here. It also carried a private ``_IDEAL_TOSS`` table that
disagreed with ``engine.format_config`` about what to do on a road.

It now reads the same config the engine reads and the same identities
``engine.pitch_registry`` defines, so /sim describes the surface the match is
actually played on.
"""

import logging

from engine import ground_config, pitch_registry

logger = logging.getLogger(__name__)


def _run_factor(name):
    """Batting-friendliness index from the config, with a registry-ordered
    fallback if no config is on disk."""
    factor = ground_config.get_run_factor(name)
    if factor is not None:
        return float(factor)
    # Config missing: derive an index from the par ordering rather than invent
    # numbers that could drift from it.
    return round(0.86 + 0.05 * pitch_registry.order(name), 2)


def _favours_from(wicket_factors):
    """Heuristic 'favours' label from a config wicket-factor map.

    Kept for callers that hold a raw profile dict; the registry's own
    :func:`~engine.pitch_registry.favours` is the answer everywhere else.
    """
    if not wicket_factors:
        return "Balanced"
    pace = max((v for k, v in wicket_factors.items()
                if any(t in k.lower() for t in ("fast", "medium"))), default=0)
    spin = max((v for k, v in wicket_factors.items()
                if "spin" in k.lower()), default=0)
    if pace and pace >= spin and pace > 1.05:
        return "Pace"
    if spin and spin > 1.05:
        return "Spin"
    return "Balanced"


def list_pitches():
    """Pitch names the sim may draw from — the surfaces a host can pick."""
    return list(pitch_registry.SELECTABLE)


def get_pitch_meta(name):
    """Return ``{description, run_factor, ideal_toss, favours, par}`` for a pitch.

    ``ideal_toss`` is the registry's day-match call, which is the same table
    ``engine.format_config.correct_toss_choice`` is built from.
    """
    pitch = pitch_registry.normalise(name)
    profile = pitch_registry.profile(pitch)
    cfg = ground_config.get_pitch_profile(pitch) or {}
    return {
        "description": cfg.get("description") or profile.blurb,
        "run_factor": _run_factor(pitch),
        "ideal_toss": profile.toss,
        "favours": profile.favours,
        "par": pitch_registry.par(pitch),
    }
