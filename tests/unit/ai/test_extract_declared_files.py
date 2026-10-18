"""
Unit tests for ``_extract_declared_files`` (#206 MVP, Phase 2).

This helper is called inside ``decompose_by_contract`` after the
LLM-produced task list is parsed. For each contract task it produces
the list of file paths the task intends to write to. The list is
persisted into ``task.source_context["declared_files"]`` and later
consulted by ``request_next_task`` (Phase 3) to skip tasks whose
files are currently held by another in-progress task.

MVP scope (per design doc open question #1): **conservative** —
the only declared write target is the task's own ``contract_file``.
Contract artifacts (foundation files) and other inferred
implementation files are NOT declared, because:

- Contract artifacts are READ by every implementation task; the
  registry only locks writes (reads are free).
- Inferred impl files would risk over-blocking (false-positive
  conflicts) before we have empirical data on contention rates.
- Bright-line: ``contract_file`` is already part of the contract
  responsibility (a WHAT, not a HOW), so declaring it adds no new
  constraint on the agent's implementation choices.

If a task has no ``contract_file`` (e.g., legacy feature-based
tasks, foundation pre-fork tasks), the helper returns an empty list
and the task is unaffected by the lock filter.
"""

import pytest

from src.ai.advanced.prd.advanced_parser import (
    _extract_declared_files,
    _normalize_declared_file_path,
)

pytestmark = pytest.mark.unit


class TestExtractDeclaredFiles:
    """``_extract_declared_files`` is the contract-first declarer."""

    def test_returns_contract_file_when_present(self) -> None:
        """A contract task with a contract_file declares that file."""
        files = _extract_declared_files(
            responsibility="implements GameEngine interface",
            contract_file="src/types/engine.ts",
            contract_artifacts={},
        )
        assert files == ["src/types/engine.ts"]

    def test_returns_empty_list_when_contract_file_missing(self) -> None:
        """A task without a contract_file declares nothing (MVP conservative).

        These tasks pass through ``request_next_task`` unfiltered —
        the registry returns False for ``any_held([])``.
        """
        files = _extract_declared_files(
            responsibility="vague description with no file",
            contract_file="",
            contract_artifacts={},
        )
        assert files == []

    def test_returns_empty_list_when_responsibility_missing(self) -> None:
        """Defensive: no responsibility AND no contract_file -> empty."""
        files = _extract_declared_files(
            responsibility="",
            contract_file="",
            contract_artifacts={},
        )
        assert files == []

    def test_does_not_include_contract_artifacts(self) -> None:
        """Contract artifacts are READ files — never in declared_files.

        The registry only locks writes; agents read freely from the
        foundation contract artifacts. Including them would block
        every implementation task on every other one because they
        all read the same foundation files.
        """
        files = _extract_declared_files(
            responsibility="implements GameEngine",
            contract_file="src/types/engine.ts",
            contract_artifacts={
                "domain_contract": {
                    "file_path": "src/types/engine.ts",
                    "schema_file": "src/types/schema.ts",
                },
                "api_contract": {
                    "file_path": "src/types/api.ts",
                },
            },
        )
        # Only the task's own contract_file is declared.
        assert files == ["src/types/engine.ts"]

    def test_handles_none_contract_file(self) -> None:
        """Defensive: contract_file=None (not empty string) -> empty list.

        Callers in the parser coerce to ``str(raw or "")`` so this
        normally cannot happen, but the helper should be robust.
        """
        files = _extract_declared_files(
            responsibility="anything",
            contract_file=None,
            contract_artifacts={},
        )
        assert files == []

    def test_handles_none_contract_artifacts(self) -> None:
        """``contract_artifacts=None`` must not raise."""
        files = _extract_declared_files(
            responsibility="implements GameEngine",
            contract_file="src/types/engine.ts",
            contract_artifacts=None,
        )
        assert files == ["src/types/engine.ts"]

    def test_strips_whitespace_in_contract_file(self) -> None:
        """A whitespace-only contract_file is treated as missing."""
        files = _extract_declared_files(
            responsibility="anything",
            contract_file="   ",
            contract_artifacts={},
        )
        assert files == []

    def test_normalizes_contract_file_value(self) -> None:
        """A contract_file with surrounding whitespace is normalized."""
        files = _extract_declared_files(
            responsibility="anything",
            contract_file="  src/types/engine.ts  ",
            contract_artifacts={},
        )
        assert files == ["src/types/engine.ts"]


class TestNormalizeDeclaredFilePath:
    """``_normalize_declared_file_path`` keeps surface-variant paths in sync.

    Without normalization, two tasks declaring the same file under
    different forms (``./src/foo.py`` vs ``src/foo.py`` vs
    ``src\\foo.py``) would miss each other in the registry and skip
    the conflict check — exactly the foot-gun Kaia's #658 review
    flagged.
    """

    def test_strips_surrounding_whitespace(self) -> None:
        """Tab / space wrappers are stripped before path normalization."""
        assert _normalize_declared_file_path("  src/foo.py  ") == "src/foo.py"

    def test_resolves_leading_dot_slash(self) -> None:
        """``./src/foo.py`` and ``src/foo.py`` canonicalize to one form."""
        assert _normalize_declared_file_path("./src/foo.py") == "src/foo.py"
        assert _normalize_declared_file_path("src/foo.py") == "src/foo.py"

    def test_collapses_embedded_dot_segments(self) -> None:
        """``src/./foo.py`` collapses just like ``src/foo.py``."""
        assert _normalize_declared_file_path("src/./foo.py") == "src/foo.py"

    def test_converts_backslashes_to_posix(self) -> None:
        """Windows-style paths normalize to POSIX form for cross-platform parity."""
        assert _normalize_declared_file_path("src\\foo.py") == "src/foo.py"
        assert _normalize_declared_file_path("src\\sub\\foo.py") == "src/sub/foo.py"

    def test_empty_input_returns_empty(self) -> None:
        """A whitespace-only path normalizes to empty (so the caller drops it)."""
        assert _normalize_declared_file_path("") == ""
        assert _normalize_declared_file_path("   ") == ""

    def test_extract_treats_variants_as_one(self) -> None:
        """End-to-end: variants produce the same declared_files list.

        This is the property the registry depends on — without it,
        the conflict scan would miss because the dictionary keys
        wouldn't match.
        """
        a = _extract_declared_files(
            responsibility="x",
            contract_file="src/foo.py",
            contract_artifacts={},
        )
        b = _extract_declared_files(
            responsibility="x",
            contract_file="./src/foo.py",
            contract_artifacts={},
        )
        c = _extract_declared_files(
            responsibility="x",
            contract_file="src\\foo.py",
            contract_artifacts={},
        )
        assert a == b == c == ["src/foo.py"]
