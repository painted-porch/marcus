"""
Unit tests for lease-system initialization in single-project mode (issue #763).

``MarcusServer.initialize()`` built the assignment monitor and the lease
manager only on its multi-project branches. In ``single_project_mode``,
the default, no lease was ever created and the lease monitor never ran,
silently. These tests drive ``initialize()`` with a fake server and pin
that both modes initialize the monitoring systems.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.marcus_mcp.server import MarcusServer

pytestmark = pytest.mark.unit


def _fake_server(kanban_client: object) -> Mock:
    """A server stand-in with just what ``initialize()`` touches."""
    fake = Mock()
    fake.ai_engine = Mock()
    fake.ai_engine.initialize = AsyncMock()
    fake.project_manager = Mock()
    fake.project_manager.initialize = AsyncMock()
    fake.project_manager.registry.get_active_project = AsyncMock(return_value=None)
    fake.project_manager.get_kanban_client = AsyncMock(return_value=kanban_client)
    fake.project_manager.switch_project = AsyncMock()
    fake.project_registry.get_active_project = AsyncMock(return_value=None)
    fake.provider = "sqlite"
    fake.kanban_client = None

    async def _initialize_kanban() -> None:
        fake.kanban_client = kanban_client

    fake.initialize_kanban = AsyncMock(side_effect=_initialize_kanban)
    fake._migrate_to_multi_project = AsyncMock()
    fake._initialize_monitoring_systems = AsyncMock()
    fake.event_visualizer = None
    return fake


def _config(single_project_mode: bool) -> Mock:
    cfg = Mock()
    cfg.single_project_mode = single_project_mode
    cfg.default_project_name = None
    return cfg


class TestInitializeBuildsTheLeaseSystem:
    """Test suite for the lease system coming up in every mode."""

    @pytest.mark.asyncio
    async def test_single_project_mode_initializes_monitoring_systems(self) -> None:
        """The default mode must build the lease manager and monitor (issue #763)."""
        fake = _fake_server(kanban_client=Mock())

        with (
            patch("src.marcus_mcp.server.get_config", return_value=_config(True)),
            patch(
                "src.marcus_mcp.server.ai_usage_middleware.wrap_ai_provider",
                side_effect=lambda x: x,
            ),
        ):
            await MarcusServer.initialize(fake)  # type: ignore[arg-type]

        fake.initialize_kanban.assert_awaited_once()
        fake._initialize_monitoring_systems.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_project_mode_still_initializes_monitoring_systems(
        self,
    ) -> None:
        """The multi-project path keeps doing what it did."""
        fake = _fake_server(kanban_client=Mock())

        with (
            patch("src.marcus_mcp.server.get_config", return_value=_config(False)),
            patch(
                "src.marcus_mcp.server.ai_usage_middleware.wrap_ai_provider",
                side_effect=lambda x: x,
            ),
        ):
            await MarcusServer.initialize(fake)  # type: ignore[arg-type]

        fake._initialize_monitoring_systems.assert_awaited()

    @pytest.mark.asyncio
    async def test_monitoring_initializer_is_a_no_op_without_a_kanban_client(
        self,
    ) -> None:
        """Calling the helper before a board exists must not build anything."""
        fake = Mock()
        fake.kanban_client = None
        fake.assignment_monitor = None
        fake.lease_manager = None

        await MarcusServer._initialize_monitoring_systems(fake)  # type: ignore[arg-type]

        assert fake.lease_manager is None
        assert fake.assignment_monitor is None
