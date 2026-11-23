"""Reference-data loading and hierarchical smoothing.

A reference file is a tree of context nodes. Each node carries *counts* over
the eight engine outcomes plus the number of deliveries behind them, and names
its parent. The lookup walks the tree from the root down the path a delivery
belongs to, smoothing each level into its parent with the formula the brief
(§6) specifies:

    P(outcome | context) = (count + alpha * P_parent(outcome))
                           / (total + alpha)

So a node with 50 deliveries behind it is dominated by its parent, a node with
50,000 stands on its own, and nothing in between needs a special case. The
``alpha`` per level is in ``config/realism.json`` and is calibrated, not
guessed — see ``tools/build_realism_reference.py --report-coverage``.

Two honesty mechanisms are built in rather than bolted on:

* Every node declares ``count_kind``: ``"observed"`` (deliveries actually
  counted from ball-by-ball data) or ``"pseudo"`` (a prior expressed as
  pseudo-counts, whose magnitude is a *confidence weight*, not a sample
  size). :func:`coverage` reports the split, and the manifest records it, so a
  prior can never be read off as a measurement.

* A file that fails :func:`validate` is refused outright. The caller then
  falls back to the legacy engine with a log line. There is no code path that
  substitutes invented numbers for a missing reference.

Loading is cached per (version, format) and reads from disk once. Nothing here
touches the network — §11 of the brief requires a simulated ball to be
network-free, and this module is the only thing that reads a file at all.
"""

import json
import logging
import os
import threading

from engine.realism.config import REFERENCE_ROOT, get_config

logger = logging.getLogger(__name__)

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")
ROOT = "root"
_TOL = 1e-6

_lock = threading.Lock()
_cache = {}
_warned = set()


class ReferenceError(Exception):
    """The reference file is missing, malformed or inconsistent."""


# --------------------------------------------------------------------------
# Loading & validation
# --------------------------------------------------------------------------
def reference_path(version: str, format_name: str) -> str:
    return os.path.join(REFERENCE_ROOT, version,
                        "%s_baselines.json" % format_name.lower())


def manifest_path(version: str) -> str:
    return os.path.join(REFERENCE_ROOT, version, "manifest.json")


def validate(doc: dict) -> dict:
    """Raise :class:`ReferenceError` unless *doc* is a usable reference."""
    if not isinstance(doc, dict):
        raise ReferenceError("reference is not an object")
    for key in ("schema_version", "format", "model_version", "nodes", "default_competition"):
        if key not in doc:
            raise ReferenceError("reference missing %r" % key)
    if int(doc["schema_version"]) != 1:
        raise ReferenceError("unsupported schema_version %r" % doc["schema_version"])
    nodes = doc["nodes"]
    if not isinstance(nodes, dict) or ROOT not in nodes:
        raise ReferenceError("reference has no %r node" % ROOT)

    for key, node in nodes.items():
        if not isinstance(node, dict):
            raise ReferenceError("node %r is not an object" % key)
        counts = node.get("counts")
        if not isinstance(counts, dict):
            raise ReferenceError("node %r has no counts" % key)
        missing = [o for o in OUTCOMES if o not in counts]
        if missing:
            raise ReferenceError("node %r missing outcomes %s" % (key, missing))
        for o in OUTCOMES:
            v = counts[o]
            if not isinstance(v, (int, float)):
                raise ReferenceError("node %r count %r is not numeric" % (key, o))
            if v < 0 or v != v or v in (float("inf"), float("-inf")):
                raise ReferenceError("node %r count %r is invalid (%r)" % (key, o, v))
        if sum(float(counts[o]) for o in OUTCOMES) <= 0:
            raise ReferenceError("node %r has zero total count" % key)
        if node.get("count_kind") not in ("observed", "pseudo"):
            raise ReferenceError("node %r has no valid count_kind" % key)
        parent = node.get("parent")
        if key == ROOT:
            if parent not in (None, ""):
                raise ReferenceError("root node must have no parent")
        else:
            if parent not in nodes:
                raise ReferenceError("node %r has unknown parent %r" % (key, parent))

    # No cycles: every node must reach the root.
    for key in nodes:
        seen, cur = set(), key
        while cur and cur != ROOT:
            if cur in seen:
                raise ReferenceError("node %r is in a parent cycle" % key)
            seen.add(cur)
            cur = nodes[cur].get("parent")
        if cur != ROOT:
            raise ReferenceError("node %r does not reach the root" % key)
    return doc


def load(version: str = None, format_name: str = "T20", reload: bool = False):
    """Return a validated reference, or ``None`` if unusable.

    Never raises: a bad reference is a fallback condition, not a crash.
    """
    from engine.realism.config import reference_version
    version = version or reference_version()
    key = (version, format_name.lower())
    with _lock:
        if not reload and key in _cache:
            return _cache[key]
    path = reference_path(version, format_name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = validate(json.load(fh))
        doc = _prepare(doc)
    except FileNotFoundError:
        doc = None
        if key not in _warned:
            _warned.add(key)
            logger.warning("realism: no reference at %s; legacy engine will be used",
                           path)
    except Exception as exc:
        doc = None
        if key not in _warned:
            _warned.add(key)
            logger.error("realism: reference %s is unusable (%s); legacy engine "
                         "will be used", path, exc)
    with _lock:
        _cache[key] = doc
    return doc


def _prepare(doc: dict) -> dict:
    """Pre-compute per-node totals and the smoothed distribution cache."""
    for node in doc["nodes"].values():
        node["_total"] = sum(float(node["counts"][o]) for o in OUTCOMES)
    doc["_smoothed"] = {}
    doc["_alpha"] = dict(get_config()["smoothing"]["alpha"])
    return doc


def clear_cache():
    with _lock:
        _cache.clear()
        _warned.clear()


# --------------------------------------------------------------------------
# Hierarchical smoothing
# --------------------------------------------------------------------------
def _raw_dist(node) -> dict:
    total = node["_total"]
    return {o: float(node["counts"][o]) / total for o in OUTCOMES}


def _alpha_for(doc, node_key) -> float:
    """The smoothing strength for a node, by its depth in the hierarchy."""
    depth = node_key.count("/")
    level = ("format", "competition", "phase", "innings", "wickets", "chase")
    name = level[min(depth, len(level) - 1)]
    alpha = doc.get("_alpha") or {}
    # The format level is the top of the tree and is never smoothed into the
    # root by a configurable alpha — the root is the same data.
    return float(alpha.get(name, 0.0))


def smoothed(doc: dict, node_key: str) -> dict:
    """The smoothed distribution at *node_key*, computed root-downwards."""
    cache = doc["_smoothed"]
    hit = cache.get(node_key)
    if hit is not None:
        return hit
    nodes = doc["nodes"]
    node = nodes.get(node_key)
    if node is None:
        return smoothed(doc, ROOT)
    parent_key = node.get("parent")
    if not parent_key:
        dist = _raw_dist(node)
    else:
        parent = smoothed(doc, parent_key)
        alpha = _alpha_for(doc, node_key)
        total = node["_total"]
        denom = total + alpha
        dist = {o: (float(node["counts"][o]) + alpha * parent[o]) / denom
                for o in OUTCOMES}
    # Guard against float drift so callers get an exactly-normalised dict.
    s = sum(dist.values())
    if s > 0:
        dist = {o: v / s for o, v in dist.items()}
    cache[node_key] = dist
    return dist


def lookup(doc: dict, path) -> tuple:
    """Smoothed distribution for the deepest node on *path* that exists.

    Returns ``(dist, node_key, meta)`` where ``meta`` reports the node's
    sample size and whether it came from observation or a prior.
    """
    nodes = doc["nodes"]
    key = ROOT
    for part in path:
        candidate = part if key == ROOT else "%s/%s" % (key, part)
        if candidate in nodes:
            key = candidate
        else:
            break
    node = nodes[key]
    meta = {
        "node": key,
        "n": node["_total"],
        "count_kind": node.get("count_kind"),
        "depth": key.count("/") + (0 if key == ROOT else 1),
    }
    return smoothed(doc, key), key, meta


def competition_key(doc: dict, requested: str = None) -> str:
    """Resolve the competition/era baseline to use."""
    from engine.realism.config import baseline_override
    for candidate in (requested, baseline_override(), doc.get("default_competition")):
        if not candidate:
            continue
        fmt = doc.get("format")
        if "%s/%s" % (fmt, candidate) in doc["nodes"]:
            return candidate
    return doc.get("default_competition") or ""


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def coverage(doc: dict) -> dict:
    """Sample sizes and observed/pseudo split, for the audit tools and docs."""
    nodes = doc["nodes"]
    observed = sum(1 for n in nodes.values() if n.get("count_kind") == "observed")
    pseudo = len(nodes) - observed
    obs_n = sum(n["_total"] for n in nodes.values()
                if n.get("count_kind") == "observed")
    return {
        "format": doc.get("format"),
        "model_version": doc.get("model_version"),
        "nodes": len(nodes),
        "observed_nodes": observed,
        "pseudo_nodes": pseudo,
        "observed_deliveries": obs_n,
        "default_competition": doc.get("default_competition"),
        "competitions": sorted(
            k.split("/", 1)[1] for k in nodes
            if k.count("/") == 1 and k.startswith("%s/" % doc.get("format"))),
        "provenance": doc.get("provenance", {}),
    }


def anchors(doc: dict, competition: str = None) -> dict:
    """The measured real-data targets this file was fitted to, if any."""
    comp = competition or doc.get("default_competition")
    return ((doc.get("anchors") or {}).get(comp)
            or (doc.get("anchors") or {}).get("_all") or {})
