"""No test may pop a submodule out of ``sys.modules`` and leave it half-gone.

``from services import player_service`` does not consult ``sys.modules`` first.
It imports the *package*, then reads the attribute off it. A submodule popped
from ``sys.modules`` is still an attribute of its package, so the pop is only
half a cache miss and the stale module comes straight back — bound to the
previous ``Base``, and so to a different mapped class for the same table.

SQLAlchemy keys its identity map on ``(class, primary key)``, so one row loaded
through both classes becomes two independent objects. Code that reads a row
through one and writes it through the other loses the write, in silence. That
is how a tournament held in ``draft`` came back ``active`` and a test that had
been asserting a real rule quietly stopped.

Nothing about that failure is loud. There is no exception, no warning, and the
test that catches it is usually in a different file from the one that caused
it — so the only cheap defence is to refuse the shape. ``tests/_module_swap.py``
does it correctly in one place; ``monkeypatch.delattr`` is the right partner
for ``monkeypatch.delitem``.
"""

import pathlib
import re
import unittest

TESTS = pathlib.Path(__file__).resolve().parent

# A dotted name is the whole problem: a top-level module has no parent package
# for the attribute to go stale on, so popping "database" is harmless.
_DOTTED = r'["\'][\w]+(?:\.[\w]+)+["\']'
_POP = re.compile(rf'sys\.modules\.pop\(\s*({_DOTTED})')
_DELITEM = re.compile(rf'delitem\(\s*sys\.modules\s*,\s*({_DOTTED})')
# A name list fed to a pop loop — _MODULE_NAMES and friends.
_NAME_LIST = re.compile(r'_MODULE_NAMES\s*=\s*[\(\[](.*?)[\)\]]', re.S)
_LOOP_POP = re.compile(r'for name in [\w.]+:\s*\n\s*sys\.modules\.pop\(name')


def _offenders():
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        src = path.read_text()
        # Either route to a correct swap is enough.
        if "_module_swap" in src or "delattr" in src:
            continue
        names = set(_POP.findall(src)) | set(_DELITEM.findall(src))
        if _LOOP_POP.search(src):
            for block in _NAME_LIST.findall(src):
                names |= {m for m in re.findall(_DOTTED, block)}
        if names:
            yield path.name, sorted(n.strip("\"'") for n in names)


class ModuleSwapHygieneTests(unittest.TestCase):

    def test_no_test_pops_a_submodule_without_clearing_the_attribute(self):
        offenders = list(_offenders())
        self.assertEqual(
            [], offenders,
            "these files pop a dotted module name but never clear it from its "
            "parent package, so the import that follows can hand back the "
            "stale module:\n"
            + "\n".join(f"  {name}: {', '.join(mods)}" for name, mods in offenders)
            + "\n\nUse tests/_module_swap.py (save/unload/restore), or pair "
              "every monkeypatch.delitem with a monkeypatch.delattr.")

    def test_the_helper_actually_clears_the_parent_attribute(self):
        """The rule is only worth enforcing if the helper obeys it."""
        import sys

        import _module_swap

        import services.config_service  # noqa: F401  (something real and cheap)
        self.assertTrue(hasattr(sys.modules["services"], "config_service"))

        saved = _module_swap.save(["services.config_service"])
        _module_swap.unload(["services.config_service"])
        try:
            self.assertNotIn("services.config_service", sys.modules)
            self.assertFalse(
                hasattr(sys.modules["services"], "config_service"),
                "unload left the attribute behind — the next "
                "`from services import config_service` would get the stale one")
        finally:
            _module_swap.restore(saved)

        self.assertIn("services.config_service", sys.modules)
        self.assertIs(sys.modules["services.config_service"],
                      getattr(sys.modules["services"], "config_service"),
                      "restore put back sys.modules and the package attribute "
                      "out of step with each other")

    def test_restore_puts_back_a_name_that_was_not_there_before(self):
        """Saving a module that does not exist yet must not invent one."""
        import sys

        import _module_swap

        sys.modules.pop("services.config_service", None)
        saved = _module_swap.save(["services.config_service"])
        self.assertIsNone(saved["services.config_service"])
        import services.config_service  # noqa: F401  (now it does exist)
        _module_swap.restore(saved)
        self.assertNotIn("services.config_service", sys.modules,
                         "restore should have taken it back out again")


if __name__ == "__main__":
    unittest.main()
