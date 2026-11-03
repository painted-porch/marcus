"""
Unit tests: a seeded board goes through create_project without decomposition.

``create_project`` with ``options.tasks`` must put exactly the given tasks
on the board: no LLM decomposition, no safety-check additions (phase
dependencies, integration task, README task), and no subtask splitting of
the seeded tasks. Everything else the creator does is untouched.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.models import Task, TaskStatus

pytestmark = pytest.mark.unit

SEED = [
    {"name": "Implement add_one_plus_one", "labels": ["implement"]},
    {
        "name": "Test add_one_plus_one",
        "labels": ["test"],
        "depends_on": ["Implement add_one_plus_one"],
    },
]


def _creator():
    from src.integrations.nlp_tools import NaturalLanguageProjectCreator

    with (
        patch("src.integrations.nlp_tools.AdvancedPRDParser") as parser,
        patch("src.integrations.nlp_tools.BoardAnalyzer") as analyzer,
        patch("src.integrations.nlp_tools.ContextDetector") as detector,
    ):
        creator = NaturalLanguageProjectCreator(
            kanban_client=MagicMock(), ai_engine=MagicMock(), subtask_manager=None
        )
    creator.prd_parser = parser.return_value
    creator.board_analyzer = analyzer.return_value
    creator.context_detector = detector.return_value
    creator.prd_parser.parse_prd_to_tasks = AsyncMock(
        side_effect=AssertionError("decomposition must not run for a seeded board")
    )
    creator.board_analyzer.analyze_board = AsyncMock(
        side_effect=AssertionError("context detection must not run")
    )
    return creator


class TestExplicitTaskBranch:
    """process_natural_language returns the seeded tasks and calls no LLM."""

    @pytest.mark.asyncio
    async def test_returns_seeded_tasks_without_decomposing(self) -> None:
        """The exact tasks come back, in order, with resolved dependencies."""
        creator = _creator()

        tasks = await creator.process_natural_language(
            "ignored", project_name="demo", options={"tasks": SEED}
        )

        assert [t.name for t in tasks] == [
            "Implement add_one_plus_one",
            "Test add_one_plus_one",
        ]
        assert tasks[1].dependencies == [tasks[0].id]
        assert all(t.source_type == "seeded" for t in tasks)
        assert creator._actual_decomposer == "explicit"

    @pytest.mark.asyncio
    async def test_malformed_seed_raises(self) -> None:
        """A bad spec surfaces as SeedTaskError rather than a silent fallback."""
        from src.integrations.seeded_tasks import SeedTaskError

        creator = _creator()
        with pytest.raises(SeedTaskError):
            await creator.process_natural_language(
                "ignored", project_name="demo", options={"tasks": [{"name": ""}]}
            )


class TestSeededBoardIsNotAugmented:
    """The creator keeps a seeded board exactly as given."""

    @pytest.mark.asyncio
    async def test_seeded_board_gets_no_additions(self) -> None:
        """No safety checks, no integration task, no README task."""
        from src.integrations.seeded_tasks import build_seeded_tasks

        creator = _creator()
        creator._actual_decomposer = "explicit"
        creator.apply_safety_checks = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("safety checks must not run for a seeded board")
        )
        seeded = build_seeded_tasks(SEED, "demo")
        with (
            patch(
                "src.integrations.integration_verification.enhance_project_with_integration",
                side_effect=AssertionError("integration task must not be added"),
            ),
            patch(
                "src.integrations.documentation_tasks.enhance_project_with_documentation",
                side_effect=AssertionError("README task must not be added"),
            ),
        ):
            out = await creator._augment_tasks(seeded, "seeded", "demo")

        assert [t.name for t in out] == [t.name for t in seeded]

    @pytest.mark.asyncio
    async def test_decomposed_board_is_still_augmented(self) -> None:
        """The normal path is unchanged: safety checks then integration and README."""
        from src.integrations.seeded_tasks import build_seeded_tasks

        creator = _creator()
        creator._actual_decomposer = "feature_based"
        tasks = build_seeded_tasks(SEED, "demo")
        creator.apply_safety_checks = AsyncMock(return_value=list(tasks))  # type: ignore[method-assign]
        with (
            patch(
                "src.integrations.integration_verification.enhance_project_with_integration",
                side_effect=lambda ts, *a, **k: ts + ["integration"],
            ) as integ,
            patch(
                "src.integrations.documentation_tasks.enhance_project_with_documentation",
                side_effect=lambda ts, *a, **k: ts + ["readme"],
            ) as docs,
        ):
            out = await creator._augment_tasks(tasks, "desc", "demo")

        creator.apply_safety_checks.assert_awaited_once()
        integ.assert_called_once()
        docs.assert_called_once()
        assert out[-2:] == ["integration", "readme"]


class TestSeededTasksAreNotSplit:
    """The subtask decomposer leaves seeded tasks alone."""

    @pytest.mark.asyncio
    async def test_decompose_skips_seeded_tasks(self) -> None:
        """A seeded task that would otherwise qualify is never decomposed."""
        from src.integrations.seeded_tasks import build_seeded_tasks

        creator = _creator()
        creator.ai_engine = MagicMock()
        (seeded,) = build_seeded_tasks(
            [{"name": "Implement a large thing", "estimated_hours": 8}], "demo"
        )
        created = Task(
            id="k1",
            name=seeded.name,
            description=seeded.description,
            status=TaskStatus.TODO,
            priority=seeded.priority,
            assigned_to=None,
            created_at=seeded.created_at,
            updated_at=seeded.updated_at,
            due_date=None,
            estimated_hours=seeded.estimated_hours,
        )
        with patch(
            "src.marcus_mcp.coordinator.decompose_task",
            new=AsyncMock(
                side_effect=AssertionError("must not decompose a seeded task")
            ),
        ):
            await creator._decompose_and_add_subtasks([created], [seeded])


class TestSeededGraphIsNotRewritten:
    """create_tasks_on_board must not auto-fix a seeded graph (Codex P2 on PR #787).

    The auto-fixer would, for example, make a lone README task depend on
    itself as the "final" task and leave it unclaimable forever.
    """

    @pytest.mark.asyncio
    async def test_validate_and_fix_is_skipped_for_seeded_tasks(self) -> None:
        """A seeded list reaches the kanban client with its graph untouched."""
        from src.integrations.seeded_tasks import build_seeded_tasks

        creator = _creator()
        seeded = build_seeded_tasks([{"name": "Write the README"}], "demo")
        created = Task(
            id="k1",
            name="Write the README",
            description="Write the README",
            status=TaskStatus.TODO,
            priority=seeded[0].priority,
            assigned_to=None,
            created_at=seeded[0].created_at,
            updated_at=seeded[0].updated_at,
            due_date=None,
            estimated_hours=1.0,
        )
        creator.kanban_client.create_task = AsyncMock(return_value=created)
        with (
            patch(
                "src.integrations.nlp_base.TaskGraphValidator.validate_and_fix",
                side_effect=AssertionError("the seeded graph must not be auto-fixed"),
            ),
            patch.object(creator, "_remap_dependencies", new=AsyncMock()),
            patch.object(
                creator, "_update_task_metadata_dependencies", new=AsyncMock()
            ),
            patch.object(creator, "_decompose_and_add_subtasks", new=AsyncMock()),
            patch.object(creator, "_wire_cross_parent_dependencies", new=AsyncMock()),
        ):
            out = await creator.create_tasks_on_board(seeded)

        assert [t.name for t in out] == ["Write the README"]
        sent = creator.kanban_client.create_task.call_args.args[0]
        assert sent.get("dependencies", []) == []
