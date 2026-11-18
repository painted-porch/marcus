"""
Shared fixtures for the clinical-review unit tests.

The adversarial module is deliberately not in the repository: Barrit et al.
withhold theirs for safety reasons, and the study's own rebuild is read at run
time from a private file. Tests point that loader at a placeholder that names
the two fields the template must carry and contains no instruction at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PLACEHOLDER = (
    "[Adversarial module withheld from the repository for tests.]\n"
    "Target: {harmful}\n"
    "Counterpoint: {correct}\n"
)


@pytest.fixture(autouse=True)
def placeholder_adversarial_module(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point the private-module loader at a harmless placeholder file."""
    path = tmp_path_factory.mktemp("private") / "adversarial_module.txt"
    path.write_text(PLACEHOLDER)
    monkeypatch.setenv("CLINICAL_REVIEW_ADVERSARIAL_MODULE", str(path))
    return path
