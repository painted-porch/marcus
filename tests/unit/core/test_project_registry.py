"""
Unit tests for ADR-0013 K2's registration field on ProjectConfig.

K2 (Accepted 2026-11-05): an extension declares, per project, a named
set of task types it serves and, for each, the evidence it requires
and whether checks may be injected. Registration is board state --
part of the project record (``ProjectConfig``), not a server config
file or a callable reference.

Before this field existed, ``ProjectConfig.from_dict`` filtered every
incoming dict to a fixed ``expected_fields`` set and silently dropped
anything outside it, so a registration block in ``options`` would
have vanished on the very next read.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from src.core.error_framework import InvalidConfigurationError, StateConflictError
from src.core.persistence import FilePersistence, MemoryPersistence, Persistence
from src.core.project_registry import (
    ProjectConfig,
    ProjectRegistry,
    register_sqlite_board,
)

pytestmark = pytest.mark.unit


def _config(**overrides: object) -> ProjectConfig:
    """Build a minimal ProjectConfig, overriding only what a test needs."""
    defaults: Dict[str, Any] = {
        "id": "proj-1",
        "name": "payments-rework",
        "provider": "sqlite",
        "provider_config": {},
        "created_at": datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc),
        "last_used": datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)


class TestTaskTypeRegistrationsDefaultsEmpty:
    """A project that registers nothing behaves exactly as before K2."""

    def test_omitting_it_defaults_to_an_empty_dict(self) -> None:
        config = _config()

        assert config.task_type_registrations == {}

    def test_from_dict_on_a_pre_k2_payload_still_constructs(self) -> None:
        """A payload persisted before K2 existed has no such key at all."""
        legacy = {
            "id": "proj-1",
            "name": "payments-rework",
            "provider": "sqlite",
            "provider_config": {},
            "created_at": "2026-11-06T00:00:00+00:00",
            "last_used": "2026-11-06T00:00:00+00:00",
        }

        config = ProjectConfig.from_dict(legacy)

        assert config.task_type_registrations == {}


class TestTaskTypeRegistrationsRoundTrip:
    """The registration survives exactly the persistence path it needs to."""

    def test_to_dict_includes_the_registration(self) -> None:
        config = _config(
            task_type_registrations={
                "verify": {
                    "evidence_schema": {"required": ["verdict"]},
                    "checks_injectable": True,
                }
            }
        )

        data = config.to_dict()

        assert data["task_type_registrations"] == {
            "verify": {
                "evidence_schema": {"required": ["verdict"]},
                "checks_injectable": True,
            }
        }

    def test_from_dict_of_to_dict_round_trips_the_registration(self) -> None:
        """This is the exact gap K2 found: from_dict used to drop any key
        outside its hardcoded expected_fields set, so a registration
        written via to_dict would vanish on the very next read."""
        original = _config(
            task_type_registrations={
                "verify": {
                    "evidence_schema": {"required": ["verdict"]},
                    "checks_injectable": True,
                },
                "synthesize": {
                    "evidence_schema": {"required": ["summary"]},
                    "checks_injectable": False,
                },
            }
        )

        restored = ProjectConfig.from_dict(original.to_dict())

        assert restored.task_type_registrations == original.task_type_registrations

    def test_two_different_projects_keep_independent_registrations(self) -> None:
        """No shared mutable default -- a classic dataclass pitfall that
        would make every project's registration the same dict object."""
        a = _config(id="proj-a")
        b = _config(id="proj-b")

        a.task_type_registrations["verify"] = {"evidence_schema": {}}

        assert b.task_type_registrations == {}


def _registry() -> ProjectRegistry:
    """Build a ProjectRegistry over an in-memory backend."""
    return ProjectRegistry(Persistence(MemoryPersistence()))


class TestGetProjectByProviderId:
    """Issue #849 regression, Codex P2 on PR #853.

    Planka and Linear stamp ``Task.project_id`` from the PROVIDER's
    own project id, while a registry entry created by
    ``add_project``/``sync_projects`` carries an independently
    generated UUID as its own ``id`` -- unlike the SQLite path
    (GH-306), where the two are the same string by construction.
    """

    @pytest.mark.asyncio
    async def test_resolves_a_planka_shaped_task_by_its_provider_id(self) -> None:
        registry = _registry()
        await registry.add_project(
            _config(
                id="uuid-registry-1",
                provider="planka",
                provider_config={"project_id": "planka-proj-77", "board_id": "b-1"},
            )
        )

        found = registry.get_project_by_provider_id("planka-proj-77")

        assert found is not None
        assert found.id == "uuid-registry-1"

    @pytest.mark.asyncio
    async def test_resolves_a_linear_shaped_task_by_its_provider_id(self) -> None:
        registry = _registry()
        await registry.add_project(
            _config(
                id="uuid-registry-2",
                provider="linear",
                provider_config={"project_id": "linear-proj-9", "team_id": "t-1"},
            )
        )

        found = registry.get_project_by_provider_id("linear-proj-9")

        assert found is not None
        assert found.id == "uuid-registry-2"

    def test_no_match_returns_none(self) -> None:
        registry = _registry()

        assert registry.get_project_by_provider_id("nothing-here") is None

    @pytest.mark.asyncio
    async def test_does_not_match_the_registry_id_itself(self) -> None:
        """A registry id is not a provider id; the two must not be
        confused even when a caller passes one where the other goes."""
        registry = _registry()
        await registry.add_project(
            _config(
                id="uuid-registry-3",
                provider="planka",
                provider_config={"project_id": "planka-proj-3"},
            )
        )

        assert registry.get_project_by_provider_id("uuid-registry-3") is None


class TestGetActiveProjectSync:
    """Issue #830: a synchronous cache read for callers that cannot await.

    ``eligibility.py`` runs inside the synchronous assignment path and
    needs the active project's registration without awaiting the
    registry's normal I/O path.
    """

    @pytest.mark.asyncio
    async def test_returns_the_active_project_once_loaded(self) -> None:
        registry = _registry()
        config = _config()
        await registry.add_project(config)

        active = registry.get_active_project_sync()

        assert active is not None
        assert active.id == config.id

    def test_returns_none_before_any_project_is_active(self) -> None:
        registry = _registry()

        assert registry.get_active_project_sync() is None

    @pytest.mark.asyncio
    async def test_matches_the_async_accessor(self) -> None:
        registry = _registry()
        config = _config()
        await registry.add_project(config)

        assert registry.get_active_project_sync() == await registry.get_active_project()


class TestRegisterSqliteBoard:
    """Issue #741 step 2: the sanctioned path for registering a board

    that was seeded directly through a kanban provider, bypassing
    Marcus's own project-creation pipeline. Callers get back the
    registry project id BEFORE seeding, so tasks can be created with
    their final, registry-prefixed ids and no seeder-side rewrite is
    ever needed.
    """

    @pytest.fixture
    def registry(self, tmp_path: Path) -> Any:
        """File-backed registry in a temporary directory."""
        backend = FilePersistence(storage_dir=tmp_path / "state")
        return ProjectRegistry(persistence=Persistence(backend=backend))

    @pytest.mark.asyncio
    async def test_registers_the_board_under_its_real_name(
        self, tmp_path: Path, registry: Any
    ) -> None:
        db = tmp_path / "kanban.db"

        project_id = await register_sqlite_board("my-board", str(db), registry=registry)

        stored = await registry.get_project(project_id)
        assert stored is not None
        assert stored.name == "my-board - Main Board"
        assert stored.provider == "sqlite"
        assert stored.provider_config["project_name"] == "my-board"
        assert Path(stored.provider_config["db_path"]).is_absolute()
        assert "auto-created" in stored.tags

    @pytest.mark.asyncio
    async def test_registry_id_matches_the_kanban_project_id(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """GH-306: the kanban project id is the single source of truth,
        not an auto-generated registry UUID, so a task's own
        project_id field (written from this same string) finds this
        project on lookup."""
        db = tmp_path / "kanban.db"

        project_id = await register_sqlite_board("my-board", str(db), registry=registry)

        assert project_id == "my-board"

    @pytest.mark.asyncio
    async def test_registration_is_idempotent(
        self, tmp_path: Path, registry: Any
    ) -> None:
        db = str(tmp_path / "kanban.db")

        first = await register_sqlite_board("my-board", db, registry=registry)
        second = await register_sqlite_board("my-board", db, registry=registry)

        assert first == second
        assert len(await registry.list_projects(provider="sqlite")) == 1

    @pytest.mark.asyncio
    async def test_the_same_project_id_against_a_different_db_is_refused(
        self, tmp_path: Path, registry: Any
    ) -> None:
        first_db = str(tmp_path / "kanban-first.db")
        second_db = str(tmp_path / "kanban-second.db")

        await register_sqlite_board("my-board", first_db, registry=registry)

        with pytest.raises(StateConflictError):
            await register_sqlite_board("my-board", second_db, registry=registry)

        stored = await registry.get_project("my-board")
        assert stored is not None
        assert Path(stored.provider_config["db_path"]) == Path(first_db).resolve()

    @pytest.mark.asyncio
    async def test_a_legacy_uuid_record_is_migrated_not_duplicated(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """A board registered by a pre-GH-306 caller carries a
        generated UUID as its registry id, not project_id.
        Re-registering it migrates in place: one record, under
        project_id, with the legacy registrations and active status
        carried over, the UUID record gone."""
        db = str(tmp_path / "kanban.db")
        absolute = str(Path(db).resolve())
        now = datetime.now(timezone.utc)
        legacy_id = "ad71d867-8384-4db7-b125-d84d6d2acf8c"
        legacy = ProjectConfig(
            id=legacy_id,
            name="my-board - Main Board",
            provider="sqlite",
            provider_config={
                "db_path": absolute,
                "project_name": "my-board",
                "attachments_dir": str(Path(absolute).parent / "attachments"),
                "project_id": "my-board",
            },
            created_at=now,
            last_used=now,
            tags=["auto-created", "sqlite", "tow-experiment"],
            task_type_registrations={
                "verify": {"evidence_schema": {"required": ["verdict"]}}
            },
        )
        await registry.add_project(legacy)
        await registry.set_active_project(legacy_id)

        new_id = await register_sqlite_board("my-board", db, registry=registry)

        assert new_id == "my-board"
        boards = await registry.list_projects(provider="sqlite")
        assert len(boards) == 1
        canonical = boards[0]
        assert canonical.id == "my-board"
        assert canonical.task_type_registrations == {
            "verify": {"evidence_schema": {"required": ["verdict"]}}
        }
        active = await registry.get_active_project()
        assert active is not None
        assert active.id == "my-board"


class TestInjectedChecksAreValidatedAtRegistration:
    """ADR-0013 K4 (issue #847): declarative, kernel-enforced checks.

    ``injected_checks`` is data under the same K2 registration record
    -- never a callable or a command to run (K2's own "dispatch is
    pull, not call" rule) -- naming checks the worker did not author
    but must show evidence for. A project declaring it wrong is a
    configuration error caught at registration, not something that
    silently does nothing or silently runs.
    """

    @pytest.mark.asyncio
    async def test_checks_injectable_false_with_a_nonempty_list_is_rejected(
        self,
    ) -> None:
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {
                    "checks_injectable": False,
                    "injected_checks": [{"name": "cross_check_totals"}],
                }
            }
        )

        with pytest.raises(InvalidConfigurationError):
            await registry.add_project(config)

    @pytest.mark.asyncio
    async def test_checks_injectable_absent_with_a_nonempty_list_is_rejected(
        self,
    ) -> None:
        """Absent is not opted-in; only an explicit ``True`` is."""
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {"injected_checks": [{"name": "cross_check_totals"}]}
            }
        )

        with pytest.raises(InvalidConfigurationError):
            await registry.add_project(config)

    @pytest.mark.asyncio
    async def test_a_check_with_no_name_is_rejected(self) -> None:
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {
                    "checks_injectable": True,
                    "injected_checks": [{"description": "no name given"}],
                }
            }
        )

        with pytest.raises(InvalidConfigurationError):
            await registry.add_project(config)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_name", [123, ["cross_check_totals"], "", None, {"nested": True}]
    )
    async def test_a_non_string_or_empty_name_is_rejected(self, bad_name) -> None:
        """Codex P2: a truthy non-string name (an int, a list) would be
        accepted and persisted today, then blow up at completion time
        instead of producing an evidence rejection --
        ``_apply_typed_evidence_gate`` uses it as a dict key (raises for
        a list) and ``_typed_evidence_rejection`` joins it as a string
        (raises for an int). Every field of a declared record is
        validated where it is declared, not where it is consumed."""
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {
                    "checks_injectable": True,
                    "injected_checks": [{"name": bad_name}],
                }
            }
        )

        with pytest.raises(InvalidConfigurationError):
            await registry.add_project(config)

    @pytest.mark.asyncio
    async def test_a_non_string_description_is_rejected(self) -> None:
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {
                    "checks_injectable": True,
                    "injected_checks": [
                        {"name": "cross_check_totals", "description": 123}
                    ],
                }
            }
        )

        with pytest.raises(InvalidConfigurationError):
            await registry.add_project(config)

    @pytest.mark.asyncio
    async def test_a_check_carrying_a_command_field_is_rejected(self) -> None:
        """K2: dispatch is pull, not call. The data shape must not be
        able to carry anything the kernel could run -- only a name and
        a human-readable description, per K4's acceptance."""
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {
                    "checks_injectable": True,
                    "injected_checks": [
                        {"name": "cross_check_totals", "command": "run_me.sh"}
                    ],
                }
            }
        )

        with pytest.raises(InvalidConfigurationError):
            await registry.add_project(config)

    @pytest.mark.asyncio
    async def test_a_well_formed_declaration_is_accepted(self) -> None:
        registry = _registry()
        config = _config(
            task_type_registrations={
                "verify": {
                    "checks_injectable": True,
                    "injected_checks": [
                        {
                            "name": "cross_check_totals",
                            "description": "Bucket totals sum to the report total",
                        }
                    ],
                }
            }
        )

        project_id = await registry.add_project(config)

        stored = await registry.get_project(project_id)
        assert stored is not None
        assert stored.task_type_registrations["verify"]["injected_checks"] == [
            {
                "name": "cross_check_totals",
                "description": "Bucket totals sum to the report total",
            }
        ]

    @pytest.mark.asyncio
    async def test_an_empty_list_needs_no_checks_injectable_flag(self) -> None:
        """An empty or absent list is simply "nothing declared" -- not
        an error, regardless of the flag."""
        registry = _registry()
        config = _config(task_type_registrations={"verify": {"injected_checks": []}})

        project_id = await registry.add_project(config)

        assert project_id == config.id

    @pytest.mark.asyncio
    async def test_update_project_is_validated_too(self) -> None:
        """Live-check seeding (and any other extension) routinely
        writes a K2 registration through ``update_project`` rather
        than at ``add_project`` time -- it must not be a back door
        around the same validation."""
        registry = _registry()
        config = _config()
        await registry.add_project(config)

        with pytest.raises(InvalidConfigurationError):
            await registry.update_project(
                config.id,
                {
                    "task_type_registrations": {
                        "verify": {
                            "checks_injectable": False,
                            "injected_checks": [{"name": "cross_check_totals"}],
                        }
                    }
                },
            )
