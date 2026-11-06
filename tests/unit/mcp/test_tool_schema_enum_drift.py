"""General guard against an MCP tool's advertised schema drifting
from what its implementation actually accepts.

Issue #889 (general version of #886's fix): three times now, an
accepted-value list and its advertised JSON schema were two
independent, hand-copied literals that drifted apart --
#871 (project_root), #886/#888 (add_project's provider enum, caught
by Codex because the stdio MCP transport validates a tool call's
arguments against the advertised schema BEFORE handle_tool_call ever
runs, so a value the implementation accepts can still be rejected
before reaching it), and #880 (a kind argument reaching an older
server). This file is the general guard #886's own fix proposed as a
separate issue rather than growing that PR: for every tool/field
pair below, assert the advertised enum (from
``handlers.get_tool_definitions``) and the implementation's own
accepted-value source agree.

Not every field with an enum in its schema is covered by a strict
equality check here -- see each entry's own docstring for why. A few
fields are deliberately open on the implementation side (the code
accepts values beyond the schema's enum, by design, already reviewed
elsewhere); for those, the meaningful invariant runs the other
direction (the schema must not omit a value the code treats
specially), not equality.

``add_project``'s ``provider`` field is covered by its own dedicated
test (``test_add_project_sqlite.py::
test_advertised_schema_accepts_every_provider_the_implementation_does``),
added alongside #886/#888's fix, not duplicated here.
"""

import pytest

pytestmark = pytest.mark.unit


def _advertised_enum(tool_name: str, field_name: str) -> set:
    """Return the real advertised enum for one tool/field pair.

    Checks every role's tool listing (a field can be agent-only,
    human-only, or both) and returns the first match -- the roles
    that do carry the tool always advertise the same schema today, so
    this does not need to check that every role's copy agrees (if
    that ever becomes false, it's a different bug than this file
    guards against).
    """
    from src.marcus_mcp.handlers import get_tool_definitions

    for role in ("agent", "human"):
        for tool in get_tool_definitions(role):
            if tool.name != tool_name:
                continue
            enum = tool.inputSchema["properties"][field_name].get("enum")
            if enum is not None:
                return set(enum)
    raise AssertionError(f"{tool_name}.{field_name} not found in any role's schema")


class TestExactMatch:
    """Fields where the implementation validates against a real,
    closed, importable source of truth -- the advertised enum must
    equal it exactly."""

    def test_register_agent_principal(self) -> None:
        from src.marcus_mcp.tools.agent import VALID_PRINCIPALS

        assert _advertised_enum("register_agent", "principal") == set(VALID_PRINCIPALS)

    def test_request_task_redo_defect_type(self) -> None:
        from src.core.models import DefectType

        assert _advertised_enum("request_task_redo", "defect_type") == {
            dt.value for dt in DefectType
        }

    def test_log_artifact_artifact_type(self) -> None:
        from src.marcus_mcp.tools.attachment import ARTIFACT_PATHS

        assert _advertised_enum("log_artifact", "artifact_type") == set(
            ARTIFACT_PATHS.keys()
        )


class TestMustContainAtLeast:
    """Fields the implementation deliberately leaves open (it accepts
    values beyond the schema's enum, by design, already reviewed) --
    equality would misrepresent that design. The invariant that
    actually matters here runs the other direction: every value the
    implementation treats specially must be representable on the
    wire, or the schema's own validation (the same layer that
    rejected #886's provider="sqlite" before the handler ran) would
    make that value unreachable through a real tool call, silently
    contradicting the implementation's own "accepts more than this"
    intent."""

    def test_report_blocker_severity_covers_the_advisory_values(self) -> None:
        """``report_blocker``'s severity is intentionally unrestricted
        on the wire (Codex P2, already reviewed): anything not in
        ``ADVISORY_SEVERITIES`` is treated as terminal/escalated by
        default, a deliberate fail-safe. The schema's enum must at
        least cover ADVISORY_SEVERITIES, or those specific values
        could never reach the handler through a real tool call."""
        from src.marcus_mcp.tools.task import ADVISORY_SEVERITIES

        assert ADVISORY_SEVERITIES <= _advertised_enum("report_blocker", "severity")


# add_feature's integration_point is not covered here: the
# implementation only special-cases "auto_detect" (everything else is
# stored as a free-form phase string, with no closed list anywhere to
# compare the schema against) -- there is no implementation-side
# source of truth this file could assert equality or containment
# against without inventing one that doesn't reflect real code.
