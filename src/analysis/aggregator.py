"""
Project History Aggregator.

Unifies data from multiple sources to provide complete project execution history:
- Conversation logs (task instructions, agent communication)
- Agent events (task assignments, completions, blockers)
- Memory system (TaskOutcomes, AgentProfiles)
- Project history (Decisions, Artifacts, Snapshots)
- Kanban board (task status, comments)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.memory import AgentProfile, TaskOutcome
from src.core.paths import marcus_db_path, marcus_state_dir
from src.core.project_history import (
    ArtifactMetadata,
    Decision,
    ProjectHistoryPersistence,
    ProjectSnapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """A message from conversation logs."""

    timestamp: datetime
    direction: str  # "from_pm" or "to_pm"
    agent_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TimelineEvent:
    """A chronological event in project execution."""

    timestamp: datetime
    event_type: str  # "task_assigned", "decision_made", "artifact_created", etc.
    agent_id: Optional[str]
    task_id: Optional[str]
    description: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskHistory:
    """
    Complete history for a single task.

    Aggregates data from all sources to provide full task execution context.
    """

    # Basic info
    task_id: str
    name: str
    description: str
    status: str

    # Timing
    estimated_hours: float
    actual_hours: float
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Assignment
    assigned_to: Optional[str] = None

    # Instructions (CRITICAL - from conversation logs)
    instructions_received: Optional[str] = None

    # Context
    dependencies: list[str] = field(default_factory=list)
    context_received: dict[str, Any] = field(default_factory=dict)

    # Decisions
    decisions_made: list[Decision] = field(default_factory=list)
    decisions_consumed: list[Decision] = field(default_factory=list)

    # Artifacts
    artifacts_produced: list[ArtifactMetadata] = field(default_factory=list)
    artifacts_consumed: list[ArtifactMetadata] = field(default_factory=list)

    # Communication
    conversations: list[Message] = field(default_factory=list)
    blockers_reported: list[dict[str, Any]] = field(default_factory=list)

    # Outcome (from Memory system)
    outcome: Optional[TaskOutcome] = None

    # Analysis fields (populated in Phase 2)
    requirement_fidelity: Optional[float] = None
    instruction_quality: Optional[float] = None
    failure_causes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "estimated_hours": self.estimated_hours,
            "actual_hours": self.actual_hours,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "assigned_to": self.assigned_to,
            "instructions_received": self.instructions_received,
            "dependencies": self.dependencies,
            "context_received": self.context_received,
            "decisions_made": [d.to_dict() for d in self.decisions_made],
            "decisions_consumed": [d.to_dict() for d in self.decisions_consumed],
            "artifacts_produced": [a.to_dict() for a in self.artifacts_produced],
            "artifacts_consumed": [a.to_dict() for a in self.artifacts_consumed],
            "conversations_count": len(self.conversations),
            "blockers_count": len(self.blockers_reported),
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "requirement_fidelity": self.requirement_fidelity,
            "instruction_quality": self.instruction_quality,
            "failure_causes": self.failure_causes,
        }


@dataclass
class AgentHistory:
    """Complete history for a single agent."""

    agent_id: str
    tasks_completed: int
    tasks_blocked: int
    total_hours: float
    average_estimation_accuracy: float
    decisions_made: int
    artifacts_produced: int
    profile: Optional[AgentProfile] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        profile_dict = None
        if self.profile:
            # AgentProfile doesn't have to_dict, so manually serialize
            profile_dict = {
                "agent_id": self.profile.agent_id,
                "total_tasks": self.profile.total_tasks,
                "successful_tasks": self.profile.successful_tasks,
                "failed_tasks": self.profile.failed_tasks,
                "blocked_tasks": self.profile.blocked_tasks,
                "skill_success_rates": self.profile.skill_success_rates,
                "average_estimation_accuracy": self.profile.average_estimation_accuracy,
                "common_blockers": self.profile.common_blockers,
                "success_rate": self.profile.success_rate,
                "blockage_rate": self.profile.blockage_rate,
            }

        return {
            "agent_id": self.agent_id,
            "tasks_completed": self.tasks_completed,
            "tasks_blocked": self.tasks_blocked,
            "total_hours": self.total_hours,
            "average_estimation_accuracy": self.average_estimation_accuracy,
            "decisions_made": self.decisions_made,
            "artifacts_produced": self.artifacts_produced,
            "profile": profile_dict,
        }


@dataclass
class ProjectHistory:
    """
    Complete aggregated history for a project.

    Unifies all data sources to provide comprehensive view of project execution.
    """

    project_id: str
    project_name: str
    snapshot: Optional[ProjectSnapshot]
    tasks: list[TaskHistory]
    agents: list[AgentHistory]
    timeline: list[TimelineEvent]
    conversations: list[Message]
    decisions: list[Decision]
    artifacts: list[ArtifactMetadata]

    @property
    def task_by_id(self) -> dict[str, TaskHistory]:
        """Map task_id to TaskHistory for quick lookup."""
        return {t.task_id: t for t in self.tasks}

    @property
    def decision_impact_graph(self) -> dict[str, list[str]]:
        """Map decision_id -> affected_task_ids."""
        graph = {}
        for decision in self.decisions:
            graph[decision.decision_id] = decision.affected_tasks
        return graph

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "project_id": self.project_id,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "tasks": [t.to_dict() for t in self.tasks],
            "agents": [a.to_dict() for a in self.agents],
            "timeline_events": len(self.timeline),
            "decisions_count": len(self.decisions),
            "artifacts_count": len(self.artifacts),
        }


class ProjectHistoryAggregator:
    """
    Aggregates historical project data from multiple sources.

    Data Sources
    ------------
    - Conversation logs: Task instructions, agent communication
    - Agent events: Task assignments, status changes
    - Memory system: TaskOutcomes, AgentProfiles
    - Project history: Decisions, Artifacts, Snapshots (NEW)
    - Kanban board: Task status, comments (optional)

    Features
    --------
    - Unified view of project execution
    - Cross-referenced decisions and artifacts
    - Complete task history with instructions
    - Timeline reconstruction
    - Caching for performance (60s TTL like Cato)
    """

    def __init__(self, marcus_root: Optional[Path] = None):
        """
        Initialize the aggregator.

        Parameters
        ----------
        marcus_root : Optional[Path]
            Root directory of MARCUS installation. If None, auto-detects
            repository root to match ProjectHistoryPersistence.
        """
        explicit_root = marcus_root is not None
        if marcus_root is None:
            # Auto-detect Marcus root for the things that genuinely live
            # beside the code: conversation logs.
            marcus_root = Path(__file__).parent.parent.parent

        self.marcus_root = marcus_root
        # The history store is DATA, so it follows MARCUS_DATA_DIR unless a
        # caller passed a root on purpose. Passing the auto-detected
        # checkout here would hand ProjectHistoryPersistence an "explicit"
        # root and send reads to <checkout>/data while the writers in
        # tools/context.py and tools/attachment.py use the relocated
        # store, so every history query would come back empty or stale
        # (Codex P1 on PR #801).
        self.history_persistence = ProjectHistoryPersistence(
            marcus_root if explicit_root else None
        )
        self.logs_dir = marcus_root / "logs"
        # Reads and writes of marcus.db go through one attribute, so they
        # cannot drift apart from history_persistence above (#792).
        self.db_path = (
            marcus_root / "data" / "marcus.db" if explicit_root else marcus_db_path()
        )
        self.state_dir = (
            marcus_root / "data" / "marcus_state"
            if explicit_root
            else marcus_state_dir() / "marcus_state"
        )

        # Simple in-memory cache (60s TTL)
        self._cache: dict[str, tuple[ProjectHistory, datetime]] = {}
        self._cache_ttl = 60  # seconds

    async def aggregate_project(
        self,
        project_id: str,
        include_conversations: bool = True,
        include_kanban: bool = False,
        decision_limit: int = 10000,
        decision_offset: int = 0,
        artifact_limit: int = 10000,
        artifact_offset: int = 0,
    ) -> ProjectHistory:
        """
        Aggregate all historical data for a project with pagination.

        Parameters
        ----------
        project_id : str
            Project identifier
        include_conversations : bool
            Whether to load conversation logs (can be slow)
        include_kanban : bool
            Whether to load Kanban data (can be slow, requires client)
        decision_limit : int
            Maximum number of decisions to load (default: 10000)
        decision_offset : int
            Number of decisions to skip (default: 0)
        artifact_limit : int
            Maximum number of artifacts to load (default: 10000)
        artifact_offset : int
            Number of artifacts to skip (default: 0)

        Returns
        -------
        ProjectHistory
            Unified data structure with all project execution data
        """
        # Check cache
        if project_id in self._cache:
            cached_history, cached_at = self._cache[project_id]
            now = datetime.now(timezone.utc)
            age = (now - cached_at).total_seconds()
            if age < self._cache_ttl:
                logger.debug(f"Returning cached history for {project_id}")
                return cached_history

        # Load new persistent registries with pagination
        decisions = await self.history_persistence.load_decisions(
            project_id, limit=decision_limit, offset=decision_offset
        )
        artifacts = await self.history_persistence.load_artifacts(
            project_id, limit=artifact_limit, offset=artifact_offset
        )
        snapshot = await self.history_persistence.load_snapshot(project_id)

        # Extract project_name from available sources
        project_name = project_id  # Default fallback
        if snapshot:
            project_name = snapshot.project_name
        else:
            # Try to get from decisions.json or artifacts.json
            import json

            history_dir = self.history_persistence.history_dir / project_id
            for filename in ["decisions.json", "artifacts.json"]:
                file_path = history_dir / filename
                if file_path.exists():
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if "project_name" in data:
                                project_name = data["project_name"]
                                break
                    except Exception as e:
                        logger.debug(f"Error reading project_name from {filename}: {e}")
                        continue

        # Load existing data sources
        conversations = (
            await self._load_conversations(project_id) if include_conversations else []
        )
        events = await self._load_agent_events(project_id)
        outcomes = await self._load_task_outcomes(project_id)
        agent_profiles = await self._load_agent_profiles(project_id)
        task_metadata = await self._load_task_metadata(project_id)

        # Extract task_ids from conversations to filter outcomes
        task_ids_in_project = set()
        for msg in conversations:
            if "task_id" in msg.metadata:
                task_ids_in_project.add(str(msg.metadata["task_id"]))

        # Filter outcomes to only those with task_ids seen in conversations
        filtered_outcomes = [
            outcome for outcome in outcomes if outcome.task_id in task_ids_in_project
        ]

        # Filter events to only those with task_ids seen in conversations
        filtered_events = [
            event for event in events if event.get("task_id") in task_ids_in_project
        ]

        # Extract agent_ids from conversations to filter profiles
        agent_ids_in_project = set()
        for msg in conversations:
            if msg.agent_id:
                agent_ids_in_project.add(msg.agent_id)

        # Filter agent profiles to only those with agent_ids seen in conversations
        filtered_profiles = [
            profile
            for profile in agent_profiles
            if profile.agent_id in agent_ids_in_project
        ]

        # Build unified history
        tasks = self._build_task_histories(
            filtered_outcomes,
            decisions,
            artifacts,
            conversations,
            filtered_events,
            task_metadata,
        )
        agents = self._build_agent_histories(
            filtered_profiles, filtered_outcomes, decisions, artifacts
        )
        timeline = self._build_timeline(
            conversations, filtered_events, decisions, artifacts
        )

        history = ProjectHistory(
            project_id=project_id,
            project_name=project_name,
            snapshot=snapshot,
            tasks=tasks,
            agents=agents,
            timeline=timeline,
            conversations=conversations,
            decisions=decisions,
            artifacts=artifacts,
        )

        # Cache result
        self._cache[project_id] = (history, datetime.now(timezone.utc))

        return history

    async def _load_conversations(self, project_id: str) -> list[Message]:
        """
        Load conversation logs for project.

        Extracts messages from conversation_logger JSON files.
        """
        messages: list[Message] = []

        try:
            # Conversation logs are stored in logs/conversations/
            conversations_dir = self.logs_dir / "conversations"
            if not conversations_dir.exists():
                logger.debug("No conversations directory found")
                return messages

            # Find all conversation JSONL files and filter by project_id
            for log_file in conversations_dir.glob("conversations_*.jsonl"):
                try:
                    with open(log_file, "r") as f:
                        for line in f:
                            if not line.strip():
                                continue

                            try:
                                entry = json.loads(line)
                                metadata = entry.get("metadata", {})

                                # Filter by project_id
                                if metadata.get("project_id") == project_id:
                                    # Map conversation_type to direction
                                    conv_type = entry.get("conversation_type", "")
                                    if conv_type == "pm_to_worker":
                                        direction = "from_pm"
                                    elif conv_type == "worker_to_pm":
                                        direction = "to_pm"
                                    else:
                                        direction = conv_type

                                    messages.append(
                                        Message(
                                            timestamp=datetime.fromisoformat(
                                                entry["timestamp"]
                                            ),
                                            direction=direction,
                                            agent_id=entry.get("worker_id", "unknown"),
                                            content=entry.get("message", ""),
                                            metadata=metadata,
                                        )
                                    )
                            except (json.JSONDecodeError, KeyError, ValueError):
                                # Skip malformed lines
                                continue

                except Exception as e:
                    logger.warning(f"Error loading conversation log {log_file}: {e}")

        except Exception as e:
            logger.warning(f"Error loading conversations: {e}")

        return messages

    async def _load_agent_events(self, project_id: str) -> list[dict[str, Any]]:
        """Load agent events from Persistence backend."""
        events: list[dict[str, Any]] = []

        try:
            # Import Persistence and get backend
            from src.core.persistence import SQLitePersistence

            # Use absolute path to database (relative to marcus root)
            db_path = self.db_path
            backend = SQLitePersistence(db_path=db_path)

            # Query all events from persistence
            events_data = await backend.query("events", limit=10000)

            # Filter by project_id and clean up persistence fields
            for event_data in events_data:
                # Remove internal persistence fields
                event_data.pop("_key", None)
                event_data.pop("_stored_at", None)

                # Filter by project_id if present
                if "project_id" in event_data:
                    if event_data.get("project_id") == project_id:
                        events.append(event_data)
                else:
                    # Include events without project_id for now
                    events.append(event_data)

        except Exception as e:
            logger.warning(f"Error loading agent events: {e}")

        return events

    async def _load_task_outcomes(self, project_id: str) -> list[TaskOutcome]:
        """Load task outcomes from Memory system via Persistence backend."""
        outcomes: list[TaskOutcome] = []

        try:
            # Import Persistence and get backend
            from src.core.persistence import SQLitePersistence

            # Use absolute path to database (relative to marcus root)
            db_path = self.db_path
            backend = SQLitePersistence(db_path=db_path)

            # Query all task outcomes from persistence
            outcomes_data = await backend.query("task_outcomes", limit=10000)

            # Filter by project_id and reconstruct TaskOutcome objects
            for outcome_data in outcomes_data:
                # Check if this outcome belongs to the project
                # Task outcomes don't have project_id directly, so include all
                # for now and filter later by matching task_id with conversations

                try:
                    # Remove internal persistence fields
                    outcome_data.pop("_key", None)
                    outcome_data.pop("_stored_at", None)

                    # Parse timestamps and ensure they're UTC timezone-aware
                    started_at = None
                    if outcome_data.get("started_at"):
                        started_at = datetime.fromisoformat(outcome_data["started_at"])
                        if started_at.tzinfo is None:
                            started_at = started_at.replace(tzinfo=timezone.utc)

                    completed_at = None
                    if outcome_data.get("completed_at"):
                        completed_at = datetime.fromisoformat(
                            outcome_data["completed_at"]
                        )
                        if completed_at.tzinfo is None:
                            completed_at = completed_at.replace(tzinfo=timezone.utc)

                    outcomes.append(
                        TaskOutcome(
                            task_id=outcome_data["task_id"],
                            agent_id=outcome_data["agent_id"],
                            task_name=outcome_data.get("task_name", "Unknown"),
                            estimated_hours=outcome_data.get("estimated_hours", 0.0),
                            actual_hours=outcome_data.get("actual_hours", 0.0),
                            success=outcome_data.get("success", False),
                            blockers=outcome_data.get("blockers", []),
                            started_at=started_at,
                            completed_at=completed_at,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error reconstructing task outcome: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Error loading task outcomes: {e}")

        return outcomes

    async def _load_agent_profiles(self, project_id: str) -> list[AgentProfile]:
        """Load agent profiles from Memory system via Persistence backend."""
        profiles: list[AgentProfile] = []

        try:
            # Import Persistence and get backend
            from src.core.persistence import SQLitePersistence

            # Use absolute path to database (relative to marcus root)
            db_path = self.db_path
            backend = SQLitePersistence(db_path=db_path)

            # Query all agent profiles from persistence
            profiles_data = await backend.query("agent_profiles", limit=10000)

            # Filter by project_id and reconstruct AgentProfile objects
            for profile_data in profiles_data:
                # Check if this profile belongs to the project
                # Agent profiles don't have project_id directly, so we'll include all
                # for now and filter later by matching agent_id with conversations

                try:
                    # Remove internal persistence fields
                    profile_data.pop("_key", None)
                    profile_data.pop("_stored_at", None)

                    profiles.append(
                        AgentProfile(
                            agent_id=profile_data["agent_id"],
                            total_tasks=profile_data.get("total_tasks", 0),
                            successful_tasks=profile_data.get("successful_tasks", 0),
                            failed_tasks=profile_data.get("failed_tasks", 0),
                            blocked_tasks=profile_data.get("blocked_tasks", 0),
                            skill_success_rates=profile_data.get(
                                "skill_success_rates", {}
                            ),
                            average_estimation_accuracy=profile_data.get(
                                "average_estimation_accuracy", 0.0
                            ),
                            common_blockers=profile_data.get("common_blockers", {}),
                            peak_performance_hours=profile_data.get(
                                "peak_performance_hours", []
                            ),
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error reconstructing agent profile: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Error loading agent profiles: {e}")

        return profiles

    async def _load_task_metadata(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Load task metadata (descriptions, etc.) from Persistence backend."""
        metadata_by_task_id: dict[str, dict[str, Any]] = {}

        try:
            # Import Persistence and get backend
            from src.core.persistence import SQLitePersistence

            # Use absolute path to database (relative to marcus root)
            db_path = self.db_path
            backend = SQLitePersistence(db_path=db_path)

            # Query all task metadata from persistence
            metadata_list = await backend.query("task_metadata", limit=10000)

            # Build dict keyed by task_id
            for metadata in metadata_list:
                task_id = metadata.get("task_id")
                if task_id:
                    metadata_by_task_id[str(task_id)] = metadata

        except Exception as e:
            logger.warning(f"Error loading task metadata: {e}")

        return metadata_by_task_id

    def _build_task_histories(
        self,
        outcomes: list[TaskOutcome],
        decisions: list[Decision],
        artifacts: list[ArtifactMetadata],
        conversations: list[Message],
        events: list[dict[str, Any]],
        task_metadata: dict[str, dict[str, Any]] = {},
    ) -> list[TaskHistory]:
        """
        Build comprehensive task histories.

        Combines data from all sources to create TaskHistory objects.
        """
        task_histories: dict[str, TaskHistory] = {}

        # Start with outcomes (provides basic task info)
        for outcome in outcomes:
            task_id = outcome.task_id

            # Get description from task_metadata if available
            metadata = task_metadata.get(task_id, {})
            description = metadata.get("description", "")

            # Get agent_id from outcome if available
            agent_id = getattr(outcome, "agent_id", None)

            task_histories[task_id] = TaskHistory(
                task_id=task_id,
                name=getattr(outcome, "task_name", task_id),
                description=description,
                status="completed" if outcome.success else "failed",
                estimated_hours=outcome.estimated_hours,
                actual_hours=outcome.actual_hours,
                outcome=outcome,
                assigned_to=agent_id,  # Set from outcome
            )

        # Add decisions made by each task
        for decision in decisions:
            decision_task_id = decision.task_id
            if decision_task_id is None:
                # A project-level opinion (deliberation layer) was made
                # by no task; it is read through the opinions query.
                continue
            task_id = decision_task_id
            if task_id not in task_histories:
                # Create minimal task history if not found
                task_histories[task_id] = TaskHistory(
                    task_id=task_id,
                    name=task_id,
                    description="",
                    status="unknown",
                    estimated_hours=0.0,
                    actual_hours=0.0,
                )

            task_histories[task_id].decisions_made.append(decision)

        # Add artifacts produced by each task
        for artifact in artifacts:
            task_id = artifact.task_id
            if task_id not in task_histories:
                task_histories[task_id] = TaskHistory(
                    task_id=task_id,
                    name=task_id,
                    description="",
                    status="unknown",
                    estimated_hours=0.0,
                    actual_hours=0.0,
                )

            task_histories[task_id].artifacts_produced.append(artifact)

        # Extract instructions from conversations
        for message in conversations:
            task_id_raw = message.metadata.get("task_id")
            if (
                task_id_raw
                and isinstance(task_id_raw, str)
                and task_id_raw in task_histories
            ):
                # Check if this is a task assignment message
                if "instructions" in message.metadata:
                    task_histories[task_id_raw].instructions_received = str(
                        message.metadata["instructions"]
                    )

                task_histories[task_id_raw].conversations.append(message)

        # Add timing and assignment from events
        for event in events:
            task_id_raw = event.get("task_id")
            if (
                task_id_raw
                and isinstance(task_id_raw, str)
                and task_id_raw in task_histories
            ):
                event_type = event.get("event_type")

                if event_type == "task_assigned":
                    agent_id = event.get("agent_id")
                    if isinstance(agent_id, str):
                        task_histories[task_id_raw].assigned_to = agent_id
                    if "timestamp" in event:
                        task_histories[task_id_raw].started_at = datetime.fromisoformat(
                            event["timestamp"]
                        )

                elif event_type == "task_completed":
                    if "timestamp" in event:
                        task_histories[task_id_raw].completed_at = (
                            datetime.fromisoformat(event["timestamp"])
                        )

        return list(task_histories.values())

    def _build_agent_histories(
        self,
        profiles: list[AgentProfile],
        outcomes: list[TaskOutcome],
        decisions: list[Decision],
        artifacts: list[ArtifactMetadata],
    ) -> list[AgentHistory]:
        """Build agent histories from profiles and execution data."""
        agent_histories: dict[str, AgentHistory] = {}

        # Start with profiles
        for profile in profiles:
            agent_histories[profile.agent_id] = AgentHistory(
                agent_id=profile.agent_id,
                tasks_completed=0,
                tasks_blocked=0,
                total_hours=0.0,
                average_estimation_accuracy=0.0,
                decisions_made=0,
                artifacts_produced=0,
                profile=profile,
            )

        # Add task completion statistics
        for outcome in outcomes:
            agent_id = getattr(outcome, "agent_id", None)
            if agent_id:
                if agent_id not in agent_histories:
                    agent_histories[agent_id] = AgentHistory(
                        agent_id=agent_id,
                        tasks_completed=0,
                        tasks_blocked=0,
                        total_hours=0.0,
                        average_estimation_accuracy=0.0,
                        decisions_made=0,
                        artifacts_produced=0,
                    )

                agent_histories[agent_id].tasks_completed += 1
                agent_histories[agent_id].total_hours += outcome.actual_hours

        # Count decisions per agent
        for decision in decisions:
            if decision.agent_id in agent_histories:
                agent_histories[decision.agent_id].decisions_made += 1

        # Count artifacts per agent
        for artifact in artifacts:
            if artifact.agent_id in agent_histories:
                agent_histories[artifact.agent_id].artifacts_produced += 1

        return list(agent_histories.values())

    def _build_timeline(
        self,
        conversations: list[Message],
        events: list[dict[str, Any]],
        decisions: list[Decision],
        artifacts: list[ArtifactMetadata],
    ) -> list[TimelineEvent]:
        """Build chronological timeline of project events."""
        timeline: list[TimelineEvent] = []

        # Add decisions
        for decision in decisions:
            timeline.append(
                TimelineEvent(
                    timestamp=decision.timestamp,
                    event_type="decision_made",
                    agent_id=decision.agent_id,
                    task_id=decision.task_id,
                    description=f"Decision: {decision.what}",
                    details=decision.to_dict(),
                )
            )

        # Add artifacts
        for artifact in artifacts:
            timeline.append(
                TimelineEvent(
                    timestamp=artifact.timestamp,
                    event_type="artifact_created",
                    agent_id=artifact.agent_id,
                    task_id=artifact.task_id,
                    description=(
                        f"Created {artifact.artifact_type}: {artifact.filename}"
                    ),
                    details=artifact.to_dict(),
                )
            )

        # Add events
        for event in events:
            if "timestamp" in event:
                # Parse timestamp and ensure it's timezone-aware
                ts = datetime.fromisoformat(event["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                timeline.append(
                    TimelineEvent(
                        timestamp=ts,
                        event_type=event.get("event_type", "unknown"),
                        agent_id=event.get("agent_id"),
                        task_id=event.get("task_id"),
                        description=event.get("description", ""),
                        details=event,
                    )
                )

        # Sort chronologically
        timeline.sort(key=lambda e: e.timestamp)

        return timeline
