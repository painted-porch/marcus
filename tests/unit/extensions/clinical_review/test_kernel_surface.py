"""
The clinical-review extension imports no kernel code at all (ADR-0013 K1/K2).

The citation verifier keeps one door, ``kernel.py``, for the MCP client. This
extension needs none: it talks to Marcus through ``marcus_toolkit.client``
and reads the files a run leaves behind. The guard walks every module in the
package and fails on any ``src.`` import, and names a stray module beside
``pyproject.toml`` as a packaging mistake (issue #913).
"""

from __future__ import annotations

import ast
from pathlib import Path

import clinical_review
import pytest

pytestmark = pytest.mark.unit

PACKAGE_DIR = Path(clinical_review.__file__).parent
DIST_DIR = PACKAGE_DIR.parent


def _kernel_imports(path: Path) -> set[str]:
    """Return every ``src.*`` module a file imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("src")
        ):
            found.add(node.module)
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("src"))
    return found


class TestNoDoorToTheKernel:
    """Zero kernel imports, and nothing stray beside the package."""

    def test_no_module_imports_the_kernel(self) -> None:
        """Every module is clean; an offender is named."""
        offenders = {
            p.name: _kernel_imports(p)
            for p in PACKAGE_DIR.glob("*.py")
            if _kernel_imports(p)
        }
        assert offenders == {}

    def test_every_module_imports_only_the_toolkit_for_marcus_access(self) -> None:
        """The only Marcus-facing dependency is marcus_toolkit."""
        marcus_facing = set()
        for path in PACKAGE_DIR.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and "marcus" in node.module
                ):
                    marcus_facing.add(node.module.split(".")[0])
        assert marcus_facing <= {"marcus_toolkit"}

    def test_no_stray_module_beside_the_package(self) -> None:
        """A ``.py`` beside ``pyproject.toml`` is out of view; refuse it."""
        stray = sorted(p.name for p in DIST_DIR.glob("*.py"))
        assert stray == []
