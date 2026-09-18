"""Swap a handful of modules out for a run against a temporary database.

Many suites here need ``database``/``models`` re-imported against their own
SQLite file. The obvious way — pop the names out of ``sys.modules``, import,
put the originals back — is subtly wrong, and the way it fails is worth
spelling out because it cost this suite two silent failures.

``from services import tournament_service`` does **not** consult
``sys.modules`` first. It imports the *package*, then tries
``getattr(services, "tournament_service")``. A submodule that was popped from
``sys.modules`` is still an attribute of its package, so that getattr succeeds
and hands back the stale module — the one bound to the previous database, and
holding the previous ``Base`` and therefore the previous mapped classes.

Two mapped classes for one table is the nasty part. SQLAlchemy keys its
identity map on ``(class, primary key)``, so the same row loaded through the
old class and the new one is two independent objects. A service that reads a
row through one and writes it through the other silently loses the write —
which is exactly how a tournament held in ``draft`` came back ``active``.

So: pop the name **and** delete the attribute from the parent package, and put
both back afterwards.
"""

import sys


def unload(names):
    """Make each dotted name a genuine cache miss for the next import."""
    for name in names:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            try:
                delattr(package, child)
            except AttributeError:
                pass


def save(names):
    """The modules currently under ``names``, for a later :func:`restore`."""
    return {name: sys.modules.get(name) for name in names}


def restore(saved):
    """Put back what :func:`save` recorded, package attributes included."""
    for name, module in saved.items():
        if module is None:
            unload([name])
            continue
        sys.modules[name] = module
        parent, _, child = name.rpartition(".")
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            setattr(package, child, module)
