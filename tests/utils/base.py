"""
Base test classes with common utilities for Marcus tests.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union, cast
from unittest.mock import AsyncMock, Mock

from src.core.models import Priority, Task, TaskStatus, WorkerStatus


class BaseTestCase:
    """
    Base test class with common utilities and helper methods.

    Provides common test setup, teardown, and utility methods
    that can be inherited by test classes.
    """

    test_dir: str

    @classmethod
    def setup_class(cls) -> None:
        """Set up test class - runs once per test class."""
        cls.test_dir = tempfile.mkdtemp(prefix="marcus_test_")

    @classmethod
    def teardown_class(cls) -> None:
        """Clean up after test class - runs once after all tests."""
        import shutil

        if hasattr(cls, "test_dir") and os.path.exists(cls.test_dir):
            shutil.rmtree(cls.test_dir)

    def setup_method(self) -> None:
        """Set up before each test method."""
        self.test_start_time = datetime.now(timezone.utc)

    def teardown_method(self) -> None:
        """Clean up after each test method."""
        pass

    # Task creation helpers

    def create_sample_task(self, **kwargs: Any) -> Task:
        """
        Create a sample task with sensible defaults.

        Parameters can be overridden via kwargs.
        """
        defaults = {
            "id": "TASK-001",
            "name": "Sample Task",
            "description": "A sample task for testing",
            "status": TaskStatus.TODO,
            "priority": Priority.MEDIUM,
            "assigned_to": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "due_date": datetime.now(timezone.utc) + timedelta(days=7),
            "estimated_hours": 4.0,
            "actual_hours": 0.0,
            "dependencies": [],
            "labels": ["test"],
        }
        defaults.update(kwargs)
        return Task(
            id=cast(str, defaults["id"]),
            name=cast(str, defaults["name"]),
            description=cast(str, defaults["description"]),
            status=cast(TaskStatus, defaults["status"]),
            priority=cast(Priority, defaults["priority"]),
            assigned_to=cast(Optional[str], defaults["assigned_to"]),
            created_at=cast(datetime, defaults["created_at"]),
            updated_at=cast(datetime, defaults["updated_at"]),
            due_date=cast(Optional[datetime], defaults["due_date"]),
            estimated_hours=cast(float, defaults["estimated_hours"]),
            actual_hours=cast(float, defaults["actual_hours"]),
            dependencies=cast(List[str], defaults["dependencies"]),
            labels=cast(List[str], defaults["labels"]),
        )

    def create_task_batch(self, count: int, **kwargs: Any) -> List[Task]:
        """Create multiple tasks with sequential IDs."""
        tasks = []
        for i in range(count):
            task_kwargs = kwargs.copy()
            task_kwargs["id"] = f"TASK-{i+1:03d}"
            task_kwargs["name"] = f"Task {i+1}"
            tasks.append(self.create_sample_task(**task_kwargs))
        return tasks

    # Worker creation helpers

    def create_sample_worker(self, **kwargs: Any) -> WorkerStatus:
        """
        Create a sample worker with sensible defaults.

        Parameters can be overridden via kwargs.
        """
        defaults = {
            "worker_id": "worker-001",
            "name": "Test Worker",
            "role": "Developer",
            "email": "test@example.com",
            "current_tasks": [],
            "completed_tasks_count": 0,
            "capacity": 40,
            "skills": ["python", "testing"],
            "availability": {
                "monday": True,
                "tuesday": True,
                "wednesday": True,
                "thursday": True,
                "friday": True,
                "saturday": False,
                "sunday": False,
            },
            "performance_score": 1.0,
        }
        defaults.update(kwargs)
        return WorkerStatus(
            worker_id=cast(str, defaults["worker_id"]),
            name=cast(str, defaults["name"]),
            role=cast(str, defaults["role"]),
            email=cast(Optional[str], defaults["email"]),
            current_tasks=cast(List[Task], defaults["current_tasks"]),
            completed_tasks_count=cast(int, defaults["completed_tasks_count"]),
            capacity=cast(int, defaults["capacity"]),
            skills=cast(List[str], defaults["skills"]),
            availability=cast(Dict[str, bool], defaults["availability"]),
            performance_score=cast(float, defaults["performance_score"]),
        )

    # Assertion helpers

    def assert_task_valid(self, task: Task) -> None:
        """Assert that a task has all required fields and is valid."""
        assert task.id is not None
        assert task.name is not None
        assert task.status in TaskStatus
        assert task.priority in Priority
        assert isinstance(task.estimated_hours, (int, float))
        assert task.estimated_hours >= 0
        assert task.created_at <= task.updated_at

    def assert_worker_valid(self, worker: WorkerStatus) -> None:
        """Assert that a worker has all required fields and is valid."""
        assert worker.worker_id is not None
        assert worker.name is not None
        assert worker.role is not None
        assert 0 <= worker.performance_score <= 2.0
        assert worker.capacity > 0
        assert isinstance(worker.skills, list)

    def assert_tasks_equal(
        self, task1: Task, task2: Task, ignore_timestamps: bool = True
    ) -> None:
        """Assert that two tasks are equal, optionally ignoring timestamps."""
        assert task1.id == task2.id
        assert task1.name == task2.name
        assert task1.description == task2.description
        assert task1.status == task2.status
        assert task1.priority == task2.priority
        assert task1.assigned_to == task2.assigned_to
        assert task1.estimated_hours == task2.estimated_hours

        if not ignore_timestamps:
            assert task1.created_at == task2.created_at
            assert task1.updated_at == task2.updated_at

    # Mock creation helpers

    def create_mock_config(self) -> Any:
        """Create a properly configured mock MarcusConfig object."""
        from src.config.marcus_config import (
            AISettings,
            FeaturesSettings,
            KanbanSettings,
            MarcusConfig,
            MCPSettings,
            MemorySettings,
            TaskLeaseSettings,
            TransportSettings,
        )

        return MarcusConfig(
            mcp=MCPSettings(),
            ai=AISettings(
                provider="anthropic",
                model="claude-3-haiku-20240307",
                enabled=False,  # Disable AI in tests by default
            ),
            kanban=KanbanSettings(
                provider="planka",
                board_name="Test Board",
            ),
            features=FeaturesSettings(events=False, context=False, memory=False),
            memory=MemorySettings(),
            transport=TransportSettings(type="stdio"),
            task_lease=TaskLeaseSettings(),
            single_project_mode=True,
            log_level="ERROR",  # Reduce log noise in tests
        )

    def create_mock_kanban_client(self) -> AsyncMock:
        """Create a properly configured mock kanban client."""
        client = AsyncMock()
        client.get_available_tasks = AsyncMock(return_value=[])
        client.get_all_tasks = AsyncMock(return_value=[])
        client.get_task_by_id = AsyncMock(return_value=None)
        client.update_task = AsyncMock()
        client.create_task = AsyncMock()
        client.add_comment = AsyncMock()
        client.get_board_summary = AsyncMock(
            return_value={
                "totalCards": 0,
                "doneCount": 0,
                "inProgressCount": 0,
                "backlogCount": 0,
            }
        )
        client.update_task_progress = AsyncMock()
        # Issue #884 group B: the real _load_workspace_state
        # (src/integrations/providers/sqlite_kanban.py) is a plain,
        # synchronous method -- request_next_task calls it with no
        # await. The base AsyncMock() this client is built from
        # returns a coroutine for any attribute not explicitly
        # overridden, so an unawaited call here raised "argument of
        # type 'coroutine' is not iterable" instead of reading a
        # workspace state dict. A plain Mock matches the real,
        # synchronous call shape.
        client._load_workspace_state = Mock(return_value=None)
        # Issue #884 group C: refresh_project_state reads
        # self.kanban_client.board_id as a plain attribute
        # (src/marcus_mcp/server.py, getattr(..., "board_id",
        # "unknown")) and logs it via json.dumps. Several callers of
        # this helper never set board_id afterward, so it stayed an
        # auto-created AsyncMock attribute -- a Mock object, not a
        # string -- and broke that json.dumps with "Object of type
        # AsyncMock is not JSON serializable" the moment any tool
        # call triggered a state refresh. A plain string default,
        # overridable per test same as before.
        client.board_id = "test-board-123"
        return client

    def create_mock_ai_engine(self) -> AsyncMock:
        """Create a properly configured mock AI engine."""
        engine = AsyncMock()
        engine.match_task_to_agent = AsyncMock(
            return_value={
                "agent_id": "worker-001",
                "confidence": 0.95,
                "reasoning": "Skills match task requirements",
            }
        )
        engine.generate_task_instructions = AsyncMock(
            return_value="1. Implement the feature\n2. Write tests\n3. Update documentation"
        )
        engine.analyze_blocker = AsyncMock(
            return_value={
                "severity": "medium",
                "suggested_actions": ["Check dependencies", "Review logs"],
                "needs_escalation": False,
            }
        )
        return engine

    # File and directory helpers

    def create_test_file(self, filename: str, content: str = "") -> str:
        """Create a test file in the test directory."""
        filepath = os.path.join(self.__class__.test_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(content)
        return filepath

    def read_test_file(self, filename: str) -> str:
        """Read content from a test file."""
        filepath = os.path.join(self.__class__.test_dir, filename)
        with open(filepath, "r") as f:
            return f.read()

    # JSON helpers

    def assert_json_equal(
        self,
        json1: Dict[str, Any],
        json2: Dict[str, Any],
        ignore_keys: Optional[List[str]] = None,
    ) -> None:
        """
        Assert two JSON objects are equal, optionally ignoring certain keys.

        Parameters
        ----------
        json1 : dict
            First JSON object
        json2 : dict
            Second JSON object
        ignore_keys : list, optional
            List of keys to ignore in comparison
        """
        if ignore_keys:
            json1 = {k: v for k, v in json1.items() if k not in ignore_keys}
            json2 = {k: v for k, v in json2.items() if k not in ignore_keys}

        assert json1 == json2
