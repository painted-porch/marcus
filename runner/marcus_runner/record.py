"""
The run record: one directory per run, every default that applied, every file.

The Minecraft runs settled the layout: ``runs/<stamp>/`` holding
``run-config.txt`` (one ``key: value`` line per setting), ``marcus-version.txt``,
the logs, the JSONL event streams, and at the end a copy of the board and the
agents' state. Every figure in a write-up is read from this directory.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional


def stamp_now() -> str:
    """Return a run stamp: ``YYYYMMDDTHHMMSS`` in local time, as Minecraft does."""
    return time.strftime("%Y%m%dT%H%M%S")


class RunRecord:
    """
    The directory a run writes into.

    Parameters
    ----------
    root : Path
        The ``runs/`` directory.
    stamp : Optional[str]
        The run's stamp; now by default.
    """

    def __init__(self, root: Path, stamp: Optional[str] = None) -> None:
        self.stamp = stamp or stamp_now()
        self.dir = root / self.stamp
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        """Return a file path inside the record."""
        return self.dir / name

    def config_line(self, key: str, value: Any) -> None:
        """Append one ``key: value`` line to ``run-config.txt``."""
        with self.path("run-config.txt").open("a") as handle:
            handle.write(f"{key + ':':<20}{value}\n")

    def config(self) -> Dict[str, str]:
        """Read ``run-config.txt`` back as a mapping (last value wins)."""
        out: Dict[str, str] = {}
        file = self.path("run-config.txt")
        if not file.is_file():
            return out
        for line in file.read_text().splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip()
        return out

    def write(self, name: str, text: str) -> Path:
        """Write a text file into the record."""
        target = self.path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def append_jsonl(self, name: str, row: Dict[str, Any]) -> None:
        """Append one row to a JSONL stream, flushed at once."""
        with self.path(name).open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()

    def copy_in(self, source: Path, name: Optional[str] = None) -> Optional[Path]:
        """Copy a file or directory into the record; ``None`` if it is missing."""
        if not source.exists():
            return None
        target = self.path(name or source.name)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        return target
