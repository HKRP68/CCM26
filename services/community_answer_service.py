"""Centralized answer validation for community games.

One place decides whether a submitted answer is correct, so every game behaves
consistently:

* case-insensitive, Unicode-normalised (NFKD → ASCII), with whitespace and
  punctuation stripped;
* matches only against an explicit **accept list** the game builds (the
  canonical answer plus approved aliases / unambiguous abbreviations) — never
  an arbitrary substring and never an ambiguous bare surname unless the game
  decided that surname is unique for this round;
* fuzzy matching is **off by default** and only ever a near-exact safety net
  when a game opts in.
"""

from __future__ import annotations

import re
import unicodedata


def normalize(s: str) -> str:
    """Lowercase, de-accent, strip punctuation, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def key(s: str) -> str:
    """Normalized with all spaces removed — the strict comparison key."""
    return normalize(s).replace(" ", "")


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def matches(guess: str, accept, *, fuzzy: bool = False) -> bool:
    """True if ``guess`` matches any entry of the accept list/iterable."""
    gk = key(guess)
    if not gk:
        return False
    accept_keys = {key(a) for a in (accept or []) if key(a)}
    if gk in accept_keys:
        return True
    if fuzzy:
        # Near-exact only: allow a single typo on reasonably long answers.
        for ak in accept_keys:
            if len(ak) >= 5 and _levenshtein(gk, ak) <= 1:
                return True
    return False


def name_variants(name: str) -> list:
    """Safe variants of a full player name for the accept list.

    Returns the full name and a dot-free form (so "A.B. de Villiers" and
    "AB de Villiers" both match). Deliberately does NOT include the bare
    surname — a game adds that itself only when it has verified the surname is
    unambiguous for the round.
    """
    name = (name or "").strip()
    if not name:
        return []
    variants = {name}
    no_dots = name.replace(".", " ")
    variants.add(no_dots)
    variants.add(name.replace(".", ""))
    # collapse double spaces
    return [re.sub(r"\s+", " ", v).strip() for v in variants if v.strip()]


def surname(name: str) -> str:
    """Last whitespace-delimited token of a name (its common surname)."""
    toks = normalize(name).split()
    return toks[-1] if toks else ""


def first_name(name: str) -> str:
    toks = normalize(name).split()
    return toks[0] if toks else ""
