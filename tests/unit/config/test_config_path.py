"""Unit tests for the shared config-path resolver (issue #891).

``resolve_config_candidates`` is the single place that decides where
``MARCUS_CONFIG`` sits in a config-loading path's candidate list: always
first, when set. Every loader this issue found disagreeing
(``MarcusConfig.get_config()``, the legacy ``ConfigLoader``,
``KanbanClient._load_config``, ``WorkspaceManager._load_default_config``,
``Settings``) now calls this instead of building its own ordering.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.config.config_path import resolve_config_candidates

pytestmark = pytest.mark.unit


def test_env_override_comes_first_when_set() -> None:
    with patch.dict("os.environ", {"MARCUS_CONFIG": "/override/config.json"}):
        candidates = resolve_config_candidates("fallback-a.json", "fallback-b.json")

    assert candidates == [
        Path("/override/config.json"),
        Path("fallback-a.json"),
        Path("fallback-b.json"),
    ]


def test_fallbacks_alone_when_env_unset() -> None:
    with patch.dict("os.environ", {}, clear=True):
        candidates = resolve_config_candidates("fallback-a.json", "fallback-b.json")

    assert candidates == [Path("fallback-a.json"), Path("fallback-b.json")]


def test_empty_env_value_is_treated_as_unset() -> None:
    """An empty string is falsy, matching every caller's own prior
    ``if override:`` guard -- an empty MARCUS_CONFIG must not become
    a literal candidate path (``Path("")`` resolves to ``"."``)."""
    with patch.dict("os.environ", {"MARCUS_CONFIG": ""}):
        candidates = resolve_config_candidates("fallback-a.json")

    assert candidates == [Path("fallback-a.json")]


def test_no_fallbacks_and_no_env_returns_empty_list() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert resolve_config_candidates() == []


def test_accepts_path_objects_as_fallbacks_too() -> None:
    with patch.dict("os.environ", {}, clear=True):
        candidates = resolve_config_candidates(Path("a.json"), Path("b.json"))

    assert candidates == [Path("a.json"), Path("b.json")]
