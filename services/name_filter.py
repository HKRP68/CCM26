"""One banned-words filter for every name a user can type.

Team names (/teamname) and typed Career Player names share a single list, kept
in ``game_config.career_name_blocklist`` (the column predates team names, and
reusing it means nothing an admin already saved is lost). A second list,
``game_config.name_allowlist``, holds innocent words that happen to contain a
banned one — the "Scunthorpe problem" — so they can be let through without
weakening the ban everywhere else.

A name is checked in three shapes, and blocked if any of them contains a term:

* **squashed** — lower-case letters and digits only, so ``"S. l u-r"`` is
  ``"slur"`` and spacing or punctuation can't walk a word past the list;
* **de-leeted** — the usual look-alike symbols read as letters first
  (``$lur``, ``5lur``, ``sh1t``), then squashed;
* **collapsed** — the de-leeted form with repeated letters squeezed down:
  runs of three or more to two (``boooob`` → ``boob``), and every run to one
  (``sluur`` → ``slur``). The fully squeezed shape is only matched against
  terms that have no doubled letter of their own, so banning ``boob`` never
  catches ``Bobby``.

Allowed words are cut out of each shape before the banned ones are looked for.
"""

import logging
import re

logger = logging.getLogger(__name__)

# What /teamname accepts: 3-50 letters, digits, spaces, apostrophes, hyphens.
# The website's Team Names page holds an admin's rename to the same rule.
TEAM_NAME_REGEX = re.compile(r"^[a-zA-Z0-9 '\-]{3,50}$")

_ALNUM = re.compile(r"[^a-z0-9]+")
_REPEATS = re.compile(r"(.)\1+")
_LONG_RUNS = re.compile(r"(.)\1{2,}")

# Look-alike characters read as the letter they stand in for.
_LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "9": "g", "@": "a", "$": "s", "!": "i", "|": "i", "+": "t",
})


def _squash(value):
    """Lower-case a name down to its letters and digits."""
    return _ALNUM.sub("", (value or "").lower())


def _deleet(value):
    return _squash((value or "").lower().translate(_LEET))


def _collapse(value):
    return _REPEATS.sub(r"\1", value or "")


def terms(raw):
    """A comma/newline separated list as squashed terms, blanks dropped."""
    parts = re.split(r"[,\n\r]+", raw or "")
    return [t for t in (_squash(p) for p in parts) if t]


def settings(session=None, cfg=None):
    """The shared lists as ``{"blocklist": str, "allowlist": str}``."""
    if cfg is None:
        try:
            from services.config_service import get_config
            cfg = get_config(session)
        except Exception:
            logger.exception("name filter settings read failed")
            cfg = {}
    return {
        "blocklist": cfg.get("career_name_blocklist") or "",
        "allowlist": cfg.get("name_allowlist") or "",
    }


def _conf(conf, session=None):
    if isinstance(conf, dict) and "blocklist" in conf:
        if "allowlist" not in conf:
            conf = dict(conf, allowlist=settings(session)["allowlist"])
        return conf
    return settings(session)


def variants(name):
    """The shapes a name is checked in, as ``(shape, fully_collapsed)``."""
    deleet = _deleet(name)
    out = []
    for shape, full in ((_squash(name), False), (deleet, False),
                        (_LONG_RUNS.sub(r"\1\1", deleet), False),
                        (_collapse(deleet), True)):
        if shape and all(shape != s for s, _ in out):
            out.append((shape, full))
    return out


def blocked_word(name, conf=None, session=None):
    """The banned term ``name`` contains, or ``None`` when it is allowed."""
    conf = _conf(conf, session)
    banned = terms(conf.get("blocklist"))
    if not banned:
        return None
    allowed = terms(conf.get("allowlist"))
    for shape, full in variants(name):
        # Cut allowed words out (longest first) in the same shape they appear.
        for word in sorted(allowed, key=len, reverse=True):
            shape = shape.replace(_collapse(word) if full else word, " ")
        for word in banned:
            if full and _collapse(word) != word:
                continue
            if word in shape:
                return word
    return None


def find_flagged_users(session, conf=None):
    """Real users whose current team name is blocked, as ``(user, word)``."""
    from models import User

    conf = _conf(conf, session)
    if not terms(conf.get("blocklist")):
        return []
    rows = (session.query(User)
            .filter(User.team_name.isnot(None), User.team_name != "",
                    User.telegram_id > 0)
            .order_by(User.id).all())
    flagged = []
    for user in rows:
        word = blocked_word(user.team_name, conf)
        if word:
            flagged.append((user, word))
    return flagged
