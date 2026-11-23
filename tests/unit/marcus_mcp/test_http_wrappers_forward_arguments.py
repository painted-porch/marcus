"""
Unit tests: each HTTP tool wrapper forwards what it advertises (issue #939).

The HTTP server builds every tool from a Python wrapper in
``src/marcus_mcp/server.py``; FastMCP advertises the wrapper's signature as
the tool's schema. Writing the kernel contract found wrappers that
advertised arguments and then ignored them, so a caller got a successful
reply that did not do what it asked:

* ``get_usage_report`` advertised ``start_date``, ``end_date`` and
  ``group_by`` and always reported the last seven days;
* ``list_projects`` took no arguments, so its ``provider`` and
  ``filter_tags`` filters were unreachable over HTTP;
* ``switch_project`` took ``project_name`` but the implementation reads
  ``name``, so switching by name always failed;
* ``add_feature`` named its arguments differently from stdio and dropped
  ``integration_point``.

Each test calls the real wrapper through FastMCP with the implementation
patched, and asserts what the implementation received.
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit


async def _call(
    tool: str, arguments: Dict[str, Any], target: str, returns: Any = None
) -> AsyncMock:
    """Register the ``--all-tools`` surface and call one tool.

    Parameters
    ----------
    tool : str
        The tool to call.
    arguments : Dict[str, Any]
        Its arguments, as a client would send them.
    target : str
        Dotted path of the implementation to patch.
    returns : Any
        What the patched implementation returns; ``{"success": True}`` when
        omitted.

    Returns
    -------
    AsyncMock
        The patched implementation, for assertions on what it received.
    """
    from mcp.server.fastmcp import FastMCP

    from src.marcus_mcp.server import MarcusServer

    server: Any = MarcusServer.__new__(MarcusServer)
    server._fastmcp = FastMCP("wrapper-test")
    server._register_fastmcp_tools()
    impl = AsyncMock(return_value={"success": True} if returns is None else returns)
    with patch(target, impl):
        await server._fastmcp.call_tool(tool, arguments)
    return impl


class TestGetUsageReport:
    """The report covers the window the caller asked for."""

    @pytest.mark.asyncio
    async def test_days_reaches_the_implementation(self) -> None:
        """``days=30`` produces a thirty-day report, not a seven-day one."""
        impl = await _call(
            "get_usage_report",
            {"days": 30},
            "src.marcus_mcp.tools.audit_tools.get_usage_report",
        )

        assert impl.await_args.kwargs["days"] == 30


class TestListProjects:
    """The filters the implementation supports are reachable over HTTP."""

    @pytest.mark.asyncio
    async def test_provider_and_tags_reach_the_implementation(self) -> None:
        """Both filters arrive in the arguments the implementation reads."""
        impl = await _call(
            "list_projects",
            {"provider": "sqlite", "filter_tags": ["citation"]},
            "src.marcus_mcp.tools.project_management.list_projects",
            returns=[],
        )

        arguments = impl.await_args.args[1]
        assert (arguments["provider"], arguments["filter_tags"]) == (
            "sqlite",
            ["citation"],
        )


class TestSwitchProject:
    """A project can be chosen by its name as advertised."""

    @pytest.mark.asyncio
    async def test_project_name_switches_to_the_matching_project(self) -> None:
        """The implementation resolves ``project_name`` to the project's id."""
        from types import SimpleNamespace

        from src.marcus_mcp.tools.project_management import switch_project

        project = SimpleNamespace(
            id="p-1", name="Alpha", provider="sqlite", tags=["sqlite"]
        )
        server = SimpleNamespace(
            project_registry=SimpleNamespace(
                list_projects=AsyncMock(return_value=[project]),
                get_project=AsyncMock(return_value=project),
            ),
            project_manager=SimpleNamespace(
                switch_project=AsyncMock(return_value=True),
                get_kanban_client=AsyncMock(return_value=object()),
            ),
        )

        result = await switch_project(server, {"project_name": "alpha"})

        assert (result["success"], result["project"]["id"]) == (True, "p-1")

    @pytest.mark.asyncio
    async def test_the_wrapper_forwards_both_ways_of_naming_a_project(self) -> None:
        """``project_id`` and ``project_name`` both reach the implementation."""
        impl = await _call(
            "switch_project",
            {"project_id": "p-1", "project_name": "Alpha"},
            "src.marcus_mcp.tools.project_management.switch_project",
        )

        arguments = impl.await_args.args[1]
        assert (arguments["project_id"], arguments["project_name"]) == ("p-1", "Alpha")


class TestAddFeature:
    """The HTTP wrapper takes the arguments stdio takes."""

    @pytest.mark.asyncio
    async def test_integration_point_reaches_the_implementation(self) -> None:
        """A caller's integration choice is not replaced by a constant."""
        impl = await _call(
            "add_feature",
            {"feature_description": "Add a pause key", "integration_point": "parallel"},
            "src.marcus_mcp.tools.nlp.add_feature",
        )

        kwargs = impl.await_args.kwargs
        assert (kwargs["feature_description"], kwargs["integration_point"]) == (
            "Add a pause key",
            "parallel",
        )


class TestGetCostSummary:
    """Reading cost back is on the contract's own surface."""

    @pytest.mark.asyncio
    async def test_project_id_reaches_the_implementation(self) -> None:
        """HTTP serves the tool stdio already served, with the same arguments."""
        impl = await _call(
            "get_cost_summary",
            {"project_id": "p-1"},
            "src.marcus_mcp.tools.cost_tracking.get_cost_summary",
        )

        assert (
            impl.await_args.kwargs["project_id"],
            impl.await_args.kwargs["run_id"],
        ) == (
            "p-1",
            None,
        )
