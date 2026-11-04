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
from typing import Optional

DATA_DIR_ENV = "MARCUS_DATA_DIR"


def _override_data_dir() -> Optional[Path]:
    """Read ``MARCUS_DATA_DIR``, normalized, or ``None`` when unset.

    Every store has to agree on what the override means. A literal tilde
    is plausible in a shell profile or a launchd plist, where nothing
    expands it, and a relative value is legitimate. Expanding and
    absolutizing in one place is what keeps one instance from opening two
    history databases: before this, ``resolve_data_path`` expanded the
    tilde while ``marcus_state_dir`` and ``cost_store_path`` did not, so
    the roster and the creation-path writers used a directory literally
    named ``~`` inside the working directory (Codex P2 on PR #801).

    Returns
    -------
    Optional[Path]
        The override as an absolute path, or ``None`` when it is unset or
        empty.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if not override:
        return None
    # absolute(), not resolve(): an already-absolute value must come back
    # byte-identical, because callers compare it against what they
    # configured. resolve() would rewrite symlinks.
    return Path(override).expanduser().absolute()


def cost_store_path() -> Path:
    """Resolve the cost store's path.

    Unlike the other stores, the cost store is deliberately **global** in
    production: the cost dashboard reads one database across projects, so
    moving it per project would orphan that history.

    But an operator who sets ``MARCUS_DATA_DIR`` is asking for isolation,
    and a scratch instance writing into the real spend history is exactly
    what that request is meant to prevent (issue #792). So the override
    redirects it and nothing else does.

    Returns
    -------
    Path
        ``$MARCUS_DATA_DIR/costs.db`` when the override is set, otherwise
        ``~/.marcus/costs.db``.
    """
    override = _override_data_dir()
    if override:
        return override / "costs.db"
    return Path.home() / ".marcus" / "costs.db"


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
    override = _override_data_dir()
    if override:
        return override
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


def _repo_root() -> Path:
    """Locate the Marcus checkout this module was loaded from.

    Returns
    -------
    Path
        The repository root, two levels above ``src/core``. Every store
        that historically built a checkout-relative default computed
        exactly this value from its own ``__file__``.
    """
    return Path(__file__).parent.parent.parent


def marcus_state_dir() -> Path:
    """Resolve the data directory for a **checkout-relative** store.

    Two different historical defaults exist, and treating them alike is a
    regression. The board and ``config.data_dir`` defaulted to the
    cwd-relative ``./data``, so :func:`marcus_data_dir` is exact parity
    for them. The worker roster, the project history and the
    project-creation writers instead built ``<checkout>/data`` from
    ``Path(__file__)`` on purpose, so that they worked regardless of the
    working directory.

    Routing the second group through :func:`marcus_data_dir` moved them to
    ``<cwd>/data`` whenever Marcus was started outside the checkout. An
    existing deployment restarted from another directory would silently
    stop rehydrating its registered workers (ADR-0012 O1) and abandon its
    history, which is a worse failure than the one #792 set out to fix.

    Returns
    -------
    Path
        ``$MARCUS_DATA_DIR`` when set, otherwise ``<checkout>/data``.

    Raises
    ------
    RuntimeError
        When called under pytest with no override, for the same reason
        :func:`marcus_data_dir` refuses: resolving the production
        directory inside a test is the #724 data-loss hazard.
    """
    override = _override_data_dir()
    if override:
        return override
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError(
            "Refusing to resolve the production Marcus data directory "
            "('<checkout>/data') under pytest (issue #724): tests once "
            "destroyed the live board this way. The isolation fixture in "
            "tests/conftest.py should have set MARCUS_DATA_DIR to a temp "
            "directory — if this component resolved its path before "
            "fixtures ran, pass an explicit path instead of relying on "
            "the default."
        )
    return _repo_root() / "data"


def _relative_to_data_dir(configured: str) -> Path:
    """Resolve a relative configured path against the data directory.

    Parameters
    ----------
    configured : str
        A relative path, such as ``"data/kanban.db"`` or ``"./data"``.

    Returns
    -------
    Path
        The path inside the data directory. A leading ``data/`` segment
        is stripped, so the historical ``"./data/kanban.db"`` and the
        config default ``"./data"`` land in the data directory itself
        rather than a nested copy of it.
    """
    # absolute(), not resolve(): an already-absolute data directory must
    # come back byte-identical. resolve() rewrites symlinks, so a macOS
    # /var/... directory became /private/var/..., and a caller comparing
    # the result against the configured value saw two strings for one
    # file.
    base = marcus_data_dir().expanduser().absolute()
    parts = [part for part in Path(configured).expanduser().parts if part != "."]
    if parts and parts[0] == "data":
        parts = parts[1:]
    return base.joinpath(*parts) if parts else base


def resolve_data_subdir(configured: str) -> Path:
    """Resolve a configured **directory** against the data directory.

    ``MarcusConfig.validate`` creates ``data_dir`` and ``cache_dir`` to
    check they are writable, and both default to cwd-relative values. It
    used them literally, so a server started outside the checkout left an
    empty ``data/`` and ``cache/`` wherever it happened to start, even
    once every writer had been redirected (issue #792).

    Parameters
    ----------
    configured : str
        A configured directory, absolute or relative.

    Returns
    -------
    Path
        The directory as given when absolute, otherwise resolved inside
        the data directory. A value of ``"./data"`` is the data directory
        itself rather than a nested copy of it.
    """
    if Path(configured).expanduser().is_absolute():
        return Path(configured).expanduser()
    return _relative_to_data_dir(configured)


def resolve_data_path(configured: Optional[str], default_name: str) -> Path:
    """Resolve one data path against the data directory, never the cwd.

    This is the single rule every store follows. A configured path is
    honoured, and a **relative** one resolves against the data directory
    rather than the process's working directory, so the same config names
    the same file wherever Marcus was started from.

    Three separate notions of "the data directory" is what let issue #792
    survive its first fix: the board resolver had this rule, the
    project-creation path built ``<checkout>/data/marcus.db`` from the
    module's own location, and ``config.data_dir`` defaulted to the
    cwd-relative ``"./data"`` and never consulted the override at all. An
    isolated instance therefore wrote its history into the production
    database and left a stray ``data/`` wherever it was started.

    Parameters
    ----------
    configured : Optional[str]
        The configured path, absolute or relative. ``None`` or empty
        means use ``default_name`` inside the data directory.
    default_name : str
        File or directory name to use when nothing is configured.

    Returns
    -------
    Path
        An absolute path whenever the data directory is absolute, which
        it is whenever ``MARCUS_DATA_DIR`` is set.
    """
    if not configured:
        return marcus_data_dir().expanduser().absolute() / default_name
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    resolved = _relative_to_data_dir(configured)
    # A value of exactly "data" names the directory, so the caller's
    # default name still has to be applied.
    if resolved == marcus_data_dir().expanduser().absolute():
        return resolved / default_name
    return resolved


def marcus_db_path(configured: Optional[str] = None) -> Path:
    """Resolve the Marcus history database (``marcus.db``).

    Accepts either a path to the database or the directory it lives in,
    because ``config.data_dir`` is a directory and several callers have
    only that.

    Parameters
    ----------
    configured : Optional[str]
        A configured database path or data directory, if the caller has
        one. ``None`` means use the data directory.

    Returns
    -------
    Path
        The history database's path, inside the data directory unless an
        absolute path was configured.
    """
    if not configured:
        # The creation-path writers and the analysis classes all built
        # <checkout>/data/marcus.db from their own __file__, so parity
        # for them is the checkout, not the working directory.
        return marcus_state_dir() / "marcus.db"
    candidate = Path(configured).expanduser()
    if candidate.suffix == ".db":
        return resolve_data_path(configured, "marcus.db")
    # A directory-style value ("./data", "/srv/marcus-data") names where
    # the database lives, not the database itself.
    return resolve_data_subdir(configured) / "marcus.db"
