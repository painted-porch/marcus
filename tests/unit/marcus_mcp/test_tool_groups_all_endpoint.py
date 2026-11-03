"""
Unit tests for the ``all`` tool group (issue #795).

``--all-tools`` promises every tool Marcus serves.  Until #795 it served
a hand-copied dozen that had drifted from the endpoint groups and lost
``log_decision`` and ``log_artifact``, so an agent on that server could
not record provenance at all.  The surface is now the union of every
endpoint group, resolved through the one registration path.
"""

import pytest

from src.marcus_mcp.tool_groups import (
    TOOL_GROUPS,
    get_tools_for_endpoint,
    is_tool_allowed,
)

pytestmark = pytest.mark.unit


class TestAllToolGroup:
    """Test suite for the union tool group."""

    def test_all_is_the_union_of_every_group(self) -> None:
        """``all`` carries exactly the tools of every named group."""
        expected = set().union(*TOOL_GROUPS.values())

        assert get_tools_for_endpoint("all") == expected

    def test_all_includes_the_provenance_tools(self) -> None:
        """The provenance tools agents call are on the union."""
        tools = get_tools_for_endpoint("all")

        assert {"log_decision", "log_artifact"} <= tools

    def test_is_tool_allowed_honours_the_union(self) -> None:
        """``is_tool_allowed`` answers for ``all`` like any other group."""
        assert is_tool_allowed("all", "log_decision") is True
        assert is_tool_allowed("all", "create_tasks") is True
        assert is_tool_allowed("all", "not_a_tool") is False

    def test_give_opinion_is_an_agent_and_analytics_tool(self) -> None:
        """Agents post opinions; analytics reads them (deliberation B1)."""
        assert "give_opinion" in TOOL_GROUPS["agent"]
        assert "give_opinion" in TOOL_GROUPS["analytics"]
        assert "give_opinion" not in TOOL_GROUPS["human"]

    def test_unknown_group_is_still_empty(self) -> None:
        """A name that is not a group resolves to no tools, as before."""
        assert get_tools_for_endpoint("nope") == set()
