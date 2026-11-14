"""
The citation verifier's kernel surface is one module and a fixed list (ADR-0013 K1/K2).

The extension may read the kernel only through ``citation_verifier.kernel``.
If any other module imports ``src.`` the extension has grown a side door;
if ``kernel`` imports more than the declared list, the contract in
``docs/architecture/extension-contract.md`` is out of date.

What the guard covers, and what it deliberately does not (issue #913).
It walks every ``.py`` file inside the *package* directory,
``extensions/citation_verifier/citation_verifier/``, because those are the
modules importable as ``citation_verifier.<name>``; a kernel import in any of
them is a side door. A file dropped one level up, beside the distribution's
``pyproject.toml``, is outside its view on purpose: it is not part of the
package, cannot be reached as ``citation_verifier.something``, and so cannot
be a door into the package. ``test_no_stray_module_beside_the_package``
names such a file as a packaging mistake instead of silently ignoring it.
"""

import ast
import importlib
import pkgutil
from pathlib import Path
from typing import Dict, Set

import citation_verifier
import pytest
from citation_verifier import kernel

pytestmark = pytest.mark.unit

PACKAGE_DIR = Path(citation_verifier.__file__).parent
#: The whole surface since the MCP-only write path (ADR-0013 K2 decided
#: 2026-11-13; #911 closed the seed-spec gaps, #910 made the reply reader
#: public): the MCP client and its public reply reader. Nothing private.
DECLARED: Dict[str, Set[str]] = {
    "src.worker.inspector": {"Inspector", "extract_text_from_result"},
}
#: The distribution directory, holding ``pyproject.toml`` and the package.
DIST_DIR = PACKAGE_DIR.parent


def _kernel_imports(path: Path) -> Dict[str, Set[str]]:
    """Map each imported ``src.*`` module to the names taken from it, for one file."""
    found: Dict[str, Set[str]] = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("src")
        ):
            found.setdefault(node.module, set()).update(a.name for a in node.names)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src"):
                    found.setdefault(alias.name, set()).add("*")
    return found


class TestOneDoorToTheKernel:
    """Every kernel import goes through ``kernel.py``, and only the declared ones."""

    def test_no_module_but_kernel_imports_the_kernel(self) -> None:
        """A second import site is a side door; the guard names it."""
        offenders = {
            p.name: _kernel_imports(p)
            for p in PACKAGE_DIR.glob("*.py")
            if p.name != "kernel.py" and _kernel_imports(p)
        }
        assert offenders == {}

    def test_kernel_imports_exactly_the_declared_surface(self) -> None:
        """The contract document and the code agree on the list."""
        assert _kernel_imports(PACKAGE_DIR / "kernel.py") == DECLARED

    def test_kernel_exports_what_the_modules_use(self) -> None:
        """Every name in ``__all__`` resolves."""
        for name in kernel.__all__:
            assert getattr(kernel, name) is not None

    def test_every_module_imports(self) -> None:
        """Packaging smoke test: each module loads under its new name."""
        for info in pkgutil.iter_modules([str(PACKAGE_DIR)]):
            importlib.import_module(f"citation_verifier.{info.name}")

    def test_nothing_on_the_surface_is_private(self) -> None:
        """Issue #910: the contract names public API only."""
        assert not any(
            name.startswith("_") for names in DECLARED.values() for name in names
        )

    def test_no_stray_module_beside_the_package(self) -> None:
        """Issue #913: a ``.py`` beside ``pyproject.toml`` is a packaging mistake.

        Such a file is outside the import guard's view by design (see the
        module docstring); this names it rather than letting it sit there
        looking like an unguarded hole.
        """
        strays = sorted(p.name for p in DIST_DIR.glob("*.py"))
        assert strays == []
