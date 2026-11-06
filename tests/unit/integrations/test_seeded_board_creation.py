"""
Unit tests: a seeded board goes through create_project without decomposition.

``create_project`` with ``options.tasks`` must put exactly the given tasks
on the board: no LLM decomposition, no safety-check additions (phase
dependencies, integration task, README task), and no subtask splitting of
the seeded tasks. Everything else the creator does is untouched.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus

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


class TestRemapDependenciesTrustsAnExistingBoardTask:
    """Issue #841: a seeded task's ``depends_on`` may already be a

    real, pre-existing board task id (resolved by
    ``build_seeded_tasks`` before ``_remap_dependencies`` ever runs),
    not a slug to remap and not part of this batch. Found live:
    ``_remap_dependencies`` had no third case for this and silently
    dropped it as "orphaned", which an existing unit test never
    caught because nothing exercised a dependency outside the batch.
    """

    def _task(self, task_id, name, deps, source_type="seeded") -> Task:
        now = datetime.now(timezone.utc)
        return Task(
            id=task_id,
            name=name,
            description=name,
            status=TaskStatus.TODO,
            priority=Priority.MEDIUM,
            assigned_to=None,
            created_at=now,
            updated_at=now,
            due_date=None,
            estimated_hours=1.0,
            dependencies=list(deps),
            source_type=source_type,
        )

    @pytest.mark.asyncio
    async def test_a_pre_existing_board_dependency_is_kept_not_orphaned(self) -> None:
        creator = _creator()
        board_task_id = "board-task-wall"
        original = self._task("seed-1-torches", "Torches", deps=[board_task_id])
        created = self._task("k-77", "Torches", deps=[board_task_id])

        await creator._remap_dependencies([original], [created])

        assert created.dependencies == [board_task_id]

    @pytest.mark.asyncio
    async def test_a_genuinely_unknown_dependency_on_a_non_seeded_task_is_still_dropped(
        self,
    ) -> None:
        """The decomposed (non-seeded) path keeps its existing safety
        net: an id matching neither a slug nor the batch is a real bug
        there, not a valid cross-batch reference, so it is still
        dropped with a warning."""
        creator = _creator()
        original = self._task(
            "slug-a", "Implement", deps=["nonexistent"], source_type="implement"
        )
        created = self._task(
            "k-1", "Implement", deps=["nonexistent"], source_type="implement"
        )

        await creator._remap_dependencies([original], [created])

        assert created.dependencies == []

    @pytest.mark.asyncio
    async def test_survives_a_provider_that_echoes_back_no_source_type(self) -> None:
        """Codex P2, round 2 on PR #855: on Planka,

        ``KanbanClientWithCreate.create_task()`` returns a Task built
        by ``_card_to_task``, which never sets ``source_type`` at
        all -- so reading it from the CREATED task (what the earlier
        version of this fix did) was always None for a real Planka
        board, silently defeating the fix above for the one provider
        it most needed to work on. Provenance must come from the
        ORIGINAL task Marcus itself built, matched by name, not from
        whatever the provider echoes back."""
        creator = _creator()
        board_task_id = "board-task-wall"
        original = self._task(
            "seed-1-torches", "Torches", deps=[board_task_id], source_type="seeded"
        )
        # The provider's own round-trip: no source_type at all.
        created = self._task("k-77", "Torches", deps=[board_task_id], source_type=None)

        await creator._remap_dependencies([original], [created])

        assert created.dependencies == [board_task_id]
