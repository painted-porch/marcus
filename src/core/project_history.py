"""
Project History Persistence Layer.

This module provides persistent storage for ephemeral project execution data:
- Architectural decisions made by agents
- Artifact metadata (files already persist in workspace)
- Project completion snapshots

All data is stored in JSON format under data/project_history/{project_id}/
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.paths import marcus_data_dir

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """
    An architectural decision made during task implementation.

    Attributes
    ----------
    decision_id : str
        Unique identifier for this decision
    task_id : str
        Task where this decision was made
    agent_id : str
        Agent who made the decision
    timestamp : datetime
        When the decision was made
    what : str
        What decision was made (the choice)
    why : str
        Rationale for the decision
    impact : str
        Expected impact on other tasks/components
    affected_tasks : list[str]
        Task IDs that will be affected by this decision
    confidence : float
        Agent's confidence in this decision (0.0-1.0)
    kanban_comment_url : Optional[str]
        URL to the Kanban comment where this was also logged
    project_id : Optional[str]
        Project identifier. Conversation logs remain the primary source for
        the project-task mapping of task-scoped decisions; ``project_id`` is
        load-bearing for project-level opinions (``task_id`` is None), which
        no task-id filter can reach.
    subject : Optional[str]
        Normalised slug naming what the entry is about (``farm.flooded``,
        ``schema.users``). Opinions on the same subject are counted
        together; architectural decisions may leave it empty.
    kind : str
        ``"decision"`` for an architectural decision made while doing a
        task, ``"assessment"`` for an opinion an agent posts to the board
        (deliberation layer).
    """

    decision_id: str
    task_id: Optional[str]
    agent_id: str
    timestamp: datetime
    what: str
    why: str
    impact: str
    affected_tasks: list[str] = field(default_factory=list)
    confidence: float = 0.8
    kanban_comment_url: Optional[str] = None
    project_id: Optional[str] = None
    subject: Optional[str] = None
    kind: str = "decision"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "decision_id": self.decision_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp.isoformat(),
            "what": self.what,
            "why": self.why,
            "impact": self.impact,
            "affected_tasks": self.affected_tasks,
            "confidence": self.confidence,
            "kanban_comment_url": self.kanban_comment_url,
            "project_id": self.project_id,
            "subject": self.subject,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        """Create from dictionary loaded from JSON.

        Rows written before the opinion fields existed load as task-scoped
        architectural decisions (``subject=None``, ``kind="decision"``).
        """
        # Parse timestamp and ensure it's timezone-aware
        ts = datetime.fromisoformat(data["timestamp"])
        if ts.tzinfo is None:
            # Make naive datetime timezone-aware (assume UTC)
            ts = ts.replace(tzinfo=timezone.utc)

        return cls(
            decision_id=data["decision_id"],
            task_id=data.get("task_id"),
            agent_id=data["agent_id"],
            timestamp=ts,
            what=data["what"],
            why=data["why"],
            impact=data["impact"],
            affected_tasks=data.get("affected_tasks", []),
            confidence=data.get("confidence", 0.8),
            kanban_comment_url=data.get("kanban_comment_url"),
            project_id=data.get("project_id"),
            subject=data.get("subject"),
            kind=data.get("kind", "decision"),
        )


@dataclass
class ArtifactMetadata:
    """
    Metadata for an artifact produced during task execution.

    Note: The actual artifact file persists in the project workspace.
    This is just the metadata registry.

    Attributes
    ----------
    artifact_id : str
        Unique identifier for this artifact
    task_id : str
        Task that produced this artifact
    agent_id : str
        Agent who created the artifact
    timestamp : datetime
        When the artifact was created
    filename : str
        Name of the artifact file
    artifact_type : str
        Type of artifact (design, specification, api, etc.)
    relative_path : str
        Path relative to project root
    absolute_path : str
        Full absolute path to the file
    description : str
        Description of what this artifact contains
    file_size_bytes : int
        Size of the file in bytes
    sha256_hash : Optional[str]
        SHA256 hash of file content (for integrity checking)
    kanban_comment_url : Optional[str]
        URL to Kanban comment where this was also logged
    referenced_by_tasks : list[str]
        Tasks that consumed this artifact
    project_id : Optional[str]
        Project identifier for validation/debugging. Note: Conversation logs
        are the authoritative source for project-task mapping. This field is
        stored for data integrity verification but is not used for filtering.
    """

    artifact_id: str
    task_id: str
    agent_id: str
    timestamp: datetime
    filename: str
    artifact_type: str
    relative_path: str
    absolute_path: str
    description: str
    file_size_bytes: int = 0
    sha256_hash: Optional[str] = None
    kanban_comment_url: Optional[str] = None
    referenced_by_tasks: list[str] = field(default_factory=list)
    project_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp.isoformat(),
            "filename": self.filename,
            "artifact_type": self.artifact_type,
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "description": self.description,
            "file_size_bytes": self.file_size_bytes,
            "sha256_hash": self.sha256_hash,
            "kanban_comment_url": self.kanban_comment_url,
            "referenced_by_tasks": self.referenced_by_tasks,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactMetadata":
        """Create from dictionary loaded from JSON."""
        # Parse timestamp and ensure it's timezone-aware
        ts = datetime.fromisoformat(data["timestamp"])
        if ts.tzinfo is None:
            # Make naive datetime timezone-aware (assume UTC)
            ts = ts.replace(tzinfo=timezone.utc)

        return cls(
            artifact_id=data["artifact_id"],
            task_id=data["task_id"],
            agent_id=data["agent_id"],
            timestamp=ts,
            filename=data["filename"],
            artifact_type=data["artifact_type"],
            relative_path=data["relative_path"],
            absolute_path=data["absolute_path"],
            description=data.get("description", ""),
            file_size_bytes=data.get("file_size_bytes", 0),
            sha256_hash=data.get("sha256_hash"),
            kanban_comment_url=data.get("kanban_comment_url"),
            referenced_by_tasks=data.get("referenced_by_tasks", []),
            project_id=data.get("project_id"),
        )


@dataclass
class ProjectSnapshot:
    """
    Complete snapshot of project state at completion.

    Captures final metrics, outcomes, and status for post-project analysis.
    """

    project_id: str
    project_name: str
    snapshot_timestamp: datetime
    completion_status: str  # "completed", "abandoned", "stalled"

    # Task statistics
    total_tasks: int
    completed_tasks: int
    in_progress_tasks: int
    blocked_tasks: int
    completion_rate: float

    # Timing
    project_started: datetime
    project_completed: Optional[datetime]
    total_duration_hours: float
    estimated_hours: float
    actual_hours: float
    estimation_accuracy: float

    # Team
    total_agents: int
    agent_summary: list[dict[str, Any]] = field(default_factory=list)

    # Quality metrics
    team_velocity: float = 0.0
    risk_level: str = "unknown"
    average_task_duration: float = 0.0
    blockage_rate: float = 0.0

    # Technology stack
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    # Outcome (user feedback)
    application_works: Optional[bool] = None
    deployment_status: Optional[str] = None
    user_satisfaction: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "snapshot_timestamp": self.snapshot_timestamp.isoformat(),
            "completion_status": self.completion_status,
            "task_statistics": {
                "total_tasks": self.total_tasks,
                "completed": self.completed_tasks,
                "in_progress": self.in_progress_tasks,
                "blocked": self.blocked_tasks,
                "completion_rate": self.completion_rate,
            },
            "timing": {
                "project_started": self.project_started.isoformat(),
                "project_completed": (
                    self.project_completed.isoformat()
                    if self.project_completed
                    else None
                ),
                "total_duration_hours": self.total_duration_hours,
                "estimated_hours": self.estimated_hours,
                "actual_hours": self.actual_hours,
                "estimation_accuracy": self.estimation_accuracy,
            },
            "team": {
                "total_agents": self.total_agents,
                "agents": self.agent_summary,
            },
            "quality_metrics": {
                "team_velocity": self.team_velocity,
                "risk_level": self.risk_level,
                "average_task_duration": self.average_task_duration,
                "blockage_rate": self.blockage_rate,
            },
            "technology_stack": {
                "languages": self.languages,
                "frameworks": self.frameworks,
                "tools": self.tools,
            },
            "outcome": {
                "application_works": self.application_works,
                "deployment_status": self.deployment_status,
                "user_satisfaction": self.user_satisfaction,
                "notes": self.notes,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSnapshot":
        """Create from dictionary loaded from JSON."""
        task_stats = data.get("task_statistics", {})
        timing = data.get("timing", {})
        team = data.get("team", {})
        quality = data.get("quality_metrics", {})
        tech_stack = data.get("technology_stack", {})
        outcome = data.get("outcome", {})

        return cls(
            project_id=data["project_id"],
            project_name=data["project_name"],
            snapshot_timestamp=datetime.fromisoformat(data["snapshot_timestamp"]),
            completion_status=data["completion_status"],
            total_tasks=task_stats.get("total_tasks", 0),
            completed_tasks=task_stats.get("completed", 0),
            in_progress_tasks=task_stats.get("in_progress", 0),
            blocked_tasks=task_stats.get("blocked", 0),
            completion_rate=task_stats.get("completion_rate", 0.0),
            project_started=datetime.fromisoformat(timing["project_started"]),
            project_completed=(
                datetime.fromisoformat(timing["project_completed"])
                if timing.get("project_completed")
                else None
            ),
            total_duration_hours=timing.get("total_duration_hours", 0.0),
            estimated_hours=timing.get("estimated_hours", 0.0),
            actual_hours=timing.get("actual_hours", 0.0),
            estimation_accuracy=timing.get("estimation_accuracy", 0.0),
            total_agents=team.get("total_agents", 0),
            agent_summary=team.get("agents", []),
            team_velocity=quality.get("team_velocity", 0.0),
            risk_level=quality.get("risk_level", "unknown"),
            average_task_duration=quality.get("average_task_duration", 0.0),
            blockage_rate=quality.get("blockage_rate", 0.0),
            languages=tech_stack.get("languages", []),
            frameworks=tech_stack.get("frameworks", []),
            tools=tech_stack.get("tools", []),
            application_works=outcome.get("application_works"),
            deployment_status=outcome.get("deployment_status"),
            user_satisfaction=outcome.get("user_satisfaction"),
            notes=outcome.get("notes", ""),
        )


class ProjectHistoryPersistence:
    """
    Manages persistent storage of project history data.

    Uses SQLite as primary storage for scalability:
    - decisions: Stored in persistence.decisions collection with project_id
    - artifacts: Stored in persistence.artifacts collection with project_id
    - snapshots: Stored in persistence.snapshots collection with project_id

    Also maintains file-based storage for archival/export purposes.
    """

    def __init__(self, marcus_root: Optional[Path] = None):
        """
        Initialize project history persistence.

        Parameters
        ----------
        marcus_root : Optional[Path]
            Path to Marcus root directory. If None, auto-detects.
        """
        if marcus_root is None:
            # Beside the rest of Marcus's state. Resolving against the
            # code checkout meant a live check with its own data
            # directory accumulated marcus.db and project_history/ inside
            # a git worktree (issue #792).
            # ``marcus_root`` keeps its original meaning, the code
            # checkout, because it is also where conversation logs are
            # read from (``logs/conversations``) and those are not state
            # that an isolated instance owns. Only the data moves.
            self.marcus_root = Path(__file__).parent.parent.parent
            data_dir = marcus_data_dir()
        else:
            # An explicit root keeps the historical layout, which the
            # analysis tools pass directly.
            self.marcus_root = Path(marcus_root)
            data_dir = self.marcus_root / "data"

        self.history_dir = data_dir / "project_history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

        # Initialize SQLite backend for primary storage
        self.db_path = data_dir / "marcus.db"

        # Create reusable backend instance for connection pooling
        from src.core.persistence import SQLitePersistence

        self._backend = SQLitePersistence(db_path=self.db_path)

        logger.info(f"ProjectHistoryPersistence initialized (SQLite: {self.db_path})")

    def _get_project_dir(self, project_id: str) -> Path:
        """Get directory for a specific project."""
        project_dir = self.history_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir

    def _atomic_write_json(self, file_path: Path, data: Any) -> None:
        """
        Atomically write JSON data to file.

        Writes to temporary file first, then renames to avoid corruption.
        """
        temp_file = file_path.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_file.replace(file_path)
        except Exception as e:
            if temp_file.exists():
                temp_file.unlink()
            raise e

    async def append_decision(
        self, project_id: str, project_name: str, decision: Decision
    ) -> None:
        """
        Append a decision to the project's decision registry.

        Writes to BOTH JSON (archival) and SQLite (queryable) storage.

        Parameters
        ----------
        project_id : str
            Project identifier
        project_name : str
            Project name
        decision : Decision
            Decision to append

        Notes
        -----
        Uses atomic writes to prevent corruption.
        Creates file if it doesn't exist.
        Persists to both JSON and SQLite for dual storage architecture.
        """
        project_dir = self._get_project_dir(project_id)
        decisions_file = project_dir / "decisions.json"

        # Load existing decisions or create new structure
        if decisions_file.exists():
            with open(decisions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "project_id": project_id,
                "project_name": project_name,
                "decisions": [],
                "metadata": {
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "total_decisions": 0,
                },
            }

        # Append new decision
        data["decisions"].append(decision.to_dict())
        data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["metadata"]["total_decisions"] = len(data["decisions"])

        # Atomic write to JSON (for archival/export)
        self._atomic_write_json(decisions_file, data)

        # ALSO write to SQLite (for queryable storage)
        await self._backend.store(
            collection="decisions",
            key=decision.decision_id,
            data=decision.to_dict(),
        )

        logger.info(f"Appended decision {decision.decision_id} to project {project_id}")

    async def append_artifact(
        self, project_id: str, project_name: str, artifact: ArtifactMetadata
    ) -> None:
        """
        Append artifact metadata to the project's artifact registry.

        Writes to BOTH JSON (archival) and SQLite (queryable) storage.

        Parameters
        ----------
        project_id : str
            Project identifier
        project_name : str
            Project name
        artifact : ArtifactMetadata
            Artifact metadata to append

        Notes
        -----
        Uses atomic writes to prevent corruption.
        Creates file if it doesn't exist.
        Persists to both JSON and SQLite for dual storage architecture.
        """
        project_dir = self._get_project_dir(project_id)
        artifacts_file = project_dir / "artifacts.json"

        # Load existing artifacts or create new structure
        if artifacts_file.exists():
            with open(artifacts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "project_id": project_id,
                "project_name": project_name,
                "project_root": str(Path(artifact.absolute_path).parent.parent),
                "artifacts": [],
                "metadata": {
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "total_artifacts": 0,
                    "total_size_mb": 0.0,
                },
            }

        # Append new artifact
        data["artifacts"].append(artifact.to_dict())
        data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["metadata"]["total_artifacts"] = len(data["artifacts"])
        data["metadata"]["total_size_mb"] = sum(
            a["file_size_bytes"] for a in data["artifacts"]
        ) / (1024 * 1024)

        # Atomic write to JSON (for archival/export)
        self._atomic_write_json(artifacts_file, data)

        # ALSO write to SQLite (for queryable storage)
        await self._backend.store(
            collection="artifacts",
            key=artifact.artifact_id,
            data=artifact.to_dict(),
        )

        logger.info(f"Appended artifact {artifact.artifact_id} to project {project_id}")

    async def save_snapshot(self, snapshot: ProjectSnapshot) -> None:
        """
        Save project completion snapshot.

        Parameters
        ----------
        snapshot : ProjectSnapshot
            Project snapshot to save

        Notes
        -----
        Overwrites any existing snapshot for this project.
        Uses atomic writes to prevent corruption.
        """
        project_dir = self._get_project_dir(snapshot.project_id)
        snapshot_file = project_dir / "snapshot.json"

        # Convert snapshot to dict
        data = snapshot.to_dict()

        # Atomic write
        self._atomic_write_json(snapshot_file, data)

        logger.info(f"Saved snapshot for project {snapshot.project_id}")

    #: Task ids per storage query; stays under SQLite's historical 999
    #: bound-variable limit with room for the other parameters.
    _TASK_ID_CHUNK = 500

    async def _load_project_rows(
        self, collection: str, project_id: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """
        Load the rows of ``collection`` that belong to ``project_id``, paged.

        A row belongs to the project when its stamped ``project_id``
        matches, or when its ``task_id`` is one of the project's tasks
        according to the conversation logs. The second clause is the
        pre-stamp safety net: it loads rows written before ``project_id``
        was stamped and rows stamped with another project while a task of
        this one was being worked. The first clause is the only route for
        a project-level opinion (``task_id`` None).

        Both clauses run as storage prefilters, so a shared database full
        of other projects' rows is never scanned in Python (Codex P2 on PR
        #800: the backend used to apply its limit before the project
        filter, and a project's rows behind enough newer foreign rows came
        back as an empty page). Each clause fetches ``limit + offset``
        rows, which is enough for the union's newest ``limit + offset`` to
        be exact; the union is ordered newest first and then paged.

        Parameters
        ----------
        collection : str
            ``"decisions"`` or ``"artifacts"``
        project_id : str
            Project identifier
        limit : int
            Maximum rows to return (capped at 10000)
        offset : int
            Rows to skip, newest first

        Returns
        -------
        list[dict[str, Any]]
            Raw rows, newest first, deduplicated by storage key
        """
        page_limit = min(limit, 10000)
        fetch = page_limit + offset
        rows = await self._backend.query(
            collection, where={"project_id": project_id}, limit=fetch
        )
        project_task_ids = sorted(
            await self._get_task_ids_from_conversations(project_id)
        )
        for start in range(0, len(project_task_ids), self._TASK_ID_CHUNK):
            chunk = project_task_ids[start : start + self._TASK_ID_CHUNK]
            rows.extend(
                await self._backend.query(
                    collection, where={"task_id": chunk}, limit=fetch
                )
            )

        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            key = str(row.get("_key", ""))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        # Newest first, with the key breaking same-second ties: the same
        # total order the backends page by, so the union of each clause's
        # newest ``fetch`` rows contains the union's newest ``fetch`` rows
        # and paging never overlaps.
        unique.sort(
            key=lambda r: (str(r.get("_stored_at", "")), str(r.get("_key", ""))),
            reverse=True,
        )
        return unique[offset : offset + page_limit]

    async def load_decisions(
        self, project_id: str, limit: int = 10000, offset: int = 0
    ) -> list[Decision]:
        """
        Load decisions for a project from SQLite with pagination.

        A row belongs to the project when its ``task_id`` is one of the
        project's tasks (derived from conversation logs) OR its
        ``project_id`` matches. The second clause is what makes a
        project-level opinion (``task_id`` None, deliberation layer B2)
        reachable: no task-id filter can match it, so ``project_id`` is
        load-bearing for it. Conversation logs remain the source for the
        task-scoped mapping, so rows written before ``project_id`` was
        stamped still load.

        Parameters
        ----------
        project_id : str
            Project identifier
        limit : int, optional
            Maximum number of decisions to return (default: 10000)
        offset : int, optional
            Number of decisions to skip (default: 0)

        Returns
        -------
        list[Decision]
            List of decisions (empty if none exist)
        """
        from src.core.error_framework import DatabaseError, error_context

        with error_context("load_decisions", custom_context={"project_id": project_id}):
            try:
                paginated_decisions = await self._load_project_rows(
                    "decisions", project_id, limit, offset
                )

                # Parse decisions
                decisions = []
                for dec_data in paginated_decisions:
                    try:
                        decision = Decision.from_dict(dec_data)
                        decisions.append(decision)
                    except Exception as e:
                        logger.debug(
                            f"Error parsing decision {dec_data.get('decision_id')}: {e}"
                        )
                        continue

                logger.debug(
                    f"Loaded {len(decisions)} decisions for project {project_id} "
                    f"(limit={limit}, offset={offset})"
                )
                return decisions

            except Exception as e:
                raise DatabaseError(
                    operation="load_decisions", table="decisions"
                ) from e

    async def load_artifacts(
        self, project_id: str, limit: int = 10000, offset: int = 0
    ) -> list[ArtifactMetadata]:
        """
        Load artifact metadata for a project from SQLite with pagination.

        Same membership rule and storage prefilters as ``load_decisions``:
        a row is the project's by its stamped ``project_id`` or by a
        ``task_id`` found in the project's conversation logs; see
        ``_load_project_rows``.

        Parameters
        ----------
        project_id : str
            Project identifier
        limit : int, optional
            Maximum number of artifacts to return (default: 10000)
        offset : int, optional
            Number of artifacts to skip (default: 0)

        Returns
        -------
        list[ArtifactMetadata]
            List of artifact metadata (empty if none exist)
        """
        from src.core.error_framework import DatabaseError, error_context

        with error_context("load_artifacts", custom_context={"project_id": project_id}):
            try:
                paginated_artifacts = await self._load_project_rows(
                    "artifacts", project_id, limit, offset
                )

                # Parse artifacts
                artifacts = []
                for art_data in paginated_artifacts:
                    try:
                        artifact = ArtifactMetadata.from_dict(art_data)
                        artifacts.append(artifact)
                    except Exception as e:
                        logger.debug(
                            f"Error parsing artifact {art_data.get('artifact_id')}: {e}"
                        )
                        continue

                logger.debug(
                    f"Loaded {len(artifacts)} artifacts for project {project_id} "
                    f"(limit={limit}, offset={offset})"
                )
                return artifacts

            except Exception as e:
                raise DatabaseError(
                    operation="load_artifacts", table="artifacts"
                ) from e

    async def load_snapshot(self, project_id: str) -> Optional[ProjectSnapshot]:
        """
        Load project completion snapshot.

        Parameters
        ----------
        project_id : str
            Project identifier

        Returns
        -------
        Optional[ProjectSnapshot]
            Project snapshot if exists, None otherwise
        """
        project_dir = self._get_project_dir(project_id)
        snapshot_file = project_dir / "snapshot.json"

        if not snapshot_file.exists():
            return None

        with open(snapshot_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        return ProjectSnapshot.from_dict(data)

    def list_projects(self) -> list[str]:
        """
        List all project IDs that have history data.

        Returns
        -------
        list[str]
            List of project IDs
        """
        if not self.history_dir.exists():
            return []

        return [
            d.name
            for d in self.history_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]

    async def _get_task_ids_from_conversations(self, project_id: str) -> set[str]:
        """
        Extract task IDs for a project from conversation logs.

        Parameters
        ----------
        project_id : str
            Project identifier

        Returns
        -------
        set[str]
            Set of task IDs found in project conversations
        """
        task_ids: set[str] = set()

        try:
            # Conversation logs are stored in logs/conversations/
            conversations_dir = self.marcus_root / "logs" / "conversations"
            if not conversations_dir.exists():
                logger.debug("No conversations directory found")
                return task_ids

            # Find all conversation JSONL files and filter by project_id
            for log_file in conversations_dir.glob("conversations_*.jsonl"):
                try:
                    with open(log_file, "r") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                                # Check if this message is for our project
                                metadata = entry.get("metadata", {})
                                if metadata.get("project_id") == project_id:
                                    # Extract task_id if present
                                    if "task_id" in metadata:
                                        task_ids.add(str(metadata["task_id"]))
                            except json.JSONDecodeError:
                                continue
                except Exception as e:
                    logger.debug(f"Error reading {log_file}: {e}")
                    continue

            logger.debug(
                f"Found {len(task_ids)} task IDs for project {project_id} "
                f"from conversations"
            )
            return task_ids

        except Exception as e:
            logger.warning(f"Error loading task IDs from conversations: {e}")
            return task_ids

    async def _get_all_project_ids_from_conversations(self) -> set[str]:
        """
        Extract all unique project IDs from conversation logs.

        Returns
        -------
        set[str]
            Set of all project IDs found in conversation logs
        """
        project_ids: set[str] = set()

        try:
            # Conversation logs are stored in logs/conversations/
            conversations_dir = self.marcus_root / "logs" / "conversations"
            if not conversations_dir.exists():
                logger.debug("No conversations directory found")
                return project_ids

            # Scan all conversation JSONL files
            for log_file in conversations_dir.glob("conversations_*.jsonl"):
                try:
                    with open(log_file, "r") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            try:
                                entry = json.loads(line)
                                # Extract project_id from metadata
                                metadata = entry.get("metadata", {})
                                if "project_id" in metadata:
                                    project_ids.add(str(metadata["project_id"]))
                            except json.JSONDecodeError:
                                continue
                except Exception as e:
                    logger.debug(f"Error reading {log_file}: {e}")
                    continue

            logger.debug(
                f"Found {len(project_ids)} unique project IDs from conversations"
            )
            return project_ids

        except Exception as e:
            logger.warning(f"Error loading project IDs from conversations: {e}")
            return project_ids
