"""
Unit tests for cached task classification on the assignment path (#743).

``EnhancedTaskClassifier.classify`` scores a task against every task
type by regex and keyword over the task's full text, which is slow for
tasks that carry long descriptions (the Tow experiment embeds excerpts
and engine answers in task text). The request path classified every
available task on every ``request_next_task`` call, and a task's type
never changes after it is created, so all but the first classification
of a given task was wasted work. On a 620-task board this made one
request take minutes and froze the single-threaded server.

``_classify_task_cached`` memoizes the result by task content so a task
is classified at most once per cache, while still reclassifying a task
whose name, description, or labels actually changed.
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.nlp_task_utils import TaskType
from src.marcus_mcp.tools.task import _classify_task_cached

pytestmark = pytest.mark.unit


def _task(task_id: str, name: str, description: str = "", labels=None) -> Task:
    """Build a minimal Task for classification tests."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description=description,
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.0,
        labels=labels or [],
        dependencies=[],
    )


class TestClassifyTaskCached:
    """Test suite for the memoized classification helper."""

    def test_same_task_classified_once(self) -> None:
        """Repeated calls for one task hit the classifier only once."""
        classifier = Mock()
        classifier.classify = Mock(return_value=TaskType.IMPLEMENTATION)
        cache: dict = {}
        task = _task("t1", "Implement login", "long text " * 500)

        first = _classify_task_cached(task, classifier, cache)
        second = _classify_task_cached(task, classifier, cache)

        assert first is second is TaskType.IMPLEMENTATION
        assert classifier.classify.call_count == 1

    def test_distinct_tasks_each_classified(self) -> None:
        """Two different tasks are each classified once."""
        classifier = Mock()
        classifier.classify = Mock(return_value=TaskType.TESTING)
        cache: dict = {}

        _classify_task_cached(_task("t1", "Verify claim A"), classifier, cache)
        _classify_task_cached(_task("t2", "Verify claim B"), classifier, cache)

        assert classifier.classify.call_count == 2

    def test_changed_content_reclassifies_and_replaces(self) -> None:
        """An edited task is reclassified, and its entry is replaced, not added.

        The cache is keyed by task id, so a task whose text changed must
        reclassify AND leave the cache with a single entry for that id,
        so a long-lived server cannot accumulate one entry per revision
        (Codex review on PR #744).
        """
        classifier = Mock()
        classifier.classify = Mock(return_value=TaskType.DESIGN)
        cache: dict = {}

        _classify_task_cached(_task("t1", "Design the schema"), classifier, cache)
        # Same id, different name: the content signature changed.
        _classify_task_cached(_task("t1", "Implement the schema"), classifier, cache)

        assert classifier.classify.call_count == 2
        # Bounded: one entry per task id, the old revision is overwritten.
        assert list(cache.keys()) == ["t1"]
