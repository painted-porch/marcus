"""
Unit tests for ``ProjectContextManager._build_provider_config`` (issue #854).

K6/K2's lookups need a task's own ``project_id`` to match a registry
key directly. For SQLite (GH-306) the registry id and the kanban
project id are the same string by construction; for Planka and
Linear they are not, which is #849/#853's regression. The real fix:
every kanban client constructed from a registered project carries
that project's own registry id down as ``registry_project_id``, so
``KanbanClient``/``LinearKanban`` can stamp it onto every task they
produce instead of the provider's own id.
"""

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import Mock

import pytest

from src.core.project_context_manager import ProjectContextManager
from src.core.project_registry import ProjectConfig

pytestmark = pytest.mark.unit


def _manager() -> ProjectContextManager:
    return ProjectContextManager.__new__(ProjectContextManager)


def _project(
    provider: str, provider_config: Dict[str, Any], project_id: str = "uuid-1"
) -> ProjectConfig:
    now = datetime.now(timezone.utc)
    return ProjectConfig(
        id=project_id,
        name="Camp",
        provider=provider,
        provider_config=provider_config,
        created_at=now,
        last_used=now,
    )


class TestBuildProviderConfigCarriesTheRegistryId:
    def test_planka_config_carries_the_registry_id(self) -> None:
        manager = _manager()
        manager.config = (
            Mock()
        )  # see module docstring: no real config/env (#873 follow-up)
        project = _project(
            "planka",
            {"project_id": "planka-proj-77", "board_id": "kb-1"},
            project_id="uuid-registry-1",
        )

        config = manager._build_provider_config(project)

        assert config["registry_project_id"] == "uuid-registry-1"
        # The provider's own id is untouched.
        assert config["project_id"] == "planka-proj-77"

    def test_linear_config_carries_the_registry_id(self) -> None:
        manager = _manager()
        manager.config = (
            Mock()
        )  # see module docstring: no real config/env (#873 follow-up)
        project = _project(
            "linear",
            {"project_id": "linear-proj-9", "team_id": "t-1"},
            project_id="uuid-registry-2",
        )

        config = manager._build_provider_config(project)

        assert config["registry_project_id"] == "uuid-registry-2"
        assert config["project_id"] == "linear-proj-9"

    def test_sqlite_config_also_carries_it_harmlessly(self) -> None:
        """SQLite's own id already equals the registry id (GH-306);

        carrying registry_project_id alongside it is a no-op there,
        not a special case to avoid."""
        manager = _manager()
        manager.config = (
            Mock()
        )  # see module docstring: no real config/env (#873 follow-up)
        project = _project("sqlite", {}, project_id="camp-1")

        config = manager._build_provider_config(project)

        assert config["registry_project_id"] == "camp-1"

    def test_the_registry_id_cannot_be_overridden_by_provider_config(self) -> None:
        """provider_config is caller-supplied and merged in; it must

        not be able to spoof registry_project_id and point a task at
        a different project's registry entry."""
        manager = _manager()
        manager.config = (
            Mock()
        )  # see module docstring: no real config/env (#873 follow-up)
        project = _project(
            "planka",
            {"project_id": "planka-proj-77", "registry_project_id": "spoofed"},
            project_id="uuid-registry-1",
        )

        config = manager._build_provider_config(project)

        assert config["registry_project_id"] == "uuid-registry-1"


class TestEndToEndThroughKanbanFactory:
    """The full chain: a registered project's own id reaches a real

    task's ``project_id``, through ``_build_provider_config`` and the
    real provider classes -- not just each piece tested in isolation.
    """

    def test_a_planka_card_is_stamped_with_the_registry_id(self) -> None:
        from unittest.mock import patch

        from src.integrations.kanban_factory import KanbanFactory
        from src.integrations.providers.planka import Planka

        manager = _manager()
        # Planka.__init__ writes these three straight into os.environ, which
        # requires real strings -- a bare Mock() (safe everywhere else in
        # this file) fails with "TypeError: str expected, not Mock" here.
        manager.config = Mock(
            kanban=Mock(
                planka_base_url="https://planka.test",
                planka_email="agent@test.local",
                planka_password="test-password",
            )
        )
        project = _project(
            "planka",
            {"project_id": "planka-proj-77", "board_id": "kb-1"},
            project_id="uuid-registry-1",
        )
        config = manager._build_provider_config(project)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.dict("os.environ", {}, clear=True),
            patch(
                "src.integrations.kanban_client.KanbanClient._load_workspace_state",
                return_value=None,
            ),
            # KanbanFactory.create() unconditionally calls get_config() at
            # its own top, even though the planka branch below never uses
            # the result when an explicit config dict (ours) is passed.
            # Patched so the deliberately-cleared os.environ/Path.exists
            # above don't make a stale cached config the only thing
            # standing between this test and a real validation failure
            # (issue #873's own follow-up).
            patch("src.integrations.kanban_factory.get_config", return_value=Mock()),
        ):
            provider = KanbanFactory.create("planka", config)

        assert isinstance(provider, Planka)
        task = provider.client._card_to_task({"id": "card-1", "name": "Task"})

        assert task.project_id == "uuid-registry-1"

    def test_a_linear_issue_is_stamped_with_the_registry_id(self) -> None:
        from unittest.mock import AsyncMock, patch

        from src.integrations.kanban_factory import KanbanFactory
        from src.integrations.providers.linear_kanban import LinearKanban

        manager = _manager()
        manager.config = (
            Mock()
        )  # see module docstring: no real config/env (#873 follow-up)
        project = _project(
            "linear",
            {"project_id": "linear-proj-9", "team_id": "t-1"},
            project_id="uuid-registry-2",
        )
        config = manager._build_provider_config(project)
        config["mcp_function_caller"] = AsyncMock()

        # KanbanFactory.create() unconditionally calls get_config() at its
        # own top, even though the linear branch below never uses the
        # result when an explicit config dict (ours) is passed -- the
        # same issue #873 follow-up found in the planka test above.
        with patch("src.integrations.kanban_factory.get_config", return_value=Mock()):
            provider = KanbanFactory.create("linear", config)
        assert isinstance(provider, LinearKanban)
        issue = {
            "id": "issue-1",
            "title": "Check the claim",
            "state": {"name": "Todo", "type": "unstarted"},
        }

        task = provider._linear_issue_to_task(issue)

        assert task.project_id == "uuid-registry-2"
