"""
Unit tests: ``create_project`` advertises the seed-spec and K2 options it accepts.

Issue #911: an extension seeding over MCP discovers what it may send from
the tool's advertised schema (``src/marcus_mcp/handlers.py``). Before this
change ``options.tasks`` was accepted but never advertised, and the three
typed-chain fields did not exist. This file keeps the advertised seed-spec
item in step with the validator's own allowed keys
(``src/integrations/seeded_tasks.py``), the same drift guard
``test_tool_schema_enum_drift.py`` applies to enums.
"""

from typing import Any, Dict

import pytest

pytestmark = pytest.mark.unit


def _create_project_options(role: str) -> Dict[str, Any]:
    from src.marcus_mcp.handlers import get_tool_definitions

    for tool in get_tool_definitions(role):
        if tool.name == "create_project":
            return dict(tool.inputSchema["properties"]["options"]["properties"])
    raise AssertionError(f"create_project not in the {role} tool listing")


class TestSeedSpecIsAdvertised:
    """Both roles' copies advertise the seed spec and the K2 declaration."""

    @pytest.mark.parametrize("role", ["agent", "human"])
    def test_tasks_items_match_the_validators_allowed_keys(self, role: str) -> None:
        """The advertised item properties are exactly the keys the validator accepts."""
        from src.integrations.seeded_tasks import _ALLOWED_KEYS

        tasks = _create_project_options(role)["tasks"]
        assert tasks["type"] == "array"
        assert set(tasks["items"]["properties"]) == set(_ALLOWED_KEYS)
        assert tasks["items"]["required"] == ["name"]

    @pytest.mark.parametrize("role", ["agent", "human"])
    def test_the_typed_chain_fields_are_typed_on_the_wire(self, role: str) -> None:
        """task_type is a string, inputs an object, id a string."""
        items = _create_project_options(role)["tasks"]["items"]["properties"]
        assert items["task_type"] == {"type": "string"}
        assert items["inputs"] == {"type": "object"}
        assert items["id"] == {"type": "string"}

    @pytest.mark.parametrize("role", ["agent", "human"])
    def test_task_type_registrations_is_a_mapping_of_objects(self, role: str) -> None:
        """The K2 declaration is an object whose values are objects."""
        declaration = _create_project_options(role)["task_type_registrations"]
        assert declaration["type"] == "object"
        assert declaration["additionalProperties"] == {"type": "object"}
