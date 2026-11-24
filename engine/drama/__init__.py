"""Balanced Drama Simulation Engine v1 (Codex Prompt 05).

A bounded drama overlay that sits ON TOP of the realism engine. It never
samples its own replacement outcome and never predetermines a competitive
result: it shapes the existing outcome weights, within one shared budget,
immediately before the single authoritative sample. Three presets:

* ``REALISTIC``  — realism engine only, no drama overlay;
* ``BALANCED``   — realism + a bounded drama overlay, no scripted outcomes
                   (the target competitive preset);
* ``CINEMATIC``  — higher volatility and the legacy ScenarioEngine's scripted
                   finales, for explicitly designated exhibition/challenge
                   modes only.

No new or hidden player attribute is introduced; every drama input is live,
match-only state (balls faced, score, wickets, phase, recent momentum). See
docs/balanced_drama_engine.md.
"""

from engine.drama import config  # noqa: F401
