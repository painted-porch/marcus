"""
Unit tests for ``_resolve_project_info_path`` (project-creation handshake).

The experiment runner waits for ``<experiment_dir>/project_info.json`` as its
go-signal and instructs the project-creator agent to pass
``project_info_path`` in the ``create_project`` options
(``spawn_agents.py`` injects it). A lower-fidelity agent (e.g. Haiku) can
drop that one field from a long options object, leaving Marcus with no path
-- so it silently skips the write, the runner times out at 600s, and the run
never spawns a work agent.

The fix derives the path from ``project_root`` (which survives reliably)
when ``project_info_path`` is absent, so a dropped field can no longer
strand the run. The runner sets ``project_root == <experiment_dir>/
implementation``, so the info file sits one level up.
"""

from pathlib import Path

import pytest

from src.marcus_mcp.tools.nlp import _resolve_project_info_path

pytestmark = pytest.mark.unit


class TestResolveProjectInfoPath:
    """Resolve the project_info.json write path independent of agent fidelity."""

    def test_explicit_path_is_used_verbatim(self) -> None:
        """An explicit ``project_info_path`` is returned unchanged."""
        opts = {
            "project_info_path": "/runs/x/project_info.json",
            "project_root": "/runs/x/implementation",
        }
        assert _resolve_project_info_path(opts) == "/runs/x/project_info.json"

    def test_derived_from_project_root_when_path_missing(self) -> None:
        """With no explicit path, derive it from ``project_root`` one level up."""
        opts = {"project_root": "/Users/me/experiments/test92/implementation"}
        assert _resolve_project_info_path(opts) == str(
            Path("/Users/me/experiments/test92") / "project_info.json"
        )

    def test_explicit_path_takes_precedence_over_project_root(self) -> None:
        """When both are present, the explicit path wins."""
        opts = {
            "project_info_path": "/custom/pi.json",
            "project_root": "/runs/x/implementation",
        }
        assert _resolve_project_info_path(opts) == "/custom/pi.json"

    def test_none_when_neither_present(self) -> None:
        """No path and no project_root yields ``None`` (caller skips the write)."""
        assert _resolve_project_info_path({}) is None

    def test_none_when_both_empty_strings(self) -> None:
        """Falsy values are treated as absent."""
        assert (
            _resolve_project_info_path({"project_info_path": "", "project_root": ""})
            is None
        )
