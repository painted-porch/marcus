"""Unit tests for AdvancedPRDParser helpers (#683 Cause 1).

Covers ``_filter_requirements_by_size``: the capacity filter must cap by
the complexity TIER (not team size) and must never drop a CORE feature
(one the caller marks as serving an in-scope user outcome).
"""

import pytest

from src.ai.advanced.prd.advanced_parser import AdvancedPRDParser

pytestmark = pytest.mark.unit


class TestFilterRequirementsBySizeCause1:
    """#683 Cause 1: cap by complexity tier (not team size) and never drop a
    core (outcome-bearing) feature."""

    def _parser(self) -> AdvancedPRDParser:
        # _filter_requirements_by_size is a pure method (uses only its args
        # + _detect_prompt_specificity, no __init__ state). Bypass __init__
        # via __new__ so the test needs no LLM provider / API key — keeps it
        # a true unit test and CI-safe with no config_marcus.json.
        return AdvancedPRDParser.__new__(AdvancedPRDParser)

    def _reqs(self, n: int):
        return [
            {"id": f"f{i}", "name": f"Feature {i}", "description": "d"}
            for i in range(n)
        ]

    def test_medium_keeps_all_when_under_tier_cap(self) -> None:
        """6 features in a medium project are all kept (tier cap 15), not cut
        to 3 by team_size — the snake-run regression."""
        parser = self._parser()
        out = parser._filter_requirements_by_size(
            self._reqs(6), "medium", 3, "build a snake game"
        )
        assert len(out) == 6

    def test_over_cap_trims_non_core_keeps_core(self) -> None:
        """20 features, medium (cap 15): core features survive; scope-creep is
        trimmed to fit, never a core feature."""
        parser = self._parser()
        protected = {"f17", "f18", "f19"}  # core features late in the list
        out = parser._filter_requirements_by_size(
            self._reqs(20), "medium", 3, "build something", protected
        )
        kept = {r["id"] for r in out}
        assert len(out) == 15
        assert protected.issubset(kept), "core features must never be dropped"

    def test_core_exceeding_cap_all_kept(self) -> None:
        """If core features alone exceed the cap, all are kept (scope floor)."""
        parser = self._parser()
        protected = {f"f{i}" for i in range(17)}  # 17 core > cap 15
        out = parser._filter_requirements_by_size(
            self._reqs(18), "medium", 3, "build something", protected
        )
        kept = {r["id"] for r in out}
        assert protected.issubset(kept)
        assert len(out) >= 17

    def test_preserves_original_order(self) -> None:
        """Kept features retain their original order."""
        parser = self._parser()
        protected = {"f10"}
        out = parser._filter_requirements_by_size(
            self._reqs(20), "medium", 3, "build something", protected
        )
        ids = [r["id"] for r in out]
        assert ids == sorted(ids, key=lambda x: int(x[1:]))

    def test_all_ids_protected_keeps_everything_over_cap(self) -> None:
        """Codex P2 (#688): the mapping-failure fallback protects ALL ids, so
        an over-cap list keeps every requirement — nothing is dropped by
        position. This is the property the except-branch fallback relies on."""
        parser = self._parser()
        reqs = self._reqs(25)  # 25 > medium cap 15
        protected = {r["id"] for r in reqs}  # fallback = protect everything
        out = parser._filter_requirements_by_size(
            reqs, "medium", 3, "build something", protected
        )
        assert len(out) == 25, "all-ids fallback must drop nothing"
