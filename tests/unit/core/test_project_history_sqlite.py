"""
Unit tests for ProjectHistoryPersistence SQLite refactor.

Tests the new SQLite-based storage for decisions and artifacts.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.persistence import SQLitePersistence
from src.core.project_history import (
    ArtifactMetadata,
    Decision,
    ProjectHistoryPersistence,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create temporary database for testing."""
    return tmp_path / "test_marcus.db"


@pytest.fixture
def persistence(temp_db: Path, tmp_path: Path) -> ProjectHistoryPersistence:
    """Create ProjectHistoryPersistence with temporary database."""
    # Create a mock marcus root structure
    marcus_root = tmp_path / "marcus"
    marcus_root.mkdir()
    (marcus_root / "data").mkdir()

    # Move test db to the data directory
    test_db = marcus_root / "data" / "marcus.db"

    persistence = ProjectHistoryPersistence(marcus_root=marcus_root)
    return persistence


@pytest.fixture
async def sample_decisions(temp_db: Path) -> list[dict[str, Any]]:
    """Create sample decisions in SQLite."""
    backend = SQLitePersistence(db_path=temp_db)

    decisions = [
        {
            "decision_id": "dec_1_123.456",
            "task_id": "task_001",
            "agent_id": "agent_001",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "what": "Chose PostgreSQL",
            "why": "Need ACID compliance",
            "impact": "Affects all data models",
            "affected_tasks": ["task_002"],
            "confidence": 0.9,
            "kanban_comment_url": None,
            "project_id": "test_project_001",
        },
        {
            "decision_id": "dec_2_123.789",
            "task_id": "task_002",
            "agent_id": "agent_001",
            "timestamp": "2025-11-08T13:00:00+00:00",
            "what": "Chose React",
            "why": "Team familiarity",
            "impact": "Affects frontend architecture",
            "affected_tasks": [],
            "confidence": 0.8,
            "kanban_comment_url": None,
            "project_id": "test_project_001",
        },
    ]

    for dec in decisions:
        await backend.store("decisions", dec["decision_id"], dec)

    return decisions


class TestProjectHistoryPersistence:
    """Test suite for ProjectHistoryPersistence SQLite refactor."""

    def test_initialization(self, persistence: ProjectHistoryPersistence) -> None:
        """Test ProjectHistoryPersistence initializes with SQLite backend."""
        # Arrange & Act: done in fixture
        # Assert
        assert persistence.db_path.name == "marcus.db"
        assert persistence.marcus_root.exists()
        assert persistence.history_dir.exists()

    @pytest.mark.asyncio
    async def test_load_decisions_empty(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test load_decisions returns empty list when no decisions exist."""
        # Arrange
        project_id = "test_project_001"

        # Act
        decisions = await persistence.load_decisions(project_id)

        # Assert
        assert decisions == []

    @pytest.mark.asyncio
    async def test_load_decisions_from_sqlite(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
        sample_decisions: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Test load_decisions loads from SQLite backend."""
        # Arrange
        project_id = "test_project_001"

        # Update persistence to use the temp_db with sample data
        persistence.db_path = temp_db
        # Update backend to use new db_path (connection pooling fix)
        persistence._backend = SQLitePersistence(db_path=temp_db)

        # Create conversation logs with project_id and task_ids
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        with open(conv_file, "w") as f:
            # Add conversation entries for each task
            for task_id in ["task_001", "task_002"]:
                entry = {
                    "metadata": {
                        "project_id": project_id,
                        "task_id": task_id,
                    },
                    "message": f"Test message for {task_id}",
                }
                f.write(json.dumps(entry) + "\n")

        # Act
        decisions = await persistence.load_decisions(project_id)

        # Assert (rows stored in the same second are ordered by key, newest
        # first, so compare as a set rather than by position)
        assert len(decisions) == 2
        assert all(isinstance(d, Decision) for d in decisions)
        by_what = {d.what: d for d in decisions}
        assert by_what["Chose PostgreSQL"].task_id == "task_001"
        assert by_what["Chose PostgreSQL"].project_id == "test_project_001"
        assert by_what["Chose React"].project_id == "test_project_001"

    @pytest.mark.asyncio
    async def test_load_decisions_includes_project_level_opinions(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
    ) -> None:
        """A project-level opinion (no task) loads by project_id alone.

        Deliberation layer B2: an opinion about the project rather than
        a task carries ``task_id=None``, so the task-id filter derived
        from conversation logs can never match it.  ``project_id`` is
        load-bearing for it, even when no conversation log exists yet.
        """
        # Arrange: no conversation logs at all
        persistence.db_path = temp_db
        persistence._backend = SQLitePersistence(db_path=temp_db)
        await persistence._backend.store(
            "decisions",
            "dec_op_1",
            {
                "decision_id": "dec_op_1",
                "task_id": None,
                "agent_id": "agent_002",
                "timestamp": "2026-11-03T12:00:00+00:00",
                "what": "the farm is flooded",
                "why": "water source breached the north plot",
                "impact": "food supply",
                "subject": "farm.flooded",
                "kind": "assessment",
                "project_id": "test_project_001",
            },
        )

        # Act
        decisions = await persistence.load_decisions("test_project_001")

        # Assert
        assert [d.decision_id for d in decisions] == ["dec_op_1"]
        assert decisions[0].task_id is None
        assert decisions[0].subject == "farm.flooded"
        assert decisions[0].kind == "assessment"

    @pytest.mark.asyncio
    async def test_load_decisions_excludes_other_projects_opinions(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
    ) -> None:
        """A project-level opinion for another project is not returned."""
        # Arrange
        persistence.db_path = temp_db
        persistence._backend = SQLitePersistence(db_path=temp_db)
        await persistence._backend.store(
            "decisions",
            "dec_op_other",
            {
                "decision_id": "dec_op_other",
                "task_id": None,
                "agent_id": "agent_002",
                "timestamp": "2026-11-03T12:00:00+00:00",
                "what": "the farm is flooded",
                "why": "water",
                "impact": "food",
                "subject": "farm.flooded",
                "kind": "assessment",
                "project_id": "some_other_project",
            },
        )

        # Act
        decisions = await persistence.load_decisions("test_project_001")

        # Assert
        assert decisions == []

    @pytest.mark.asyncio
    async def test_load_decisions_finds_an_opinion_behind_many_newer_foreign_rows(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
    ) -> None:
        """A shared database full of newer foreign rows cannot hide ours.

        Codex P2 on PR #800: the backend applied its SQL limit before the
        project filter, so one target-project opinion behind 2 x limit
        newer rows from other projects came back as an empty page.
        """
        import json
        import sqlite3
        from datetime import datetime, timedelta, timezone

        persistence.db_path = temp_db
        persistence._backend = SQLitePersistence(db_path=temp_db)
        t0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        rows = [
            (
                "dec_target",
                {
                    "decision_id": "dec_target",
                    "task_id": None,
                    "agent_id": "agent_002",
                    "timestamp": t0.isoformat(),
                    "what": "the farm is flooded",
                    "why": "water",
                    "impact": "food",
                    "subject": "farm.flooded",
                    "kind": "assessment",
                    "project_id": "test_project_001",
                },
                0,
            )
        ]
        rows += [
            (
                f"dec_foreign_{i}",
                {
                    "decision_id": f"dec_foreign_{i}",
                    "task_id": f"task_{i}",
                    "agent_id": "agent_009",
                    "timestamp": (t0 + timedelta(minutes=i + 1)).isoformat(),
                    "what": "elsewhere",
                    "why": "elsewhere",
                    "impact": "elsewhere",
                    "project_id": "some_other_project",
                },
                i + 1,
            )
            for i in range(24)
        ]
        with sqlite3.connect(temp_db) as conn:
            for key, data, minute in rows:
                conn.execute(
                    "INSERT INTO persistence (collection, key, data, stored_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "decisions",
                        key,
                        json.dumps(data),
                        (t0 + timedelta(minutes=minute)).isoformat(),
                    ),
                )
            conn.commit()

        # Act: limit 5, so the old SQL prefetch of 10 rows held only foreign ones
        decisions = await persistence.load_decisions("test_project_001", limit=5)

        # Assert
        assert [d.decision_id for d in decisions] == ["dec_target"]

    @pytest.mark.asyncio
    async def test_load_decisions_keeps_the_task_id_safety_net_for_mis_stamped_rows(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
    ) -> None:
        """A row stamped with another project but made on our task still loads."""
        import json

        persistence.db_path = temp_db
        persistence._backend = SQLitePersistence(db_path=temp_db)
        await persistence._backend.store(
            "decisions",
            "dec_mis",
            {
                "decision_id": "dec_mis",
                "task_id": "task_001",
                "agent_id": "agent_001",
                "timestamp": "2026-11-03T12:00:00+00:00",
                "what": "chose postgres",
                "why": "acid",
                "impact": "data",
                "project_id": "some_other_project",
            },
        )
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        with open(conversations_dir / "conversations_mis.jsonl", "w") as f:
            f.write(
                json.dumps(
                    {
                        "metadata": {
                            "project_id": "test_project_001",
                            "task_id": "task_001",
                        },
                        "message": "mis-stamped",
                    }
                )
                + "\n"
            )

        decisions = await persistence.load_decisions("test_project_001")

        assert [d.decision_id for d in decisions] == ["dec_mis"]

    @pytest.mark.asyncio
    async def test_load_decisions_still_matches_task_scoped_rows_by_task_id(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
        sample_decisions: list[dict[str, Any]],
    ) -> None:
        """Rows with no project_id still load through the task-id filter."""
        # Arrange: strip project_id from a stored row, keep its task in the logs
        persistence.db_path = temp_db
        persistence._backend = SQLitePersistence(db_path=temp_db)
        legacy = dict(sample_decisions[0])
        legacy["project_id"] = None
        await persistence._backend.store("decisions", legacy["decision_id"], legacy)
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        import json

        with open(conversations_dir / "conversations_legacy.jsonl", "w") as f:
            f.write(
                json.dumps(
                    {
                        "metadata": {
                            "project_id": "test_project_001",
                            "task_id": "task_001",
                        },
                        "message": "legacy",
                    }
                )
                + "\n"
            )

        # Act
        decisions = await persistence.load_decisions("test_project_001")

        # Assert
        assert "dec_1_123.456" in [d.decision_id for d in decisions]

    @pytest.mark.asyncio
    async def test_load_decisions_timezone_aware(
        self,
        persistence: ProjectHistoryPersistence,
        temp_db: Path,
        sample_decisions: list[dict[str, Any]],
    ) -> None:
        """Test loaded decisions have timezone-aware timestamps."""
        # Arrange
        project_id = "test_project_001"
        persistence.db_path = temp_db
        # Update backend to use new db_path (connection pooling fix)
        persistence._backend = SQLitePersistence(db_path=temp_db)

        # Create conversation logs
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        with open(conv_file, "w") as f:
            for task_id in ["task_001", "task_002"]:
                entry = {
                    "metadata": {"project_id": project_id, "task_id": task_id},
                    "message": f"Test message for {task_id}",
                }
                f.write(json.dumps(entry) + "\n")

        # Act
        decisions = await persistence.load_decisions(project_id)

        # Assert
        assert len(decisions) > 0
        for decision in decisions:
            assert decision.timestamp.tzinfo is not None
            assert decision.timestamp.tzinfo == timezone.utc

    @pytest.mark.asyncio
    async def test_load_decisions_naive_timestamp_converted(
        self, persistence: ProjectHistoryPersistence, temp_db: Path
    ) -> None:
        """Test naive timestamps are converted to timezone-aware."""
        # Arrange
        backend = SQLitePersistence(db_path=temp_db)
        persistence.db_path = temp_db
        # Update backend to use new db_path (connection pooling fix)
        persistence._backend = backend

        # Store decision with naive timestamp (no timezone)
        naive_decision = {
            "decision_id": "dec_3_456.789",
            "task_id": "task_003",
            "agent_id": "agent_002",
            "timestamp": "2025-11-08T14:00:00",  # No timezone info
            "what": "Chose MongoDB",
            "why": "Need flexible schema",
            "impact": "Affects data layer",
            "affected_tasks": [],
            "confidence": 0.7,
            "kanban_comment_url": None,
            "project_id": "test_project",
        }
        await backend.store("decisions", naive_decision["decision_id"], naive_decision)

        # Create conversation logs
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        with open(conv_file, "w") as f:
            entry = {
                "metadata": {"project_id": "test_project", "task_id": "task_003"},
                "message": "Test message for task_003",
            }
            f.write(json.dumps(entry) + "\n")

        # Act
        decisions = await persistence.load_decisions("test_project")

        # Assert
        naive_loaded = [d for d in decisions if d.decision_id == "dec_3_456.789"][0]
        assert naive_loaded.timestamp.tzinfo is not None
        assert naive_loaded.timestamp.tzinfo == timezone.utc

    @pytest.mark.asyncio
    async def test_load_artifacts_empty(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test load_artifacts returns empty list when no artifacts exist."""
        # Arrange
        project_id = "test_project_001"

        # Act
        artifacts = await persistence.load_artifacts(project_id)

        # Assert
        assert artifacts == []

    @pytest.mark.asyncio
    async def test_load_decisions_malformed_data(
        self, persistence: ProjectHistoryPersistence, temp_db: Path
    ) -> None:
        """Test load_decisions skips malformed decision records."""
        # Arrange
        backend = SQLitePersistence(db_path=temp_db)
        persistence.db_path = temp_db
        # Update backend to use new db_path (connection pooling fix)
        persistence._backend = backend

        # Store valid decision
        valid_dec = {
            "decision_id": "dec_4_111.222",
            "task_id": "task_004",
            "agent_id": "agent_003",
            "timestamp": "2025-11-08T15:00:00+00:00",
            "what": "Valid decision",
            "why": "Good reason",
            "impact": "Some impact",
            "project_id": "test_project",
        }
        await backend.store("decisions", valid_dec["decision_id"], valid_dec)

        # Store malformed decision (missing required fields)
        malformed_dec = {
            "decision_id": "dec_5_333.444",
            # Missing task_id, agent_id, etc.
            "what": "Incomplete decision",
        }
        await backend.store("decisions", malformed_dec["decision_id"], malformed_dec)

        # Create conversation logs
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        with open(conv_file, "w") as f:
            entry = {
                "metadata": {"project_id": "test_project", "task_id": "task_004"},
                "message": "Test message for task_004",
            }
            f.write(json.dumps(entry) + "\n")

        # Act
        decisions = await persistence.load_decisions("test_project")

        # Assert - should only load valid decision, skip malformed
        assert len(decisions) == 1
        assert decisions[0].decision_id == "dec_4_111.222"


class TestDecisionFromDict:
    """Test suite for Decision.from_dict() method."""

    def test_from_dict_with_timezone_aware_timestamp(self) -> None:
        """Test from_dict handles timezone-aware timestamp correctly."""
        # Arrange
        data = {
            "decision_id": "dec_1",
            "task_id": "task_1",
            "agent_id": "agent_1",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "what": "Decision",
            "why": "Reason",
            "impact": "Impact",
        }

        # Act
        decision = Decision.from_dict(data)

        # Assert
        assert decision.timestamp.tzinfo is not None
        assert decision.timestamp.hour == 12

    def test_from_dict_with_naive_timestamp(self) -> None:
        """Test from_dict converts naive timestamp to UTC."""
        # Arrange
        data = {
            "decision_id": "dec_2",
            "task_id": "task_2",
            "agent_id": "agent_2",
            "timestamp": "2025-11-08T12:00:00",  # Naive
            "what": "Decision",
            "why": "Reason",
            "impact": "Impact",
        }

        # Act
        decision = Decision.from_dict(data)

        # Assert
        assert decision.timestamp.tzinfo is not None
        assert decision.timestamp.tzinfo == timezone.utc
        assert decision.timestamp.hour == 12

    def test_from_dict_with_project_id(self) -> None:
        """Test from_dict handles project_id correctly."""
        # Arrange
        data = {
            "decision_id": "dec_3",
            "task_id": "task_3",
            "agent_id": "agent_3",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "what": "Decision with project",
            "why": "Reason",
            "impact": "Impact",
            "project_id": "test_project_123",
        }

        # Act
        decision = Decision.from_dict(data)

        # Assert
        assert decision.project_id == "test_project_123"
        assert decision.to_dict()["project_id"] == "test_project_123"

    def test_from_dict_without_project_id(self) -> None:
        """Test from_dict handles missing project_id gracefully."""
        # Arrange
        data = {
            "decision_id": "dec_4",
            "task_id": "task_4",
            "agent_id": "agent_4",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "what": "Decision without project",
            "why": "Reason",
            "impact": "Impact",
            # No project_id field
        }

        # Act
        decision = Decision.from_dict(data)

        # Assert
        assert decision.project_id is None
        assert decision.to_dict()["project_id"] is None


class TestArtifactMetadataFromDict:
    """Test suite for ArtifactMetadata.from_dict() method."""

    def test_from_dict_with_timezone_aware_timestamp(self) -> None:
        """Test from_dict handles timezone-aware timestamp correctly."""
        # Arrange
        data = {
            "artifact_id": "art_1",
            "task_id": "task_1",
            "agent_id": "agent_1",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "filename": "spec.md",
            "artifact_type": "specification",
            "relative_path": "docs/spec.md",
            "absolute_path": "/path/to/docs/spec.md",
        }

        # Act
        artifact = ArtifactMetadata.from_dict(data)

        # Assert
        assert artifact.timestamp.tzinfo is not None
        assert artifact.timestamp.hour == 12

    def test_from_dict_with_naive_timestamp(self) -> None:
        """Test from_dict converts naive timestamp to UTC."""
        # Arrange
        data = {
            "artifact_id": "art_2",
            "task_id": "task_2",
            "agent_id": "agent_2",
            "timestamp": "2025-11-08T12:00:00",  # Naive
            "filename": "design.md",
            "artifact_type": "design",
            "relative_path": "docs/design.md",
            "absolute_path": "/path/to/docs/design.md",
        }

        # Act
        artifact = ArtifactMetadata.from_dict(data)

        # Assert
        assert artifact.timestamp.tzinfo is not None
        assert artifact.timestamp.tzinfo == timezone.utc
        assert artifact.timestamp.hour == 12

    def test_from_dict_with_project_id(self) -> None:
        """Test from_dict handles project_id correctly."""
        # Arrange
        data = {
            "artifact_id": "art_3",
            "task_id": "task_3",
            "agent_id": "agent_3",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "filename": "spec.md",
            "artifact_type": "specification",
            "relative_path": "docs/spec.md",
            "absolute_path": "/path/to/docs/spec.md",
            "project_id": "test_project_456",
        }

        # Act
        artifact = ArtifactMetadata.from_dict(data)

        # Assert
        assert artifact.project_id == "test_project_456"
        assert artifact.to_dict()["project_id"] == "test_project_456"

    def test_from_dict_without_project_id(self) -> None:
        """Test from_dict handles missing project_id gracefully."""
        # Arrange
        data = {
            "artifact_id": "art_4",
            "task_id": "task_4",
            "agent_id": "agent_4",
            "timestamp": "2025-11-08T12:00:00+00:00",
            "filename": "api.md",
            "artifact_type": "api",
            "relative_path": "docs/api.md",
            "absolute_path": "/path/to/docs/api.md",
            # No project_id field
        }

        # Act
        artifact = ArtifactMetadata.from_dict(data)

        # Assert
        assert artifact.project_id is None
        assert artifact.to_dict()["project_id"] is None


class TestPaginationSupport:
    """Test suite for pagination in load_decisions and load_artifacts."""

    @pytest.fixture
    async def persistence_with_many_decisions(
        self, tmp_path: Path
    ) -> ProjectHistoryPersistence:
        """Create persistence with many decisions for pagination testing."""
        marcus_root = tmp_path / "marcus"
        marcus_root.mkdir()
        (marcus_root / "data").mkdir()

        persistence = ProjectHistoryPersistence(marcus_root=marcus_root)

        # Create conversation logs with project_id and task_ids
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        # Create 50 tasks for pagination testing
        task_ids = [f"task_{i:03d}" for i in range(50)]
        with open(conv_file, "w") as f:
            for task_id in task_ids:
                entry = {
                    "metadata": {"project_id": "pagination_test", "task_id": task_id},
                    "message": f"Test message for {task_id}",
                }
                f.write(json.dumps(entry) + "\n")

        # Create 50 decisions in SQLite
        backend = SQLitePersistence(db_path=persistence.db_path)
        for i in range(50):
            decision = {
                "decision_id": f"dec_{i}_123.456",
                "task_id": f"task_{i:03d}",
                "agent_id": "agent_001",
                "timestamp": "2025-11-08T12:00:00+00:00",
                "what": f"Decision {i}",
                "why": f"Reason {i}",
                "impact": f"Impact {i}",
                "project_id": "pagination_test",
            }
            await backend.store("decisions", decision["decision_id"], decision)

        return persistence

    @pytest.mark.asyncio
    async def test_load_decisions_default_limit(
        self, persistence_with_many_decisions: ProjectHistoryPersistence
    ) -> None:
        """Test load_decisions uses default limit of 10000."""
        # Act
        decisions = await persistence_with_many_decisions.load_decisions(
            "pagination_test"
        )

        # Assert - should load all 50 decisions (under default limit)
        assert len(decisions) == 50

    @pytest.mark.asyncio
    async def test_load_decisions_with_limit(
        self, persistence_with_many_decisions: ProjectHistoryPersistence
    ) -> None:
        """Test load_decisions respects limit parameter."""
        # Act
        decisions = await persistence_with_many_decisions.load_decisions(
            "pagination_test", limit=10
        )

        # Assert
        assert len(decisions) == 10
        # Verify decisions are from the test set (not specific order)
        decision_nums = [int(d.what.split()[-1]) for d in decisions]
        assert all(0 <= num < 50 for num in decision_nums)

    @pytest.mark.asyncio
    async def test_load_decisions_with_offset(
        self, persistence_with_many_decisions: ProjectHistoryPersistence
    ) -> None:
        """Test load_decisions respects offset parameter."""
        # Act - Get first 10 and then next 10 with offset
        first_batch = await persistence_with_many_decisions.load_decisions(
            "pagination_test", limit=10, offset=0
        )
        second_batch = await persistence_with_many_decisions.load_decisions(
            "pagination_test", limit=10, offset=10
        )

        # Assert
        assert len(first_batch) == 10
        assert len(second_batch) == 10
        # Verify no overlap between batches
        first_ids = {d.decision_id for d in first_batch}
        second_ids = {d.decision_id for d in second_batch}
        assert len(first_ids & second_ids) == 0  # No intersection

    @pytest.mark.asyncio
    async def test_load_decisions_offset_beyond_results(
        self, persistence_with_many_decisions: ProjectHistoryPersistence
    ) -> None:
        """Test load_decisions with offset beyond available results."""
        # Act
        decisions = await persistence_with_many_decisions.load_decisions(
            "pagination_test", limit=10, offset=100
        )

        # Assert - should return empty list
        assert len(decisions) == 0

    @pytest.mark.asyncio
    async def test_load_decisions_caps_at_10000(
        self, persistence_with_many_decisions: ProjectHistoryPersistence
    ) -> None:
        """Test load_decisions caps query limit at 10000."""
        # Act - request huge limit
        decisions = await persistence_with_many_decisions.load_decisions(
            "pagination_test", limit=100000
        )

        # Assert - should still work and return all 50
        assert len(decisions) == 50

    @pytest.mark.asyncio
    async def test_load_artifacts_with_pagination(
        self, persistence_with_many_decisions: ProjectHistoryPersistence
    ) -> None:
        """Test load_artifacts supports pagination."""
        # Arrange - add some artifacts
        backend = SQLitePersistence(db_path=persistence_with_many_decisions.db_path)
        for i in range(30):
            artifact = {
                "artifact_id": f"art_{i}",
                "task_id": f"task_{i:03d}",
                "agent_id": "agent_001",
                "timestamp": "2025-11-08T12:00:00+00:00",
                "filename": f"spec_{i}.md",
                "artifact_type": "specification",
                "relative_path": f"docs/spec_{i}.md",
                "absolute_path": f"/path/to/docs/spec_{i}.md",
                "project_id": "pagination_test",
            }
            await backend.store("artifacts", artifact["artifact_id"], artifact)

        # Act - Get two batches to verify pagination
        first_batch = await persistence_with_many_decisions.load_artifacts(
            "pagination_test", limit=10, offset=0
        )
        second_batch = await persistence_with_many_decisions.load_artifacts(
            "pagination_test", limit=10, offset=10
        )

        # Assert
        assert len(first_batch) == 10
        assert len(second_batch) == 10
        # Verify no overlap between batches
        first_ids = {a.artifact_id for a in first_batch}
        second_ids = {a.artifact_id for a in second_batch}
        assert len(first_ids & second_ids) == 0  # No intersection


class TestErrorHandling:
    """Test suite for Marcus Error Framework integration."""

    @pytest.fixture
    def persistence(self, tmp_path: Path) -> ProjectHistoryPersistence:
        """Create persistence for error testing."""
        marcus_root = tmp_path / "marcus"
        marcus_root.mkdir()
        (marcus_root / "data").mkdir()
        return ProjectHistoryPersistence(marcus_root=marcus_root)

    @pytest.mark.asyncio
    async def test_load_decisions_raises_database_error(
        self, persistence: ProjectHistoryPersistence, monkeypatch: Any
    ) -> None:
        """Test load_decisions raises DatabaseError on failure."""
        from src.core.error_framework import DatabaseError

        # Arrange - mock backend to raise error
        async def mock_query(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("Database connection failed")

        monkeypatch.setattr(persistence._backend, "query", mock_query)

        # Create minimal conversation logs
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        with open(conv_file, "w") as f:
            entry = {
                "metadata": {"project_id": "error_test", "task_id": "task_001"},
                "message": "Test message",
            }
            f.write(json.dumps(entry) + "\n")

        # Act & Assert
        with pytest.raises(DatabaseError) as exc_info:
            await persistence.load_decisions("error_test")

        # Verify error attributes
        assert "load_decisions" in str(exc_info.value)
        assert "decisions" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_load_artifacts_raises_database_error(
        self, persistence: ProjectHistoryPersistence, monkeypatch: Any
    ) -> None:
        """Test load_artifacts raises DatabaseError on failure."""
        from src.core.error_framework import DatabaseError

        # Arrange - mock backend to raise error
        async def mock_query(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("Database connection failed")

        monkeypatch.setattr(persistence._backend, "query", mock_query)

        # Create minimal conversation logs
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        import json

        with open(conv_file, "w") as f:
            entry = {
                "metadata": {"project_id": "error_test", "task_id": "task_001"},
                "message": "Test message",
            }
            f.write(json.dumps(entry) + "\n")

        # Act & Assert
        with pytest.raises(DatabaseError) as exc_info:
            await persistence.load_artifacts("error_test")

        # Verify error attributes
        assert "load_artifacts" in str(exc_info.value)
        assert "artifacts" in str(exc_info.value)


class TestConversationLogQueries:
    """Test suite for querying project IDs from conversation logs."""

    @pytest.mark.asyncio
    async def test_get_all_project_ids_from_conversations(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test extracting all unique project IDs from conversation logs."""
        import json

        # Arrange - create conversation logs with multiple projects
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)

        # Create multiple log files with different projects
        conv_file1 = conversations_dir / "conversations_20250101.jsonl"
        conv_file2 = conversations_dir / "conversations_20250102.jsonl"

        # File 1: projects A and B
        with open(conv_file1, "w") as f:
            f.write(
                json.dumps(
                    {
                        "metadata": {"project_id": "proj_a", "task_id": "task_1"},
                        "message": "Test",
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "metadata": {"project_id": "proj_b", "task_id": "task_2"},
                        "message": "Test",
                    }
                )
                + "\n"
            )

        # File 2: projects B and C (B should appear only once)
        with open(conv_file2, "w") as f:
            f.write(
                json.dumps(
                    {
                        "metadata": {"project_id": "proj_b", "task_id": "task_3"},
                        "message": "Test",
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "metadata": {"project_id": "proj_c", "task_id": "task_4"},
                        "message": "Test",
                    }
                )
                + "\n"
            )

        # Act
        project_ids = await persistence._get_all_project_ids_from_conversations()

        # Assert
        assert len(project_ids) == 3
        assert "proj_a" in project_ids
        assert "proj_b" in project_ids
        assert "proj_c" in project_ids

    @pytest.mark.asyncio
    async def test_get_all_project_ids_no_conversations(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test when no conversation logs exist."""
        # Act
        project_ids = await persistence._get_all_project_ids_from_conversations()

        # Assert
        assert len(project_ids) == 0

    @pytest.mark.asyncio
    async def test_get_all_project_ids_handles_malformed_lines(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test that malformed JSON lines are skipped gracefully."""
        import json

        # Arrange - create conversation log with some malformed lines
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)

        conv_file = conversations_dir / "conversations_test.jsonl"
        with open(conv_file, "w") as f:
            # Valid line
            f.write(
                json.dumps(
                    {
                        "metadata": {"project_id": "proj_good", "task_id": "task_1"},
                        "message": "Valid",
                    }
                )
                + "\n"
            )
            # Malformed JSON
            f.write("{ this is not valid json }\n")
            # Another valid line
            f.write(
                json.dumps(
                    {
                        "metadata": {"project_id": "proj_good2", "task_id": "task_2"},
                        "message": "Valid",
                    }
                )
                + "\n"
            )

        # Act
        project_ids = await persistence._get_all_project_ids_from_conversations()

        # Assert - should get both valid projects despite malformed line
        assert len(project_ids) == 2
        assert "proj_good" in project_ids
        assert "proj_good2" in project_ids


class TestArtifactPersistenceRoundtrip:
    """
    Test suite for artifact append → load roundtrip.

    This tests the complete flow: append_artifact() should write to BOTH
    JSON and SQLite, so that load_artifacts() can successfully retrieve them.
    """

    @pytest.fixture
    def persistence(self, tmp_path: Path) -> ProjectHistoryPersistence:
        """Create ProjectHistoryPersistence with temporary database."""
        marcus_root = tmp_path / "marcus"
        marcus_root.mkdir()
        (marcus_root / "data").mkdir()

        # Create conversations directory for task_id filtering
        conversations_dir = marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)

        return ProjectHistoryPersistence(marcus_root=marcus_root)

    @pytest.fixture
    def sample_artifact(self) -> ArtifactMetadata:
        """Create a sample artifact for testing."""
        return ArtifactMetadata(
            artifact_id="art_roundtrip_test",
            task_id="task_roundtrip",
            agent_id="agent_test",
            timestamp=datetime.now(timezone.utc),
            filename="test_spec.md",
            artifact_type="specification",
            relative_path="docs/specifications/test_spec.md",
            absolute_path="/tmp/project/docs/specifications/test_spec.md",  # nosec B108 (test data only)
            description="Test specification",
            file_size_bytes=1024,
            sha256_hash="abc123",
            project_id="roundtrip_project",
        )

    @pytest.mark.asyncio
    async def test_append_artifact_persists_to_sqlite(
        self, persistence: ProjectHistoryPersistence, sample_artifact: ArtifactMetadata
    ) -> None:
        """
        Test that append_artifact writes to SQLite so load_artifacts can retrieve it.

        This test exposes the bug where append_artifact only writes to JSON but
        load_artifacts reads from SQLite.
        """
        import json

        # Arrange - create conversation log so artifact won't be filtered out
        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conv_file = conversations_dir / "conversations_test.jsonl"

        with open(conv_file, "w") as f:
            entry = {
                "metadata": {
                    "project_id": "roundtrip_project",
                    "task_id": "task_roundtrip",
                },
                "message": "Test task for roundtrip",
            }
            f.write(json.dumps(entry) + "\n")

        # Act - append artifact
        await persistence.append_artifact(
            "roundtrip_project", "Roundtrip Test Project", sample_artifact
        )

        # Assert - artifact should be in JSON file
        artifacts_file = (
            persistence.history_dir / "roundtrip_project" / "artifacts.json"
        )
        assert artifacts_file.exists()

        with open(artifacts_file, "r") as f:
            json_data = json.load(f)
        assert len(json_data["artifacts"]) == 1
        assert json_data["artifacts"][0]["artifact_id"] == "art_roundtrip_test"

        # Assert - artifact should ALSO be in SQLite (THIS WILL FAIL - exposes bug)
        loaded_artifacts = await persistence.load_artifacts("roundtrip_project")
        assert len(loaded_artifacts) == 1, (
            f"Expected 1 artifact from SQLite, got {len(loaded_artifacts)}. "
            "Bug: append_artifact writes to JSON only, not SQLite!"
        )
        assert loaded_artifacts[0].artifact_id == "art_roundtrip_test"
        assert loaded_artifacts[0].filename == "test_spec.md"
