"""
Unit tests for the seeded-board task builder (``src/integrations/seeded_tasks.py``).

A seed spec is the exact task list a live check wants on the board. The
builder must turn it into Task objects the board creator accepts (synthetic
ids, name-resolved dependencies, ``source_type == "seeded"``) and refuse
anything malformed with every problem named at once.
"""

import pytest

from src.core.models import Priority, TaskStatus
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
