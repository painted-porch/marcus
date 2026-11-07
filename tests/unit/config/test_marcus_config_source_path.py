"""
Unit tests for ``MarcusConfig.source_path`` (issue #905).

A server start must be able to say which configuration file it actually
loaded, before anything calls out.
``MarcusConfig.from_file`` therefore records the path it read, and leaves the
field ``None`` when it fell back to built-in defaults because no file existed.
"""

import json
from pathlib import Path

import pytest

from src.config.marcus_config import MarcusConfig

pytestmark = pytest.mark.unit


class TestSourcePath:
    """``from_file`` records where the configuration came from."""

    def test_from_file_records_the_path_it_loaded(self, tmp_path: Path) -> None:
        """Loading an existing file stores that file's absolute path."""
        config_file = tmp_path / "config_marcus.json"
        config_file.write_text(
            json.dumps({"ai": {"enabled": False, "anthropic_api_key": ""}})
        )

        cfg = MarcusConfig.from_file(str(config_file))

        assert cfg.source_path == str(config_file.absolute())

    def test_defaults_record_no_source_path(self, tmp_path: Path) -> None:
        """A missing file yields defaults and no source path."""
        cfg = MarcusConfig.from_file(str(tmp_path / "does-not-exist.json"))

        assert cfg.source_path is None

    def test_a_freshly_constructed_config_has_no_source_path(self) -> None:
        """Constructing the dataclass directly is not loading a file."""
        assert MarcusConfig().source_path is None
