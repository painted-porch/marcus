"""
Unit tests for the kernel contract's tool index (issue #939, Track A1).

Marcus serves its tools through two registries that were written by hand
and drifted apart: the HTTP server (``src/marcus_mcp/server.py``, the
surface every extension starts with ``--http --all-tools``) and the stdio
handlers (``src/marcus_mcp/handlers.py``). The contract names the HTTP
surface as the contract and sorts every tool it serves into one of three
tiers in ``src/marcus_mcp/kernel_contract.py``. These tests pin that:

* every tool the HTTP server registers is in exactly one tier, so a new
  tool cannot ship without someone deciding whether it is part of the door;
* ``TOOL_GROUPS`` names no tool that nothing registers;
* stdio serves no tool HTTP does not, defines none twice, and agrees with
  HTTP on the argument names and the required set of every tool both serve;
* ``docs/architecture/kernel-contract.md`` states the same version as the
  code and documents every contract tool.
"""

import re
from pathlib import Path
from typing import Any, Dict, Set

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DOC = _REPO_ROOT / "docs" / "architecture" / "kernel-contract.md"
_STDIO_ROLES = ("agent", "human")


async def _http_schemas() -> Dict[str, Dict[str, Any]]:
    """Return every tool the ``--all-tools`` HTTP surface registers.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Tool name to its advertised input schema.
    """
    from mcp.server.fastmcp import FastMCP

    from src.marcus_mcp.server import MarcusServer

    server: Any = MarcusServer.__new__(MarcusServer)
    server._fastmcp = FastMCP("contract-test")
    server._register_fastmcp_tools()
    return {t.name: t.inputSchema for t in await server._fastmcp.list_tools()}


def _stdio_schemas() -> Dict[str, Dict[str, Any]]:
    """Return every tool any stdio role is offered, by name.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Tool name to its advertised input schema.
    """
    from src.marcus_mcp.handlers import get_tool_definitions

    schemas: Dict[str, Dict[str, Any]] = {}
    for role in _STDIO_ROLES:
        for tool in get_tool_definitions(role):
            schemas.setdefault(tool.name, tool.inputSchema)
    return schemas


class TestEveryServedToolHasATier:
    """The HTTP surface and the three tiers are the same set of names."""

    @pytest.mark.asyncio
    async def test_every_registered_tool_is_in_exactly_one_tier(self) -> None:
        """A tool registered on HTTP but in no tier is an undecided door."""
        from src.marcus_mcp.kernel_contract import TIERS

        registered = set(await _http_schemas())
        tiered: Set[str] = set().union(*TIERS.values())

        assert registered == tiered, (
            f"untiered: {sorted(registered - tiered)}; "
            f"tiered but not registered: {sorted(tiered - registered)}"
        )

    def test_the_tiers_do_not_overlap(self) -> None:
        """A tool cannot be both part of the door and outside it."""
        from src.marcus_mcp.kernel_contract import TIERS

        names = [name for tier in TIERS.values() for name in tier]

        assert len(names) == len(set(names))

    def test_tier_of_names_the_tier_and_refuses_an_unknown_tool(self) -> None:
        """``tier_of`` is the lookup a caller uses instead of the sets."""
        from src.marcus_mcp.kernel_contract import tier_of

        assert (tier_of("request_next_task"), tier_of("get_code_metrics")) == (
            "contract",
            "leaving_with_track_c",
        )
        with pytest.raises(KeyError):
            tier_of("no_such_tool")


class TestToolGroupsNameOnlyRegisteredTools:
    """``TOOL_GROUPS`` grants nothing that no code registers."""

    @pytest.mark.asyncio
    async def test_no_group_names_an_unregistered_tool(self) -> None:
        """Fourteen pipeline and what-if names once registered nothing."""
        from src.marcus_mcp.tool_groups import (
            ALL_TOOLS_ENDPOINT,
            get_tools_for_endpoint,
        )

        granted = get_tools_for_endpoint(ALL_TOOLS_ENDPOINT)
        registered = set(await _http_schemas())

        assert granted - registered == set()


class TestStdioIsTestedAgainstTheContract:
    """stdio is a second transport and must say what HTTP says."""

    @pytest.mark.parametrize("role", _STDIO_ROLES)
    def test_stdio_defines_no_tool_twice(self, role: str) -> None:
        """Two definitions of one name leave which one a client sees to chance."""
        from src.marcus_mcp.handlers import get_tool_definitions

        names = [tool.name for tool in get_tool_definitions(role)]

        assert sorted({n for n in names if names.count(n) > 1}) == []

    @pytest.mark.asyncio
    async def test_stdio_serves_no_tool_http_does_not(self) -> None:
        """Every stdio tool is on the contract's own surface."""
        http = set(await _http_schemas())

        assert set(_stdio_schemas()) - http == set()

    @pytest.mark.asyncio
    async def test_stdio_and_http_agree_on_argument_names(self) -> None:
        """An argument one transport advertises and the other drops is drift."""
        http = await _http_schemas()
        stdio = _stdio_schemas()

        disagreements = {
            name: {
                "http_only": sorted(
                    set(http[name].get("properties", {}))
                    - set(stdio[name].get("properties", {}))
                ),
                "stdio_only": sorted(
                    set(stdio[name].get("properties", {}))
                    - set(http[name].get("properties", {}))
                ),
            }
            for name in set(http) & set(stdio)
            if set(http[name].get("properties", {}))
            != set(stdio[name].get("properties", {}))
        }

        assert disagreements == {}

    @pytest.mark.asyncio
    async def test_stdio_and_http_agree_on_required_arguments(self) -> None:
        """A call valid on one transport must not be refused by the other."""
        http = await _http_schemas()
        stdio = _stdio_schemas()

        disagreements = {
            name: {
                "http": sorted(http[name].get("required", [])),
                "stdio": sorted(stdio[name].get("required", [])),
            }
            for name in set(http) & set(stdio)
            if set(http[name].get("required", []))
            != set(stdio[name].get("required", []))
        }

        assert disagreements == {}


class TestTheDocumentMatchesTheCode:
    """``kernel-contract.md`` is the spec; the index is its machine copy."""

    def test_the_document_states_the_code_version(self) -> None:
        """A release states one contract version, so the two cannot differ."""
        from src.marcus_mcp.kernel_contract import KERNEL_CONTRACT_VERSION

        text = _CONTRACT_DOC.read_text()
        stated = re.search(r"^\*\*Contract version: (\S+)\*\*$", text, re.MULTILINE)

        assert stated is not None and stated.group(1) == KERNEL_CONTRACT_VERSION

    def test_the_document_has_a_section_for_every_contract_tool(self) -> None:
        """Each contract tool is specified under its own heading."""
        from src.marcus_mcp.kernel_contract import CONTRACT_TOOLS

        headings = set(
            re.findall(r"^#### `([a-z_]+)`$", _CONTRACT_DOC.read_text(), re.MULTILINE)
        )

        assert headings == set(CONTRACT_TOOLS)

    def test_the_document_names_every_tool_outside_the_door(self) -> None:
        """An operator or leaving tool missing from the document is unexplained."""
        from src.marcus_mcp.kernel_contract import (
            LEAVING_WITH_TRACK_C,
            OPERATOR_TOOLS,
        )

        text = _CONTRACT_DOC.read_text()
        missing = sorted(
            name
            for name in OPERATOR_TOOLS | LEAVING_WITH_TRACK_C
            if f"`{name}`" not in text
        )

        assert missing == []
