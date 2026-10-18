"""
Unit tests for scaffold anchoring (issue #659).

When Marcus generates a project scaffold pre-fork, each implementation
task receives a placeholder file at a canonical path (e.g.,
``src/core/gameEngine.js`` for the "Implement Game Core Engine" task).
Without an explicit anchor in the agent's task instructions, agents
sometimes invent sibling paths (the ``src/game/gameEngine.js`` orphan
observed in ``snake-baton-1``), leaving the scaffold as dead code on
disk and creating import ambiguity downstream.

These tests verify two behaviors:

1. ``_generate_project_scaffold`` returns a ``{task_name → path}``
   mapping for every placeholder the LLM bound to an implementation
   task. Config files (manifests, entry points, .gitignore) are
   absent from the mapping because they have no owning task.

2. ``build_tiered_instructions`` surfaces the persisted
   ``scaffold_path`` from ``task.source_context`` in the
   IMPLEMENTATION FILE section of the agent's task instructions.
"""

import json
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest

from src.core.models import (
    Priority,
    Task,
    TaskStatus,
)
from src.integrations.nlp_tools import _generate_project_scaffold
from src.marcus_mcp.tools.task import build_tiered_instructions

pytestmark = pytest.mark.unit


def _make_task(name: str, source_context: Optional[dict[str, Any]] = None) -> Task:
    """Construct a minimal Task instance for the tests below."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Task(
        id=f"task_{name.lower().replace(' ', '_')}",
        name=name,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=2.0,
        labels=["implementation"],
        source_context=source_context or {},
        responsibility="implements " + name,
    )


class TestScaffoldReturnsAnchorMapping:
    """``_generate_project_scaffold`` returns ``task_name → scaffold_path``."""

    @pytest.mark.asyncio
    async def test_returns_mapping_for_each_bound_placeholder(self, tmp_path) -> None:
        """Each placeholder with a valid ``task_name`` lands in the mapping.

        Mirrors the snake-baton-1 shape: two implementation tasks, one
        placeholder per task. The LLM is mocked to emit the canonical
        JSON the prompt asks for.
        """
        tasks = [
            _make_task("Implement Game Core Engine"),
            _make_task("Implement Game Presentation and Feedback System"),
        ]
        design_content = {
            "Design Game Core Engine": {
                "artifacts": [
                    {
                        "artifact_type": "architecture",
                        "content": "# arch doc",
                    }
                ]
            }
        }
        llm_response = json.dumps(
            [
                {"path": "package.json", "content": "{}"},
                {
                    "path": "src/core/gameEngine.js",
                    "content": "// game engine placeholder",
                    "task_name": "Implement Game Core Engine",
                },
                {
                    "path": "src/presentation/renderer.js",
                    "content": "// renderer placeholder",
                    "task_name": "Implement Game Presentation and Feedback System",
                },
            ]
        )

        with patch(
            "src.ai.providers.llm_abstraction.LLMAbstraction.analyze",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            success, task_to_path = await _generate_project_scaffold(
                tasks=tasks,
                project_description="Snake game in vanilla JS",
                project_name="snake",
                project_root=str(tmp_path),
                design_content=design_content,
            )

        assert success is True
        assert task_to_path == {
            "Implement Game Core Engine": "src/core/gameEngine.js",
            "Implement Game Presentation and Feedback System": (
                "src/presentation/renderer.js"
            ),
        }

    @pytest.mark.asyncio
    async def test_config_files_excluded_from_mapping(self, tmp_path) -> None:
        """Files without a ``task_name`` (configs / entry points) skip the mapping.

        ``package.json``, ``vite.config.js``, ``index.html``, and the
        entry-point ``main.js`` are shared infrastructure — no single
        task owns them, so they MUST NOT appear in the anchor mapping.
        """
        tasks = [_make_task("Implement Game Core Engine")]
        design_content = {
            "Design Game Core Engine": {
                "artifacts": [{"artifact_type": "architecture", "content": "doc"}]
            }
        }
        llm_response = json.dumps(
            [
                {"path": "package.json", "content": "{}"},
                {"path": "vite.config.js", "content": "export default {}"},
                {"path": "index.html", "content": "<!doctype html>"},
                {"path": "src/main.js", "content": "// entry"},
                {
                    "path": "src/core/gameEngine.js",
                    "content": "// engine",
                    "task_name": "Implement Game Core Engine",
                },
            ]
        )

        with patch(
            "src.ai.providers.llm_abstraction.LLMAbstraction.analyze",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            _, task_to_path = await _generate_project_scaffold(
                tasks=tasks,
                project_description="snake",
                project_name="snake",
                project_root=str(tmp_path),
                design_content=design_content,
            )

        # Only the explicitly-bound placeholder appears.
        assert task_to_path == {"Implement Game Core Engine": "src/core/gameEngine.js"}

    @pytest.mark.asyncio
    async def test_unknown_task_name_is_ignored(self, tmp_path) -> None:
        """A ``task_name`` not matching any impl task is silently dropped.

        Defensive against LLM hallucination — if the model emits a
        ``task_name`` that doesn't correspond to a real task, we'd
        rather write the scaffold file and skip the anchor than write
        a phantom anchor that misleads downstream code.
        """
        tasks = [_make_task("Implement Game Core Engine")]
        design_content = {
            "Design X": {
                "artifacts": [{"artifact_type": "architecture", "content": "doc"}]
            }
        }
        llm_response = json.dumps(
            [
                {
                    "path": "src/foo.js",
                    "content": "// foo",
                    "task_name": "Implement Some Task That Does Not Exist",
                },
            ]
        )

        with patch(
            "src.ai.providers.llm_abstraction.LLMAbstraction.analyze",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            _, task_to_path = await _generate_project_scaffold(
                tasks=tasks,
                project_description="x",
                project_name="x",
                project_root=str(tmp_path),
                design_content=design_content,
            )

        assert task_to_path == {}

    @pytest.mark.asyncio
    async def test_duplicate_task_name_keeps_first(self, tmp_path) -> None:
        """When the LLM binds two files to the same task, only the first wins.

        Multiple placeholders per task is a prompt-drift symptom we
        log but don't fail on. The mapping is task → ONE path; the
        first one the writer sees stays.
        """
        tasks = [_make_task("Implement Game Core Engine")]
        design_content = {
            "Design X": {
                "artifacts": [{"artifact_type": "architecture", "content": "doc"}]
            }
        }
        llm_response = json.dumps(
            [
                {
                    "path": "src/core/gameEngine.js",
                    "content": "// first",
                    "task_name": "Implement Game Core Engine",
                },
                {
                    "path": "src/engine/index.js",
                    "content": "// second",
                    "task_name": "Implement Game Core Engine",
                },
            ]
        )

        with patch(
            "src.ai.providers.llm_abstraction.LLMAbstraction.analyze",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            _, task_to_path = await _generate_project_scaffold(
                tasks=tasks,
                project_description="x",
                project_name="x",
                project_root=str(tmp_path),
                design_content=design_content,
            )

        # First write wins; both files still land on disk, but only
        # the first is anchored.
        assert task_to_path == {"Implement Game Core Engine": "src/core/gameEngine.js"}


class TestAgentInstructionsSurfaceScaffoldPath:
    """``build_tiered_instructions`` surfaces ``scaffold_path`` to the agent."""

    def test_implementation_file_section_appears_when_path_set(self) -> None:
        """A task with ``source_context.scaffold_path`` gets an anchor section.

        The instructions must explicitly name the path so the agent
        fills the scaffold rather than picking a sibling path.
        """
        task = _make_task(
            "Implement Game Core Engine",
            source_context={
                "scaffold_path": "src/core/gameEngine.js",
                "contract_file": "docs/architecture/engine.md",
            },
        )

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert "IMPLEMENTATION FILE: src/core/gameEngine.js" in instructions
        assert "do NOT create a sibling file" in instructions

    def test_anchor_appears_for_feature_based_task_without_responsibility(
        self,
    ) -> None:
        """Feature-based tasks (no ``responsibility``) also get the anchor.

        Regression for #659: scaffolds are generated for both the
        contract-first and feature-based decomposition paths, but the
        anchor previously lived inside the contract-first
        ``if responsibility:`` block — so a feature-based agent had the
        path persisted on its task yet never saw it. The anchor is now a
        standalone layer that fires for any task with a scaffold_path.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        # Built directly (not via _make_task) so responsibility is None,
        # i.e. the contract-first layer does NOT fire.
        task = Task(
            id="task_feature_engine",
            name="Implement Game Core Engine",
            description="",
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=2.0,
            labels=["implementation"],
            source_context={"scaffold_path": "src/core/gameEngine.js"},
        )
        assert not getattr(task, "responsibility", None)

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        # Anchor present even though the contract-first layer did not fire.
        assert "IMPLEMENTATION FILE: src/core/gameEngine.js" in instructions
        assert "CONTRACT RESPONSIBILITY" not in instructions

    def test_no_anchor_section_when_path_missing(self) -> None:
        """Without ``scaffold_path``, no IMPLEMENTATION FILE section appears.

        Legacy tasks created before #659 won't have the field;
        instructions must still render cleanly without it.
        """
        task = _make_task(
            "Implement Game Core Engine",
            source_context={"contract_file": "docs/architecture/engine.md"},
        )

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert "IMPLEMENTATION FILE:" not in instructions

    def test_empty_scaffold_path_treated_as_missing(self) -> None:
        """``scaffold_path = ""`` should not render an empty anchor section."""
        task = _make_task(
            "Implement Game Core Engine",
            source_context={
                "scaffold_path": "",
                "contract_file": "docs/architecture/engine.md",
            },
        )

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert "IMPLEMENTATION FILE:" not in instructions

    def test_falls_back_to_description_marker_for_non_sqlite(self) -> None:
        """Non-SQLite providers carry the anchor via a description marker.

        Planka, GitHub, and Linear don't have a ``source_context``
        column. The anchor survives in the task description as a
        ``<!-- MARCUS_SCAFFOLD_PATH: ... -->`` marker, mirroring the
        ``MARCUS_CONTRACT_FIRST`` pattern. ``_resolve_scaffold_path``
        parses the marker as the fallback path source.
        """
        task = _make_task(
            "Implement Game Core Engine",
            source_context={
                "contract_file": "docs/architecture/engine.md",
                # No scaffold_path here — simulates a Planka-backed
                # task that never persisted the JSON column.
            },
        )
        # Description carries the marker, the way the
        # cross-provider write path leaves it.
        task.description = (
            "Build the engine.\n\n"
            "<!-- MARCUS_SCAFFOLD_PATH: src/core/gameEngine.js -->"
        )

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert "IMPLEMENTATION FILE: src/core/gameEngine.js" in instructions

    def test_source_context_wins_over_description_marker(self) -> None:
        """When both are present, ``source_context`` is the source of truth.

        SQLite carries the path in its JSON column; the description
        marker is the cross-provider fallback. If both somehow
        disagree (e.g., the description marker is stale and the JSON
        column was updated), the JSON column is authoritative for
        the IMPLEMENTATION FILE section.

        Note: the description (including its marker) appears verbatim
        in Layer 0's mandatory workflow listing. That's expected —
        the marker IS a description-borne signal in non-SQLite
        providers. What matters is which path the structured anchor
        section names.
        """
        task = _make_task(
            "Implement Game Core Engine",
            source_context={"scaffold_path": "src/core/gameEngine.js"},
        )
        task.description = "<!-- MARCUS_SCAFFOLD_PATH: src/old/stale_path.js -->"

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        # The structured anchor section names the source_context path.
        assert "IMPLEMENTATION FILE: src/core/gameEngine.js" in instructions
        # The structured section does NOT name the stale marker path.
        assert "IMPLEMENTATION FILE: src/old/stale_path.js" not in instructions

    def test_malformed_marker_does_not_render_section(self) -> None:
        """A marker without a closing ``-->`` is silently ignored."""
        task = _make_task(
            "Implement Game Core Engine",
            source_context={},
        )
        task.description = "<!-- MARCUS_SCAFFOLD_PATH: src/core/gameEngine.js"

        instructions = build_tiered_instructions(
            base_instructions="Build the engine.",
            task=task,
            context_data=None,
            dependency_awareness=None,
            predictions=None,
            state=None,
        )

        assert "IMPLEMENTATION FILE:" not in instructions
