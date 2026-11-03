"""
Unit tests for project history data models and persistence.

Tests serialization, deserialization, atomic writes, and file operations
for decisions, artifacts, and project snapshots.
"""

import json
import tempfile
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.core.project_history import (
    ArtifactMetadata,
    Decision,
    ProjectHistoryPersistence,
    ProjectSnapshot,
)


class TestDecision:
    """Test suite for Decision dataclass."""

    @pytest.fixture
    def sample_decision(self) -> Decision:
        """Create a sample decision for testing."""
        return Decision(
            decision_id="dec_123",
            task_id="task_456",
            agent_id="agent_789",
            timestamp=datetime(2025, 11, 7, 10, 30, 0, tzinfo=timezone.utc),
            what="Use PostgreSQL for database",
            why="Need ACID compliance and strong relational model",
            impact="All data models must use SQLAlchemy",
            affected_tasks=["task_db_impl", "task_api_impl"],
            confidence=0.9,
            kanban_comment_url="https://kanban.local/comment/123",
        )

    def test_decision_to_dict_serializes_correctly(
        self, sample_decision: Decision
    ) -> None:
        """Test Decision serializes to dict with ISO timestamps."""
        result = sample_decision.to_dict()

        assert result["decision_id"] == "dec_123"
        assert result["task_id"] == "task_456"
        assert result["agent_id"] == "agent_789"
        assert result["timestamp"] == "2025-11-07T10:30:00+00:00"
        assert result["what"] == "Use PostgreSQL for database"
        assert result["why"] == "Need ACID compliance and strong relational model"
        assert result["impact"] == "All data models must use SQLAlchemy"
        assert result["affected_tasks"] == ["task_db_impl", "task_api_impl"]
        assert result["confidence"] == 0.9
        assert result["kanban_comment_url"] == "https://kanban.local/comment/123"

    def test_decision_from_dict_deserializes_correctly(
        self, sample_decision: Decision
    ) -> None:
        """Test Decision deserializes from dict."""
        data = sample_decision.to_dict()
        restored = Decision.from_dict(data)

        assert restored.decision_id == sample_decision.decision_id
        assert restored.task_id == sample_decision.task_id
        assert restored.agent_id == sample_decision.agent_id
        assert restored.timestamp == sample_decision.timestamp
        assert restored.what == sample_decision.what
        assert restored.why == sample_decision.why
        assert restored.impact == sample_decision.impact
        assert restored.affected_tasks == sample_decision.affected_tasks
        assert restored.confidence == sample_decision.confidence
        assert restored.kanban_comment_url == sample_decision.kanban_comment_url

    def test_decision_roundtrip_preserves_data(self, sample_decision: Decision) -> None:
        """Test serializing and deserializing preserves all data."""
        restored = Decision.from_dict(sample_decision.to_dict())
        assert restored == sample_decision

    def test_decision_with_minimal_fields(self) -> None:
        """Test Decision with only required fields."""
        decision = Decision(
            decision_id="dec_min",
            task_id="task_min",
            agent_id="agent_min",
            timestamp=datetime.now(timezone.utc),
            what="Minimal decision",
            why="Testing",
            impact="None",
        )

        data = decision.to_dict()
        restored = Decision.from_dict(data)

        assert restored.affected_tasks == []
        assert restored.confidence == 0.8
        assert restored.kanban_comment_url is None


class TestArtifactMetadata:
    """Test suite for ArtifactMetadata dataclass."""

    @pytest.fixture
    def sample_artifact(self) -> ArtifactMetadata:
        """Create a sample artifact for testing."""
        return ArtifactMetadata(
            artifact_id="art_123",
            task_id="task_456",
            agent_id="agent_789",
            timestamp=datetime(2025, 11, 7, 11, 0, 0, tzinfo=timezone.utc),
            filename="api_spec.md",
            artifact_type="specification",
            relative_path="docs/specifications/api_spec.md",
            absolute_path="/Users/test/project/docs/specifications/api_spec.md",
            description="REST API specification",
            file_size_bytes=4096,
            sha256_hash="abc123def456",
            kanban_comment_url="https://kanban.local/comment/456",
            referenced_by_tasks=["task_api_impl", "task_tests"],
        )

    def test_artifact_to_dict_serializes_correctly(
        self, sample_artifact: ArtifactMetadata
    ) -> None:
        """Test ArtifactMetadata serializes to dict."""
        result = sample_artifact.to_dict()

        assert result["artifact_id"] == "art_123"
        assert result["task_id"] == "task_456"
        assert result["agent_id"] == "agent_789"
        assert result["timestamp"] == "2025-11-07T11:00:00+00:00"
        assert result["filename"] == "api_spec.md"
        assert result["artifact_type"] == "specification"
        assert result["relative_path"] == "docs/specifications/api_spec.md"
        assert (
            result["absolute_path"]
            == "/Users/test/project/docs/specifications/api_spec.md"
        )
        assert result["description"] == "REST API specification"
        assert result["file_size_bytes"] == 4096
        assert result["sha256_hash"] == "abc123def456"
        assert result["kanban_comment_url"] == "https://kanban.local/comment/456"
        assert result["referenced_by_tasks"] == ["task_api_impl", "task_tests"]

    def test_artifact_from_dict_deserializes_correctly(
        self, sample_artifact: ArtifactMetadata
    ) -> None:
        """Test ArtifactMetadata deserializes from dict."""
        data = sample_artifact.to_dict()
        restored = ArtifactMetadata.from_dict(data)

        assert restored == sample_artifact

    def test_artifact_roundtrip_preserves_data(
        self, sample_artifact: ArtifactMetadata
    ) -> None:
        """Test serializing and deserializing preserves all data."""
        restored = ArtifactMetadata.from_dict(sample_artifact.to_dict())
        assert restored == sample_artifact

    def test_artifact_with_minimal_fields(self) -> None:
        """Test ArtifactMetadata with only required fields."""
        artifact = ArtifactMetadata(
            artifact_id="art_min",
            task_id="task_min",
            agent_id="agent_min",
            timestamp=datetime.now(timezone.utc),
            filename="test.txt",
            artifact_type="temporary",
            relative_path="tmp/test.txt",
            absolute_path="/tmp/test.txt",  # nosec - test fixture path
            description="Test file",
        )

        data = artifact.to_dict()
        restored = ArtifactMetadata.from_dict(data)

        assert restored.file_size_bytes == 0
        assert restored.sha256_hash is None
        assert restored.kanban_comment_url is None
        assert restored.referenced_by_tasks == []


class TestProjectSnapshot:
    """Test suite for ProjectSnapshot dataclass."""

    @pytest.fixture
    def sample_snapshot(self) -> ProjectSnapshot:
        """Create a sample project snapshot for testing."""
        return ProjectSnapshot(
            project_id="proj_123",
            project_name="Test Project",
            snapshot_timestamp=datetime(2025, 11, 7, 12, 0, 0, tzinfo=timezone.utc),
            completion_status="completed",
            total_tasks=10,
            completed_tasks=9,
            in_progress_tasks=0,
            blocked_tasks=1,
            completion_rate=0.9,
            project_started=datetime(2025, 11, 5, 9, 0, 0, tzinfo=timezone.utc),
            project_completed=datetime(2025, 11, 7, 12, 0, 0, tzinfo=timezone.utc),
            total_duration_hours=51.0,
            estimated_hours=48.0,
            actual_hours=51.0,
            estimation_accuracy=0.94,
            total_agents=3,
            agent_summary=[
                {"agent_id": "agent_1", "tasks_completed": 4},
                {"agent_id": "agent_2", "tasks_completed": 3},
                {"agent_id": "agent_3", "tasks_completed": 2},
            ],
            team_velocity=3.0,
            risk_level="low",
            average_task_duration=5.67,
            blockage_rate=0.1,
            languages=["Python"],
            frameworks=["FastAPI"],
            tools=["PostgreSQL"],
            application_works=True,
            deployment_status="production",
            user_satisfaction="high",
            notes="Successfully implemented task management API",
        )

    def test_snapshot_to_dict_serializes_correctly(
        self, sample_snapshot: ProjectSnapshot
    ) -> None:
        """Test ProjectSnapshot serializes to dict."""
        result = sample_snapshot.to_dict()

        assert result["project_id"] == "proj_123"
        assert result["project_name"] == "Test Project"
        assert result["snapshot_timestamp"] == "2025-11-07T12:00:00+00:00"
        assert result["completion_status"] == "completed"
        assert result["task_statistics"]["total_tasks"] == 10
        assert result["task_statistics"]["completed"] == 9
        assert result["task_statistics"]["completion_rate"] == 0.9
        assert result["timing"]["project_started"] == "2025-11-05T09:00:00+00:00"
        assert result["timing"]["project_completed"] == "2025-11-07T12:00:00+00:00"
        assert result["timing"]["total_duration_hours"] == 51.0
        assert result["team"]["total_agents"] == 3
        assert len(result["team"]["agents"]) == 3
        assert result["technology_stack"]["languages"] == ["Python"]
        assert result["technology_stack"]["frameworks"] == ["FastAPI"]

    def test_snapshot_from_dict_deserializes_correctly(
        self, sample_snapshot: ProjectSnapshot
    ) -> None:
        """Test ProjectSnapshot deserializes from dict."""
        data = sample_snapshot.to_dict()
        restored = ProjectSnapshot.from_dict(data)

        assert restored == sample_snapshot

    def test_snapshot_roundtrip_preserves_data(
        self, sample_snapshot: ProjectSnapshot
    ) -> None:
        """Test serializing and deserializing preserves all data."""
        restored = ProjectSnapshot.from_dict(sample_snapshot.to_dict())
        assert restored == sample_snapshot


class TestProjectHistoryPersistence:
    """Test suite for ProjectHistoryPersistence class."""

    @pytest.fixture
    def temp_marcus_root(self) -> Generator[Path, None, None]:
        """Create a temporary MARCUS root directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def persistence(self, temp_marcus_root: Path) -> ProjectHistoryPersistence:
        """Create ProjectHistoryPersistence with temp directory."""
        return ProjectHistoryPersistence(marcus_root=temp_marcus_root)

    @pytest.fixture
    def sample_decision(self) -> Decision:
        """Create a sample decision."""
        return Decision(
            decision_id="dec_test",
            task_id="task_test",
            agent_id="agent_test",
            timestamp=datetime(2025, 11, 7, 10, 0, 0, tzinfo=timezone.utc),
            what="Test decision",
            why="Testing",
            impact="None",
        )

    @pytest.fixture
    def sample_artifact(self) -> ArtifactMetadata:
        """Create a sample artifact."""
        return ArtifactMetadata(
            artifact_id="art_test",
            task_id="task_test",
            agent_id="agent_test",
            timestamp=datetime(2025, 11, 7, 11, 0, 0, tzinfo=timezone.utc),
            filename="test.txt",
            artifact_type="temporary",
            relative_path="tmp/test.txt",
            absolute_path="/tmp/test.txt",  # nosec - test fixture path
            description="Test artifact",
        )

    @pytest.fixture
    def sample_snapshot(self) -> ProjectSnapshot:
        """Create a sample snapshot."""
        return ProjectSnapshot(
            project_id="proj_test",
            project_name="Test Project",
            snapshot_timestamp=datetime(2025, 11, 7, 12, 0, 0, tzinfo=timezone.utc),
            completion_status="completed",
            total_tasks=5,
            completed_tasks=5,
            in_progress_tasks=0,
            blocked_tasks=0,
            completion_rate=1.0,
            project_started=datetime(2025, 11, 7, 9, 0, 0, tzinfo=timezone.utc),
            project_completed=datetime(2025, 11, 7, 12, 0, 0, tzinfo=timezone.utc),
            total_duration_hours=3.0,
            estimated_hours=3.0,
            actual_hours=3.0,
            estimation_accuracy=1.0,
            total_agents=1,
            agent_summary=[{"agent_id": "agent_test", "tasks_completed": 5}],
            languages=["Python"],
        )

    def _create_conversation_log(
        self, persistence: ProjectHistoryPersistence, project_id: str, task_id: str
    ) -> None:
        """Helper to create conversation log for task_id filtering."""
        import json

        conversations_dir = persistence.marcus_root / "logs" / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conversations_dir / "conversations_test.jsonl"

        with open(conv_file, "a") as f:  # Append mode
            entry = {
                "metadata": {"project_id": project_id, "task_id": task_id},
                "message": f"Test message for {task_id}",
            }
            f.write(json.dumps(entry) + "\n")

    def test_persistence_creates_directory_structure(
        self, temp_marcus_root: Path
    ) -> None:
        """Test persistence creates data/project_history directory."""
        persistence = ProjectHistoryPersistence(marcus_root=temp_marcus_root)

        assert persistence.history_dir.exists()
        assert persistence.history_dir.is_dir()
        assert persistence.history_dir == temp_marcus_root / "data/project_history"

    @pytest.mark.asyncio
    async def test_append_decision_creates_new_file(
        self,
        persistence: ProjectHistoryPersistence,
        sample_decision: Decision,
    ) -> None:
        """Test appending decision creates new file if none exists."""
        await persistence.append_decision("proj_test", "Test Project", sample_decision)

        project_dir = persistence.history_dir / "proj_test"
        decisions_file = project_dir / "decisions.json"

        assert project_dir.exists()
        assert decisions_file.exists()

        with open(decisions_file, "r") as f:
            data = json.load(f)

        assert data["project_id"] == "proj_test"
        assert data["project_name"] == "Test Project"
        assert len(data["decisions"]) == 1
        assert data["decisions"][0]["decision_id"] == "dec_test"

    @pytest.mark.asyncio
    async def test_append_decision_appends_to_existing(
        self,
        persistence: ProjectHistoryPersistence,
        sample_decision: Decision,
    ) -> None:
        """Test appending multiple decisions accumulates them."""
        # Create conversation logs for task_id filtering
        self._create_conversation_log(persistence, "proj_test", "task_test")
        self._create_conversation_log(persistence, "proj_test", "task_test_2")

        # Add first decision
        await persistence.append_decision("proj_test", "Test Project", sample_decision)

        # Add second decision
        decision2 = Decision(
            decision_id="dec_test_2",
            task_id="task_test_2",
            agent_id="agent_test",
            timestamp=datetime(2025, 11, 7, 11, 0, 0, tzinfo=timezone.utc),
            what="Second decision",
            why="Testing append",
            impact="None",
        )
        await persistence.append_decision("proj_test", "Test Project", decision2)

        # Verify both decisions are present (rows stored in the same second
        # are ordered newest first by key, so compare as a set)
        decisions = await persistence.load_decisions("proj_test")
        assert len(decisions) == 2
        assert {d.decision_id for d in decisions} == {"dec_test", "dec_test_2"}

    @pytest.mark.asyncio
    async def test_append_artifact_creates_new_file(
        self,
        persistence: ProjectHistoryPersistence,
        sample_artifact: ArtifactMetadata,
    ) -> None:
        """Test appending artifact creates new file if none exists."""
        await persistence.append_artifact("proj_test", "Test Project", sample_artifact)

        artifacts_file = persistence.history_dir / "proj_test/artifacts.json"
        assert artifacts_file.exists()

        with open(artifacts_file, "r") as f:
            data = json.load(f)

        assert len(data["artifacts"]) == 1
        assert data["artifacts"][0]["artifact_id"] == "art_test"

    @pytest.mark.asyncio
    async def test_save_snapshot_creates_file(
        self,
        persistence: ProjectHistoryPersistence,
        sample_snapshot: ProjectSnapshot,
    ) -> None:
        """Test saving snapshot creates snapshot.json."""
        await persistence.save_snapshot(sample_snapshot)

        snapshot_file = persistence.history_dir / "proj_test/snapshot.json"
        assert snapshot_file.exists()

        with open(snapshot_file, "r") as f:
            data = json.load(f)

        assert data["project_id"] == "proj_test"
        assert data["completion_status"] == "completed"

    @pytest.mark.asyncio
    async def test_load_decisions_returns_empty_for_nonexistent(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test loading decisions from non-existent project returns empty list."""
        decisions = await persistence.load_decisions("nonexistent_project")
        assert decisions == []

    @pytest.mark.asyncio
    async def test_load_artifacts_returns_empty_for_nonexistent(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test loading artifacts from non-existent project returns empty list."""
        artifacts = await persistence.load_artifacts("nonexistent_project")
        assert artifacts == []

    @pytest.mark.asyncio
    async def test_load_snapshot_returns_none_for_nonexistent(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test loading snapshot from non-existent project returns None."""
        snapshot = await persistence.load_snapshot("nonexistent_project")
        assert snapshot is None

    @pytest.mark.asyncio
    async def test_load_decisions_deserializes_correctly(
        self,
        persistence: ProjectHistoryPersistence,
        sample_decision: Decision,
    ) -> None:
        """Test loading decisions correctly deserializes from JSON."""
        # Create conversation log for task_id filtering
        self._create_conversation_log(persistence, "proj_test", "task_test")

        await persistence.append_decision("proj_test", "Test Project", sample_decision)

        decisions = await persistence.load_decisions("proj_test")

        assert len(decisions) == 1
        assert decisions[0] == sample_decision

    @pytest.mark.asyncio
    async def test_load_artifacts_deserializes_correctly(
        self,
        persistence: ProjectHistoryPersistence,
        sample_artifact: ArtifactMetadata,
    ) -> None:
        """Test loading artifacts correctly deserializes from JSON."""
        # Create conversation log for task_id filtering
        self._create_conversation_log(persistence, "proj_test", "task_test")

        await persistence.append_artifact("proj_test", "Test Project", sample_artifact)

        artifacts = await persistence.load_artifacts("proj_test")

        assert len(artifacts) == 1
        assert artifacts[0] == sample_artifact

    @pytest.mark.asyncio
    async def test_load_snapshot_deserializes_correctly(
        self,
        persistence: ProjectHistoryPersistence,
        sample_snapshot: ProjectSnapshot,
    ) -> None:
        """Test loading snapshot correctly deserializes from JSON."""
        await persistence.save_snapshot(sample_snapshot)

        snapshot = await persistence.load_snapshot("proj_test")

        assert snapshot == sample_snapshot

    def test_list_projects_returns_all_project_ids(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test listing projects returns all project directory names."""
        # Create some project directories
        (persistence.history_dir / "proj_1").mkdir(parents=True)
        (persistence.history_dir / "proj_2").mkdir(parents=True)
        (persistence.history_dir / "proj_3").mkdir(parents=True)

        projects = persistence.list_projects()

        assert set(projects) == {"proj_1", "proj_2", "proj_3"}

    def test_list_projects_returns_empty_for_no_projects(
        self, persistence: ProjectHistoryPersistence
    ) -> None:
        """Test listing projects returns empty list when no projects exist."""
        projects = persistence.list_projects()
        assert projects == []

    @pytest.mark.asyncio
    async def test_atomic_write_prevents_corruption(
        self, persistence: ProjectHistoryPersistence, sample_decision: Decision
    ) -> None:
        """Test atomic write produces correct final file."""
        project_dir = persistence.history_dir / "proj_test"
        project_dir.mkdir(parents=True)
        decisions_file = project_dir / "decisions.json"

        # Write decision using atomic write
        await persistence.append_decision("proj_test", "Test Project", sample_decision)

        # Verify final file exists and is valid JSON
        assert decisions_file.exists()
        with open(decisions_file, "r") as f:
            data = json.load(f)
        assert len(data["decisions"]) == 1
        assert data["decisions"][0]["decision_id"] == "dec_test"

        # Verify no temp files remain
        temp_files = list(project_dir.glob("*.tmp"))
        assert len(temp_files) == 0

    @pytest.mark.asyncio
    async def test_roundtrip_full_workflow(
        self,
        persistence: ProjectHistoryPersistence,
        sample_decision: Decision,
        sample_artifact: ArtifactMetadata,
        sample_snapshot: ProjectSnapshot,
    ) -> None:
        """Test complete workflow: save all types and load them back."""
        # Create conversation log for task_id filtering
        self._create_conversation_log(persistence, "proj_test", "task_test")

        # Save all data
        await persistence.append_decision("proj_test", "Test Project", sample_decision)
        await persistence.append_artifact("proj_test", "Test Project", sample_artifact)
        await persistence.save_snapshot(sample_snapshot)

        # Load all data
        decisions = await persistence.load_decisions("proj_test")
        artifacts = await persistence.load_artifacts("proj_test")
        snapshot = await persistence.load_snapshot("proj_test")

        # Verify everything matches
        assert len(decisions) == 1
        assert decisions[0] == sample_decision

        assert len(artifacts) == 1
        assert artifacts[0] == sample_artifact

        assert snapshot == sample_snapshot

        # Verify project shows up in list
        projects = persistence.list_projects()
        assert "proj_test" in projects
