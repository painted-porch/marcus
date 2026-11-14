"""
Unit tests for the seeded-board task builder (``src/integrations/seeded_tasks.py``).

A seed spec is the exact task list a live check wants on the board. The
builder must turn it into Task objects the board creator accepts (synthetic
ids, name-resolved dependencies, ``source_type == "seeded"``) and refuse
anything malformed with every problem named at once.
"""

from datetime import datetime, timezone

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.seeded_tasks import (
    SEEDED_SOURCE_TYPE,
    SeedTaskError,
    build_seeded_tasks,
    validate_seed_specs,
)

pytestmark = pytest.mark.unit


def _spec(**overrides):
    spec = {"name": "Implement add_one_plus_one", "labels": ["implement"]}
    spec.update(overrides)
    return spec


def _board_task(task_id: str, name: str, status: TaskStatus = TaskStatus.TODO) -> Task:
    """A minimal existing board task for #841's resolver tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=name,
        status=status,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
    )


class TestBuildSeededTasks:
    """Valid specs become board-ready tasks."""

    def test_one_task_with_defaults(self) -> None:
        """Only a name is required; the rest take sensible defaults."""
        (task,) = build_seeded_tasks([{"name": "Write the README"}], "demo")

        assert task.id == "seed-1-write-the-readme"
        assert task.name == "Write the README"
        assert task.description == "Write the README"
        assert task.status == TaskStatus.TODO
        assert task.priority == Priority.MEDIUM
        assert task.estimated_hours == 1.0
        assert task.labels == []
        assert task.dependencies == []
        assert task.completion_criteria is None
        assert task.source_type == SEEDED_SOURCE_TYPE
        assert task.project_name == "demo"

    def test_dependencies_resolve_by_name_to_synthetic_ids(self) -> None:
        """depends_on names map to the synthetic ids the board remaps later."""
        tasks = build_seeded_tasks(
            [
                _spec(name="Design the API"),
                _spec(name="Implement the API", depends_on=["Design the API"]),
                _spec(
                    name="Test the API",
                    depends_on=["Design the API", "Implement the API"],
                ),
            ],
            "demo",
        )
        by_name = {t.name: t for t in tasks}
        assert by_name["Implement the API"].dependencies == [
            by_name["Design the API"].id
        ]
        assert by_name["Test the API"].dependencies == [
            by_name["Design the API"].id,
            by_name["Implement the API"].id,
        ]

    def test_all_fields_are_carried(self) -> None:
        """Labels, criteria, hours, priority and description are kept as given."""
        (task,) = build_seeded_tasks(
            [
                _spec(
                    description="Return 1 + 1.",
                    labels=["implement", "python"],
                    completion_criteria=["add_one_plus_one() returns 2"],
                    estimated_hours=0.5,
                    priority="high",
                )
            ],
            "demo",
        )
        assert task.description == "Return 1 + 1."
        assert task.labels == ["implement", "python"]
        assert task.completion_criteria == ["add_one_plus_one() returns 2"]
        assert task.estimated_hours == 0.5
        assert task.priority == Priority.HIGH


class TestValidateSeedSpecs:
    """Malformed specs are refused with every problem named."""

    def test_empty_or_non_list_is_refused(self) -> None:
        """The option must be a non-empty list."""
        assert validate_seed_specs([]) == [
            "tasks must be a non-empty list of task mappings"
        ]
        assert validate_seed_specs("nope")[0].startswith("tasks must be")

    def test_every_problem_is_reported_at_once(self) -> None:
        """Duplicate names, unknown dependencies, bad hours and priority all appear."""
        errors = validate_seed_specs(
            [
                {"name": "A", "estimated_hours": 0, "priority": "urgent-ish"},
                {"name": "A", "depends_on": ["Z"], "labels": ["ok", ""]},
                {"name": "B", "depends_on": ["B"], "extra": 1},
            ]
        )
        joined = "\n".join(errors)
        assert "repeats the name 'A'" in joined
        assert "unknown task 'Z'" in joined
        assert "estimated_hours must be a positive number" in joined
        assert "priority must be one of" in joined
        assert "labels must be a list of non-empty strings" in joined
        assert "depends on itself" in joined
        assert "unknown keys: extra" in joined

    def test_builder_raises_with_the_problems(self) -> None:
        """build_seeded_tasks surfaces validation as SeedTaskError."""
        with pytest.raises(SeedTaskError, match="needs a non-empty name"):
            build_seeded_tasks([{"name": "  "}], "demo")


class TestValidatorEdges:
    """Codex P2s on PR #787: malformed types and cycles are reported, never raised."""

    def test_non_list_depends_on_is_one_error_not_a_type_error(self) -> None:
        """``depends_on: 7`` yields the list-type problem and nothing crashes."""
        errors = validate_seed_specs([{"name": "A", "depends_on": 7}])
        assert errors == ["tasks[0].depends_on must be a list of non-empty strings"]

    def test_dependency_cycle_is_rejected(self) -> None:
        """A -> B -> A is named as a cycle (the board's auto-fixer no longer runs)."""
        errors = validate_seed_specs(
            [
                {"name": "A", "depends_on": ["B"]},
                {"name": "B", "depends_on": ["A"]},
            ]
        )
        assert any(e.startswith("tasks form a dependency cycle") for e in errors)

    def test_a_chain_is_not_a_cycle(self) -> None:
        """A -> B -> C validates."""
        assert (
            validate_seed_specs(
                [
                    {"name": "A"},
                    {"name": "B", "depends_on": ["A"]},
                    {"name": "C", "depends_on": ["B", "A"]},
                ]
            )
            == []
        )

    def test_unhashable_priority_is_one_error_not_a_type_error(self) -> None:
        """``priority: []`` is reported like any other bad priority."""
        errors = validate_seed_specs([{"name": "A", "priority": []}])
        assert len(errors) == 1 and errors[0].startswith(
            "tasks[0].priority must be one of"
        )
        errors = validate_seed_specs([{"name": "A", "priority": {"x": 1}}])
        assert len(errors) == 1 and "priority must be one of" in errors[0]


class TestDependsOnAnExistingBoardTask:
    """Issue #841, ADR-0013 K10 accepted narrowly 2026-11-06: a runtime

    task may depend on a task already on the board, by name or by
    id, scoped to the caller's own project (the caller is responsible
    for passing only that project's tasks as ``existing_tasks``).
    In-batch names are always tried first; the board is only
    consulted for a name the batch does not have.
    """

    def test_an_in_batch_name_still_wins_over_a_same_named_board_task(self) -> None:
        """In-batch resolution is unchanged and takes priority."""
        existing = [_board_task("board-1", "Wall")]

        wall, torches = build_seeded_tasks(
            [{"name": "Wall"}, {"name": "Torches", "depends_on": ["Wall"]}],
            "camp",
            existing_tasks=existing,
        )

        assert torches.dependencies == [wall.id]
        assert wall.id != "board-1"

    def test_a_board_task_is_found_by_exact_name(self) -> None:
        existing = [_board_task("board-1", "Raise the north wall")]

        (torches,) = build_seeded_tasks(
            [{"name": "Torches", "depends_on": ["Raise the north wall"]}],
            "camp",
            existing_tasks=existing,
        )

        assert torches.dependencies == ["board-1"]

    def test_a_board_task_is_found_by_id(self) -> None:
        existing = [_board_task("board-1", "Raise the north wall")]

        (torches,) = build_seeded_tasks(
            [{"name": "Torches", "depends_on": ["board-1"]}],
            "camp",
            existing_tasks=existing,
        )

        assert torches.dependencies == ["board-1"]

    def test_unknown_name_is_rejected_naming_both_places_checked(self) -> None:
        errors = validate_seed_specs(
            [{"name": "Torches", "depends_on": ["No such task"]}],
            existing_tasks=[_board_task("board-1", "Wall")],
        )

        assert len(errors) == 1
        assert "not in this list and not on the board" in errors[0]
        assert "No such task" in errors[0]

    def test_an_ambiguous_board_name_is_refused_not_guessed(self) -> None:
        """Two board tasks sharing a name: refuse, naming both ids,

        never picking one (the Minecraft planner's own ask: it would
        rather drop a dependency than attach it to the wrong wall)."""
        existing = [
            _board_task("board-1", "Wall"),
            _board_task("board-2", "Wall"),
        ]

        errors = validate_seed_specs(
            [{"name": "Torches", "depends_on": ["Wall"]}],
            existing_tasks=existing,
        )

        assert len(errors) == 1
        assert "matches" in errors[0]
        assert "board-1" in errors[0]
        assert "board-2" in errors[0]

    def test_an_ambiguous_name_resolves_cleanly_by_id_instead(self) -> None:
        """The same ambiguous name, named by id, is unambiguous."""
        existing = [
            _board_task("board-1", "Wall"),
            _board_task("board-2", "Wall"),
        ]

        (torches,) = build_seeded_tasks(
            [{"name": "Torches", "depends_on": ["board-2"]}],
            "camp",
            existing_tasks=existing,
        )

        assert torches.dependencies == ["board-2"]

    def test_no_existing_tasks_behaves_exactly_as_before_841(self) -> None:
        """Omitting existing_tasks keeps the pre-#841 in-batch-only rule."""
        errors = validate_seed_specs([{"name": "Torches", "depends_on": ["Wall"]}])

        assert len(errors) == 1
        assert "not in this list and not on the board" in errors[0]

    def test_depending_on_a_leased_board_task_is_allowed(self) -> None:
        """K10's rule is never REWIRE a leased task; depending ON one

        declares a new edge INTO it and touches nothing about the
        leased task itself, so it is within the rule as written."""
        existing = [_board_task("board-1", "Wall", status=TaskStatus.IN_PROGRESS)]

        (torches,) = build_seeded_tasks(
            [{"name": "Torches", "depends_on": ["Wall"]}],
            "camp",
            existing_tasks=existing,
        )

        assert torches.dependencies == ["board-1"]

    @pytest.mark.parametrize(
        "status",
        [
            TaskStatus.TODO,
            TaskStatus.IN_PROGRESS,
            TaskStatus.DONE,
            TaskStatus.BLOCKED,
            TaskStatus.DECLINED,
        ],
    )
    def test_the_targets_status_never_matters(self, status) -> None:
        """Confirmed with the Minecraft planner session: a dependency

        on a DONE task is already satisfied; on BLOCKED/DECLINED the
        new task is simply unclaimable until that changes, which is
        K10's own "add and mark unclaimable" rule working as
        intended -- not a reason to refuse the dependency itself."""
        existing = [_board_task("board-1", "Wall", status=status)]

        (torches,) = build_seeded_tasks(
            [{"name": "Torches", "depends_on": ["Wall"]}],
            "camp",
            existing_tasks=existing,
        )

        assert torches.dependencies == ["board-1"]


class TestTypedSeedSpecs:
    """Issue #911: a seed spec may carry ``task_type``, ``inputs`` and ``id``.

    These are the three fields an extension seeding a typed chain over
    MCP needs and could not send before: every seeded card was an
    ``implement`` card with no inputs and a board-minted id.
    """

    def test_task_type_and_inputs_are_carried(self) -> None:
        """The builder stamps the type and the inputs on the Task as given."""
        (task,) = build_seeded_tasks(
            [
                _spec(
                    name="Find the source of tow-0001",
                    task_type="find",
                    inputs={"case_id": "tow-0001", "query": "who said it"},
                )
            ],
            "demo",
        )
        assert task.task_type == "find"
        assert task.inputs == {"case_id": "tow-0001", "query": "who said it"}

    def test_defaults_are_unchanged_when_the_fields_are_absent(self) -> None:
        """A spec without the new keys builds exactly as before #911."""
        (task,) = build_seeded_tasks([_spec()], "demo")
        assert task.task_type == "implement"
        assert task.inputs == {}
        assert task.id.startswith("seed-1-")
        assert task.source_context == {"seeded": True}

    def test_a_caller_supplied_id_is_the_tasks_id(self) -> None:
        """The id the caller chose is the Task's id and is flagged as caller-supplied."""
        (task,) = build_seeded_tasks([_spec(id="tow-0001-find")], "demo")
        assert task.id == "tow-0001-find"
        assert task.source_context == {"seeded": True, "caller_id": True}

    def test_dependencies_resolve_to_caller_supplied_ids(self) -> None:
        """A depends_on by name resolves to the dependency's caller-chosen id."""
        find, verify = build_seeded_tasks(
            [
                _spec(name="Find", id="c1-find", task_type="find"),
                _spec(
                    name="Verify",
                    id="c1-verify",
                    task_type="verify",
                    depends_on=["Find"],
                ),
            ],
            "demo",
        )
        assert find.id == "c1-find"
        assert verify.dependencies == ["c1-find"]

    def test_depends_on_may_name_an_in_batch_task_by_its_id(self) -> None:
        """A caller that chose ids may wire dependencies by those ids too."""
        _, verify = build_seeded_tasks(
            [
                _spec(name="Find", id="c1-find"),
                _spec(name="Verify", id="c1-verify", depends_on=["c1-find"]),
            ],
            "demo",
        )
        assert verify.dependencies == ["c1-find"]

    def test_malformed_typed_fields_are_each_one_named_problem(self) -> None:
        """Bad type, bad inputs, bad id charset and a repeated id all appear."""
        errors = validate_seed_specs(
            [
                {"name": "A", "task_type": "", "inputs": ["not", "a", "mapping"]},
                {"name": "B", "id": "has spaces"},
                {"name": "C", "id": "dup"},
                {"name": "D", "id": "dup"},
            ]
        )
        joined = "\n".join(errors)
        assert "tasks[0].task_type must be a non-empty string" in joined
        assert "tasks[0].inputs must be a mapping" in joined
        assert "tasks[1].id must match [A-Za-z0-9_-]" in joined
        assert "tasks[3] repeats the id 'dup'" in joined

    def test_an_id_already_on_the_board_is_refused(self) -> None:
        """The board would refuse the duplicate at creation; name it up front instead."""
        errors = validate_seed_specs(
            [{"name": "A", "id": "taken"}],
            existing_tasks=[_board_task("taken", "Older task")],
        )
        assert errors == ["tasks[0].id 'taken' already identifies a board task"]
