"""
Marcus data-directory resolution and the pytest isolation guard.

Issue #724: on 2026-08-09, ``pytest`` runs destroyed six live project
boards. Several components default their data paths to the cwd-relative
``./data`` — which IS the production data directory when a process (or a
test that constructs real Marcus components) runs from the repo root.
Tests wrote fixture junk into the live ``kanban.db`` and, during one
run, everything created after 2026-05-14 was deleted, with no backup.

This module is the single resolver for that default:

* **Production is unchanged.** With ``MARCUS_DATA_DIR`` unset and pytest
  not running, ``marcus_data_dir()`` returns ``Path("data")`` — byte-for-
  byte the historical default, preserving a year of working behavior.
* **Tests are isolated.** The autouse fixture in ``tests/conftest.py``
  points ``MARCUS_DATA_DIR`` at a session temp directory, so every
  default-path component writes there.
* **Bypasses fail loudly.** If a default data path is resolved under
  pytest WITHOUT the override (fixture bypassed, env stripped), the
  resolver raises instead of silently touching production.
"""

import os
from pathlib import Path

DATA_DIR_ENV = "MARCUS_DATA_DIR"


def marcus_data_dir() -> Path:
    """Resolve the Marcus data directory for default paths.

    Returns
    -------
    Path
        ``$MARCUS_DATA_DIR`` when set; otherwise the historical
        cwd-relative ``Path("data")``.

    Raises
    ------
    RuntimeError
        When called under pytest with no ``MARCUS_DATA_DIR`` override —
        resolving the production default inside a test is exactly the
        #724 data-loss hazard, so it is refused rather than honored.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError(
            "Refusing to resolve the production Marcus data directory "
            "('./data') under pytest (issue #724): tests once destroyed "
            "the live board this way. The isolation fixture in "
            "tests/conftest.py should have set MARCUS_DATA_DIR to a temp "
            "directory — if this component resolved its path before "
            "fixtures ran, pass an explicit path instead of relying on "
            "the default."
        )
    return Path("data")
