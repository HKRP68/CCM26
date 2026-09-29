"""Conditions Engine v4 — weather, time of day, stadiums, ball aging and drama.

Every tuning number lives in ``config/sim_engine.json``. The modules here are
pure functions over small dataclasses (``engine.sim.models``) so each rule can
be tested on its own; randomness only ever comes from an explicitly passed
``engine.sim.rng.SimRng``, which makes a whole match reproducible from a seed.

Two consumers:

* ``engine.sim.match`` — a standalone T20 / ODI / Test simulator with
  commentary, scorecard and an explained summary (``python -m tools.sim_match``).
* ``engine.sim.hook`` — one bounded weight hook for the live /letsplay and CIPL
  engine (``services.cipl_match``), applying only the *conditions* delta so the
  calibrated pitch profiles keep their par bands.

See ``docs/conditions-engine-v4.md``.
"""
