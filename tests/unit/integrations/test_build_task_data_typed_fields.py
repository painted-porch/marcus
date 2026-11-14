"""
Unit tests: ``TaskBuilder.build_task_data`` carries the typed-chain fields.

Issue #911: a seeded card may now carry ``task_type``, ``inputs`` and a
caller-supplied ``id`` (``src/integrations/seeded_tasks.py``). Those three
only reach the board if the Task-to-``task_data`` conversion in
``src/integrations/nlp_task_utils.py`` passes them on; before this change
it dropped all three, so every seeded card landed as an ``implement`` card
with no inputs and a board-minted id (the same silent-drop shape #608 and
#611 hit with ``completion_criteria``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.nlp_task_utils import TaskBuilder

pytestmark = pytest.mark.unit


def _task(**overrides: Any) -> Task:
    now = datetime.now(timezone.utc)
    fields: Dict[str, Any] = dict(
        id="seed-1-find",
        name="Find the source",
        description="Find it.",
        status=TaskStatus.TODO,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=0.1,
    )
    fields.update(overrides)
    return Task(**fields)


class TestTypedFieldsReachTheBoard:
    """task_type and inputs are always passed; id only when the caller chose it."""

    def test_task_type_and_inputs_are_in_the_result(self) -> None:
        """A typed task's type and inputs survive the conversion."""
        data = TaskBuilder.build_task_data(
            _task(task_type="find", inputs={"case_id": "c1", "query": "q"})
        )
        assert data["task_type"] == "find"
        assert data["inputs"] == {"case_id": "c1", "query": "q"}

    def test_defaults_are_the_providers_defaults(self) -> None:
        """An untyped task converts to the same card the provider minted before."""
        data = TaskBuilder.build_task_data(_task())
        assert data["task_type"] == "implement"
        assert data["inputs"] == {}
        assert "id" not in data

    def test_a_caller_supplied_seeded_id_is_passed_as_the_cards_id(self) -> None:
        """Seeded with ``caller_id``: the Task's id becomes ``task_data["id"]``."""
        data = TaskBuilder.build_task_data(
            _task(
                id="tow-0001-find",
                source_type="seeded",
                source_context={"seeded": True, "caller_id": True},
            )
        )
        assert data["id"] == "tow-0001-find"
        assert data["original_id"] == "tow-0001-find"

    def test_a_synthetic_seeded_id_is_not_passed(self) -> None:
        """Seeded without a caller id: the board still mints the id, as before."""
        data = TaskBuilder.build_task_data(
            _task(source_type="seeded", source_context={"seeded": True})
        )
        assert "id" not in data

    def test_a_decomposed_tasks_id_is_never_passed(self) -> None:
        """Decomposer ids are placeholders for the remap; the board must mint real ones."""
        data = TaskBuilder.build_task_data(
            _task(
                id="task_1",
                source_type="nlp_project",
                source_context={"caller_id": True},
            )
        )
        assert "id" not in data
