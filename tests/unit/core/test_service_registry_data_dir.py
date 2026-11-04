"""
Unit tests for the service registry naming each instance's data directory.

The service registry at ``~/.marcus/services`` is how a tool finds the
Marcus servers running on a machine. Each server writes one JSON file
there describing itself.

Issue #792 made ``MARCUS_DATA_DIR`` isolate everything Marcus writes, so
two servers can now run side by side with entirely separate state. The
registry did not record which data directory an instance was using, so
two sqlite instances were indistinguishable: same ``provider``, same
``log_dir`` when both ran from one checkout, and nothing naming the board
they were actually serving. A reader could see that two servers existed
but not which was which.

The registry is machine-wide on purpose, because a tool looking for
Marcus has no reason to know about ``MARCUS_DATA_DIR``. Naming the data
directory in the entry is what makes that shared registry usable: every
instance is self-describing, and the reader does not have to guess.

Every test here redirects ``HOME``. ``MarcusServiceRegistry.__init__``
resolves its directory from the home directory and ``mkdir``s it, and
``discover_services`` is a classmethod that builds its own registry, so
overriding an instance attribute isolates neither. Without the
redirection these tests read and create state under the real
``~/.marcus/services``. That is the same hazard as issue #724, one
directory over. It also matters more than it looks: ``discover_services``
prunes entries whose process is gone, so a test that calls it against the
real directory silently deletes another tool's view of the machine. On the
machine this was written on, one such call took the directory from 938
entries to 11.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from src.core.service_registry import MarcusServiceRegistry

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the registry at a temporary home directory.

    Parameters
    ----------
    tmp_path : Path
        Pytest's per-test temporary directory.
    monkeypatch : pytest.MonkeyPatch
        Used to redirect ``HOME``.

    Returns
    -------
    Path
        The ``services`` directory the registry will resolve to.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".marcus" / "services"


def _register(
    instance_id: str,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
    **extra: Any,
) -> Dict[str, Any]:
    """Register one instance with ``MARCUS_DATA_DIR`` set to ``override``.

    Parameters
    ----------
    instance_id : str
        Identifier for this instance.
    override : str
        Value for ``MARCUS_DATA_DIR`` while registering. Named this
        rather than ``data_dir`` so a test can also pass an explicit
        ``data_dir`` through ``extra``.
    monkeypatch : pytest.MonkeyPatch
        Used to set the override for the duration of the call.
    **extra : Any
        Passed through to ``register_service``.

    Returns
    -------
    Dict[str, Any]
        The entry that was written.
    """
    monkeypatch.setenv("MARCUS_DATA_DIR", override)
    registry = MarcusServiceRegistry(instance_id=instance_id)
    return registry.register_service(
        mcp_command="python -m src.marcus_mcp.server --http",
        log_dir="/Users/shared/dev/marcus/logs",
        provider="sqlite",
        **extra,
    )


class TestTheEntryNamesItsDataDirectory:
    """An entry has to say which state the instance is serving."""

    def test_the_data_directory_is_recorded(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The field exists and holds the instance's data directory."""
        info = _register("marcus_a", str(tmp_path / "state"), monkeypatch)

        assert info["data_dir"] == str(tmp_path / "state")

    def test_it_is_absolute_even_when_the_override_is_relative(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative override is useless to a reader in another process.

        ``MARCUS_DATA_DIR=scratch/state`` means something only relative
        to the directory that server was started in, which the reader
        does not share.
        """
        monkeypatch.chdir(tmp_path)

        info = _register("marcus_rel", "scratch/state", monkeypatch)

        assert Path(info["data_dir"]).is_absolute()
        assert info["data_dir"] == str(tmp_path / "scratch" / "state")

    def test_a_literal_tilde_is_expanded(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader must not have to expand the writer's home directory."""
        info = _register("marcus_tilde", "~/isolated-marcus", monkeypatch)

        assert info["data_dir"] == str(tmp_path / "home" / "isolated-marcus")

    def test_without_an_override_the_checkout_is_named(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single-instance deployment still gets a usable value.

        The field must never be empty or ``None``, or a reader has to
        special-case the common deployment.
        """
        import src.core.paths as paths

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.setattr(paths, "_repo_root", lambda: checkout)
        monkeypatch.delenv("MARCUS_DATA_DIR", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        registry = MarcusServiceRegistry(instance_id="marcus_default")

        info = registry.register_service(
            mcp_command="python -m src.marcus_mcp.server --http",
            log_dir="/Users/shared/dev/marcus/logs",
            provider="sqlite",
        )

        assert info["data_dir"] == str(checkout / "data")

    def test_an_explicit_value_is_honoured(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller that knows better can say so.

        ``register_service`` already forwards ``**kwargs`` into the
        entry, and that must keep working rather than being overwritten
        by the resolver.
        """
        info = _register(
            "marcus_explicit",
            str(tmp_path / "state"),
            monkeypatch,
            data_dir="/srv/somewhere-else",
        )

        assert info["data_dir"] == "/srv/somewhere-else"


class TestTwoInstancesAreDistinguishable:
    """The point of the field: telling two servers apart."""

    def test_two_sqlite_instances_differ_in_the_registry(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Everything else about them can be identical.

        This is the case that was indistinguishable: two servers from one
        checkout, same provider, same log directory, no project selected
        yet. Before the field, the entries differed only by pid.
        """
        first = _register("marcus_1", str(tmp_path / "state-a"), monkeypatch)
        second = _register("marcus_2", str(tmp_path / "state-b"), monkeypatch)

        assert first["provider"] == second["provider"]
        assert first["log_dir"] == second["log_dir"]
        assert first["project_name"] == second["project_name"]
        assert first["data_dir"] != second["data_dir"]
        assert first["data_dir"] == str(tmp_path / "state-a")
        assert second["data_dir"] == str(tmp_path / "state-b")

    def test_discovery_reports_both_data_directories(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader using discover_services() sees which is which."""
        _register("marcus_1", str(tmp_path / "state-a"), monkeypatch)
        _register("marcus_2", str(tmp_path / "state-b"), monkeypatch)
        monkeypatch.setattr(
            "src.core.service_registry.psutil.pid_exists", lambda _pid: True
        )

        found = {
            service["instance_id"]: service.get("data_dir")
            for service in MarcusServiceRegistry.discover_services()
        }

        assert found == {
            "marcus_1": str(tmp_path / "state-a"),
            "marcus_2": str(tmp_path / "state-b"),
        }

    def test_the_entry_on_disk_carries_it(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader that parses the JSON directly works too.

        Nothing in the Marcus repository reads this registry, so every
        consumer is out of tree, and some read the files rather than
        importing the class.
        """
        _register("marcus_1", str(tmp_path / "state-a"), monkeypatch)

        on_disk = json.loads((isolated_home / "marcus_1.json").read_text())

        assert on_disk["data_dir"] == str(tmp_path / "state-a")


class TestTheHeartbeatKeepsIt:
    """A long-running instance must not lose the field."""

    def test_an_update_does_not_drop_the_data_directory(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``update_heartbeat`` rewrites the file, so it has to survive."""
        monkeypatch.setenv("MARCUS_DATA_DIR", str(tmp_path / "state"))
        registry = MarcusServiceRegistry(instance_id="marcus_beat")
        registry.register_service(
            mcp_command="python -m src.marcus_mcp.server --http",
            log_dir="/Users/shared/dev/marcus/logs",
            provider="sqlite",
        )

        registry.update_heartbeat(project_name="some-project")

        on_disk = json.loads((isolated_home / "marcus_beat.json").read_text())
        assert on_disk["data_dir"] == str(tmp_path / "state")
        assert on_disk["project_name"] == "some-project"


class TestTheDocumentationMatchesTheCode:
    """The stale claim that sent this work down the wrong path.

    ``docs/source/systems/architecture/15-service-registry.md`` said Cato
    calls ``MarcusServiceRegistry.discover_services()``. Cato does not:
    it is pointed at Marcus by ``marcus_data_path`` in its own
    ``config.json`` and reads the data directory directly. Acting on the
    doc rather than the code produced a recommendation whose stated
    benefit could not happen, so the doc is corrected and pinned here.
    """

    def test_the_doc_does_not_claim_cato_calls_discovery(self) -> None:
        """A reader must not be told a consumer exists that does not."""
        doc = (
            Path(__file__).parents[3]
            / "docs"
            / "source"
            / "systems"
            / "architecture"
            / "15-service-registry.md"
        )

        text = doc.read_text()

        assert "Cato calls `MarcusServiceRegistry.discover_services()`" not in text

    def test_nothing_in_the_repository_reads_the_registry(self) -> None:
        """Pins the fact that every consumer is out of tree.

        If a reader appears inside Marcus, this test fails and whoever
        added it should say so in the docs, because the registry's
        machine-wide scope is a decision that depends on who reads it.
        """
        src = Path(__file__).parents[3] / "src"
        callers = sorted(
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if "discover_services(" in path.read_text()
            and path.name != "service_registry.py"
        )

        assert not callers, (
            "something in src/ now reads the service registry: "
            f"{callers}. The registry is machine-wide on the assumption "
            "that its consumers are external; revisit that if Marcus "
            "itself starts discovering instances."
        )
