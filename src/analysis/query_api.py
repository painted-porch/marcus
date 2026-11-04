"""
Query API for Project History.

Provides convenient methods for filtering and searching project execution data:
- Find tasks by status, agent, time range
- Find decisions by task or impact
- Find artifacts by type or task
- Analyze task dependencies and chains
- Search conversations and events
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.analysis.aggregator import (
    AgentHistory,
    Message,
    ProjectHistory,
    ProjectHistoryAggregator,
    TaskHistory,
    TimelineEvent,
)
from src.core.project_history import ArtifactMetadata, Decision

#: Rows per ``load_decisions`` page when a read must see every decision.
DECISION_PAGE_SIZE = 10000


def _as_utc(moment: datetime) -> datetime:
    """Read a stored timestamp as UTC when it carries no timezone."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def group_opinions_by_subject(
    decisions: list[Decision],
    subject: Optional[str] = None,
    kind: str = "assessment",
    since: Optional[str] = None,
    window_minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """
    Group opinions into one entry per subject (deliberation layer, B2).

    The count that matters for corroboration is the number of DISTINCT
    agents who posted on a subject, so one agent repeating itself does
    not raise a subject's weight; ``opinion_count`` keeps the raw total.

    Parameters
    ----------
    decisions : list[Decision]
        The project's decisions, opinions included.
    subject : Optional[str]
        Keep only this subject (already normalised).
    kind : str
        Which kind to group: ``"assessment"`` (opinions, the default) or
        ``"decision"``. Rows with no subject are never grouped.
    since : Optional[str]
        ISO-8601 timestamp; rows before it are ignored.
    window_minutes : Optional[int]
        When given and positive, each entry also reports how many
        DISTINCT agents posted within the last ``window_minutes``, and
        the read is ordered by that figure first.

        A pheromone evaporates, and the reason is not tidiness: without
        age, a hazard reported an hour ago weighs exactly as much as one
        reported a minute ago, so a priority signal cannot tell "three
        agents think this now" from "three agents thought it at different
        times for different reasons". Nothing is deleted or hidden, since
        a faded subject is still worth seeing; the kernel reports both
        figures and holds no policy about which one matters, because how
        fast a signal should fade belongs to the domain, not here.
    now : Optional[datetime]
        The instant the window is measured back from; defaults to the
        current UTC time and exists so tests can fix it.

    Returns
    -------
    list[dict[str, Any]]
        One entry per subject, most corroborated first, then most recent:
        ``subject``, ``kind``, ``count`` (distinct agents), ``opinion_count``,
        ``agents`` (sorted), ``first_seen``, ``last_seen``, ``task_ids``
        (sorted, project-level rows contribute none) and ``opinions``
        (the rows themselves, newest first). With a window, also
        ``recent_count``, ``recent_agents`` and ``window_minutes``.
    """
    since_ts: Optional[datetime] = None
    if since:
        since_ts = _as_utc(datetime.fromisoformat(since))

    windowed = bool(window_minutes and window_minutes > 0)
    window_start: Optional[datetime] = None
    if windowed:
        measured_from = now or datetime.now(timezone.utc)
        if measured_from.tzinfo is None:
            measured_from = measured_from.replace(tzinfo=timezone.utc)
        window_start = measured_from - timedelta(minutes=int(window_minutes or 0))

    groups: dict[str, list[Decision]] = {}
    for decision in decisions:
        if decision.kind != kind or not decision.subject:
            continue
        if subject and decision.subject != subject:
            continue
        timestamp = decision.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if since_ts and timestamp < since_ts:
            continue
        groups.setdefault(decision.subject, []).append(decision)

    entries: list[dict[str, Any]] = []
    for group_subject, items in groups.items():
        items.sort(key=lambda d: d.timestamp, reverse=True)
        agents = sorted({d.agent_id for d in items})
        task_ids = sorted({d.task_id for d in items if d.task_id})
        entry: dict[str, Any] = {
            "subject": group_subject,
            "kind": kind,
            "count": len(agents),
            "opinion_count": len(items),
            "agents": agents,
            "first_seen": items[-1].timestamp.isoformat(),
            "last_seen": items[0].timestamp.isoformat(),
            "task_ids": task_ids,
            "opinions": [d.to_dict() for d in items],
        }
        if windowed and window_start is not None:
            recent = sorted(
                {d.agent_id for d in items if _as_utc(d.timestamp) >= window_start}
            )
            entry["recent_count"] = len(recent)
            entry["recent_agents"] = recent
            entry["window_minutes"] = int(window_minutes or 0)
        entries.append(entry)

    # Most recent first, then (stable) most corroborated first. With a
    # window, who still thinks so outranks who ever did.
    entries.sort(key=lambda e: e["last_seen"], reverse=True)
    entries.sort(key=lambda e: e["count"], reverse=True)
    if windowed:
        entries.sort(key=lambda e: e.get("recent_count", 0), reverse=True)
    return entries


class ProjectHistoryQuery:
    """
    Query interface for project history with filtering and search capabilities.

    Provides high-level queries for common analysis needs:
    - Task filtering by status, assignee, time
    - Decision impact analysis
    - Artifact tracing
    - Dependency chain analysis
    - Timeline event search

    Parameters
    ----------
    aggregator : ProjectHistoryAggregator
        The aggregator to query data from
    """

    def __init__(self, aggregator: ProjectHistoryAggregator):
        """Initialize query API with aggregator."""
        self.aggregator = aggregator

    async def get_project_history(
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
        Get complete project history with pagination.

        Parameters
        ----------
        project_id : str
            Project to query
        include_conversations : bool
            Whether to include conversation logs (default: True)
        include_kanban : bool
            Whether to include Kanban data (default: False)
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
            Complete aggregated project history
        """
        return await self.aggregator.aggregate_project(
            project_id=project_id,
            include_conversations=include_conversations,
            include_kanban=include_kanban,
            decision_limit=decision_limit,
            decision_offset=decision_offset,
            artifact_limit=artifact_limit,
            artifact_offset=artifact_offset,
        )

    # Task Queries

    async def find_tasks_by_status(
        self, project_id: str, status: str
    ) -> list[TaskHistory]:
        """
        Find all tasks with a specific status.

        Parameters
        ----------
        project_id : str
            Project to search
        status : str
            Task status to filter by (e.g., "completed", "in_progress", "blocked")

        Returns
        -------
        list[TaskHistory]
            Tasks matching the status
        """
        history = await self.get_project_history(project_id)
        return [task for task in history.tasks if task.status == status]

    async def find_tasks_by_agent(
        self, project_id: str, agent_id: str
    ) -> list[TaskHistory]:
        """
        Find all tasks assigned to a specific agent.

        Parameters
        ----------
        project_id : str
            Project to search
        agent_id : str
            Agent ID to filter by

        Returns
        -------
        list[TaskHistory]
            Tasks assigned to the agent
        """
        history = await self.get_project_history(project_id)
        return [task for task in history.tasks if task.assigned_to == agent_id]

    async def find_tasks_in_timerange(
        self,
        project_id: str,
        start_time: datetime,
        end_time: Optional[datetime] = None,
    ) -> list[TaskHistory]:
        """
        Find tasks started or completed within a time range.

        Parameters
        ----------
        project_id : str
            Project to search
        start_time : datetime
            Start of time range
        end_time : Optional[datetime]
            End of time range (default: now)

        Returns
        -------
        list[TaskHistory]
            Tasks in the time range
        """
        if end_time is None:
            end_time = datetime.now(timezone.utc)

        history = await self.get_project_history(project_id)
        matching_tasks = []

        for task in history.tasks:
            # Check if task started or completed in range
            if task.started_at and start_time <= task.started_at <= end_time:
                matching_tasks.append(task)
            elif task.completed_at and start_time <= task.completed_at <= end_time:
                matching_tasks.append(task)

        return matching_tasks

    async def find_blocked_tasks(self, project_id: str) -> list[TaskHistory]:
        """
        Find all tasks that reported blockers.

        Parameters
        ----------
        project_id : str
            Project to search

        Returns
        -------
        list[TaskHistory]
            Tasks with reported blockers
        """
        history = await self.get_project_history(project_id)
        return [task for task in history.tasks if task.blockers_reported]

    async def get_task_dependency_chain(
        self, project_id: str, task_id: str
    ) -> list[TaskHistory]:
        """
        Get the full dependency chain for a task (all upstream dependencies).

        Parameters
        ----------
        project_id : str
            Project to search
        task_id : str
            Task to analyze

        Returns
        -------
        list[TaskHistory]
            All tasks in the dependency chain (ordered: dependencies first)
        """
        history = await self.get_project_history(project_id)

        # Build task lookup
        task_map = {task.task_id: task for task in history.tasks}

        # Find target task
        target_task = task_map.get(task_id)
        if not target_task:
            return []

        # BFS to find all dependencies
        chain = []
        visited = set()
        queue = list(target_task.dependencies)

        while queue:
            dep_id = queue.pop(0)
            if dep_id in visited:
                continue

            visited.add(dep_id)
            dep_task = task_map.get(dep_id)
            if dep_task:
                chain.append(dep_task)
                queue.extend(dep_task.dependencies)

        return chain

    # Decision Queries

    async def find_decisions_by_task(
        self, project_id: str, task_id: str
    ) -> list[Decision]:
        """
        Find all decisions made during a specific task.

        Parameters
        ----------
        project_id : str
            Project to search
        task_id : str
            Task ID to filter by

        Returns
        -------
        list[Decision]
            Decisions made during the task
        """
        history = await self.get_project_history(project_id)
        return [d for d in history.decisions if d.task_id == task_id]

    async def find_decisions_by_agent(
        self, project_id: str, agent_id: str
    ) -> list[Decision]:
        """
        Find all decisions made by a specific agent.

        Parameters
        ----------
        project_id : str
            Project to search
        agent_id : str
            Agent ID to filter by

        Returns
        -------
        list[Decision]
            Decisions made by the agent
        """
        history = await self.get_project_history(project_id)
        return [d for d in history.decisions if d.agent_id == agent_id]

    async def find_decisions_affecting_task(
        self, project_id: str, task_id: str
    ) -> list[Decision]:
        """
        Find decisions that affect a specific task (from dependencies).

        Parameters
        ----------
        project_id : str
            Project to search
        task_id : str
            Task to check impact on

        Returns
        -------
        list[Decision]
            Decisions that affect this task
        """
        history = await self.get_project_history(project_id)
        return [d for d in history.decisions if task_id in d.affected_tasks]

    async def summarize_subjects(
        self,
        project_id: str,
        subject: Optional[str] = None,
        kind: str = "assessment",
        since: Optional[str] = None,
        window_minutes: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        Group the project's opinions by subject (deliberation layer, B2).

        Reads the decision registry directly rather than the aggregated
        history: opinions are read on an agent's pull cadence, and the
        aggregate (conversations, timeline, cache) is far heavier than
        the list this needs.

        Parameters
        ----------
        project_id : str
            Project to query
        subject : Optional[str]
            Keep only this subject (already normalised)
        kind : str
            ``"assessment"`` (default) or ``"decision"``
        since : Optional[str]
            ISO-8601 timestamp; earlier rows are ignored
        window_minutes : Optional[int]
            Also report how many distinct agents posted within this many
            minutes, and order the read by that; see
            ``group_opinions_by_subject``.

        Returns
        -------
        list[dict[str, Any]]
            One entry per subject; see ``group_opinions_by_subject``.
        """
        # Walk every page: ``load_decisions`` caps a page at
        # DECISION_PAGE_SIZE, and grouping a single page would undercount
        # corroboration on a large project and could drop subjects
        # outright (Codex P2 on PR #800). The loop stops on the first
        # empty page rather than the first short one, because a page
        # can come back short when malformed rows were skipped.
        decisions: list[Decision] = []
        offset = 0
        while True:
            page = await self.aggregator.history_persistence.load_decisions(
                project_id, limit=DECISION_PAGE_SIZE, offset=offset
            )
            if not page:
                break
            decisions.extend(page)
            offset += DECISION_PAGE_SIZE
        return group_opinions_by_subject(
            decisions,
            subject=subject,
            kind=kind,
            since=since,
            window_minutes=window_minutes,
        )

    # Artifact Queries

    async def find_artifacts_by_task(
        self, project_id: str, task_id: str
    ) -> list[ArtifactMetadata]:
        """
        Find all artifacts produced by a specific task.

        Parameters
        ----------
        project_id : str
            Project to search
        task_id : str
            Task ID to filter by

        Returns
        -------
        list[ArtifactMetadata]
            Artifacts produced by the task
        """
        history = await self.get_project_history(project_id)
        return [a for a in history.artifacts if a.task_id == task_id]

    async def find_artifacts_by_type(
        self, project_id: str, artifact_type: str
    ) -> list[ArtifactMetadata]:
        """
        Find all artifacts of a specific type.

        Parameters
        ----------
        project_id : str
            Project to search
        artifact_type : str
            Artifact type to filter by (e.g., "specification", "design")

        Returns
        -------
        list[ArtifactMetadata]
            Artifacts of the specified type
        """
        history = await self.get_project_history(project_id)
        return [a for a in history.artifacts if a.artifact_type == artifact_type]

    async def find_artifacts_by_agent(
        self, project_id: str, agent_id: str
    ) -> list[ArtifactMetadata]:
        """
        Find all artifacts produced by a specific agent.

        Parameters
        ----------
        project_id : str
            Project to search
        agent_id : str
            Agent ID to filter by

        Returns
        -------
        list[ArtifactMetadata]
            Artifacts produced by the agent
        """
        history = await self.get_project_history(project_id)
        return [a for a in history.artifacts if a.agent_id == agent_id]

    # Agent Queries

    async def get_agent_history(
        self, project_id: str, agent_id: str
    ) -> Optional[AgentHistory]:
        """
        Get complete history for a specific agent.

        Parameters
        ----------
        project_id : str
            Project to search
        agent_id : str
            Agent ID to retrieve

        Returns
        -------
        Optional[AgentHistory]
            Agent history or None if not found
        """
        history = await self.get_project_history(project_id)
        for agent in history.agents:
            if agent.agent_id == agent_id:
                return agent
        return None

    async def get_agent_performance_metrics(
        self, project_id: str, agent_id: str
    ) -> dict[str, Any]:
        """
        Calculate performance metrics for an agent.

        Parameters
        ----------
        project_id : str
            Project to analyze
        agent_id : str
            Agent to analyze

        Returns
        -------
        dict[str, Any]
            Performance metrics including:
            - tasks_completed: Number of completed tasks
            - tasks_blocked: Number of blocked tasks
            - avg_task_hours: Average hours per task
            - decisions_made: Number of decisions
            - artifacts_produced: Number of artifacts
        """
        agent = await self.get_agent_history(project_id, agent_id)
        if not agent:
            return {
                "agent_id": agent_id,
                "error": "Agent not found",
            }

        tasks = await self.find_tasks_by_agent(project_id, agent_id)
        completed_tasks = [t for t in tasks if t.status == "completed"]
        blocked_tasks = [t for t in tasks if t.blockers_reported]

        # Calculate average hours for completed tasks
        total_hours = sum(t.actual_hours for t in completed_tasks if t.actual_hours > 0)
        avg_hours = total_hours / len(completed_tasks) if completed_tasks else 0.0

        return {
            "agent_id": agent_id,
            "tasks_assigned": len(tasks),
            "tasks_completed": len(completed_tasks),
            "tasks_blocked": len(blocked_tasks),
            "avg_task_hours": round(avg_hours, 2),
            "decisions_made": agent.decisions_made,
            "artifacts_produced": agent.artifacts_produced,
        }

    # Timeline Queries

    async def search_timeline(
        self,
        project_id: str,
        event_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[TimelineEvent]:
        """
        Search timeline events with multiple filters.

        Parameters
        ----------
        project_id : str
            Project to search
        event_type : Optional[str]
            Filter by event type (e.g., "task_assigned", "decision_logged")
        agent_id : Optional[str]
            Filter by agent
        task_id : Optional[str]
            Filter by task
        start_time : Optional[datetime]
            Start of time range
        end_time : Optional[datetime]
            End of time range

        Returns
        -------
        list[TimelineEvent]
            Matching timeline events (chronologically ordered)
        """
        history = await self.get_project_history(project_id)
        events = history.timeline

        # Apply filters
        if event_type:
            events = [e for e in events if e.event_type == event_type]

        if agent_id:
            events = [e for e in events if e.agent_id == agent_id]

        if task_id:
            events = [e for e in events if e.task_id == task_id]

        if start_time:
            events = [e for e in events if e.timestamp >= start_time]

        if end_time:
            events = [e for e in events if e.timestamp <= end_time]

        return events

    async def search_conversations(
        self,
        project_id: str,
        keyword: Optional[str] = None,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> list[Message]:
        """
        Search conversation messages.

        Parameters
        ----------
        project_id : str
            Project to search
        keyword : Optional[str]
            Search for keyword in message content (case-insensitive)
        agent_id : Optional[str]
            Filter by agent (sender or receiver)
        task_id : Optional[str]
            Filter by associated task

        Returns
        -------
        list[Message]
            Matching messages (chronologically ordered)
        """
        history = await self.get_project_history(project_id, include_conversations=True)

        # Flatten all conversations from all tasks
        messages: list[Message] = []
        for task in history.tasks:
            messages.extend(task.conversations)

        # Apply filters
        if keyword:
            keyword_lower = keyword.lower()
            messages = [m for m in messages if keyword_lower in m.content.lower()]

        if agent_id:
            messages = [
                m
                for m in messages
                if m.agent_id == agent_id or m.metadata.get("receiver_id") == agent_id
            ]

        if task_id:
            messages = [m for m in messages if m.metadata.get("task_id") == task_id]

        return messages

    # Analysis Helpers

    async def get_project_summary(self, project_id: str) -> dict[str, Any]:
        """
        Get high-level summary statistics for a project.

        Parameters
        ----------
        project_id : str
            Project to summarize

        Returns
        -------
        dict[str, Any]
            Summary including:
            - total_tasks: Total number of tasks
            - completed_tasks: Number completed
            - blocked_tasks: Number blocked
            - total_decisions: Total decisions made
            - total_artifacts: Total artifacts produced
            - active_agents: Number of agents who worked
            - project_duration_hours: Time from first to last event
        """
        history = await self.get_project_history(project_id)

        completed = len([t for t in history.tasks if t.status == "completed"])
        blocked = len([t for t in history.tasks if t.blockers_reported])

        # Calculate project duration
        duration_hours = 0.0
        if history.timeline:
            first_event = min(history.timeline, key=lambda e: e.timestamp)
            last_event = max(history.timeline, key=lambda e: e.timestamp)
            duration = last_event.timestamp - first_event.timestamp
            duration_hours = duration.total_seconds() / 3600

        return {
            "project_id": project_id,
            "project_name": history.project_name,
            "total_tasks": len(history.tasks),
            "completed_tasks": completed,
            "blocked_tasks": blocked,
            "completion_rate": (
                round(completed / len(history.tasks) * 100, 1) if history.tasks else 0.0
            ),
            "total_decisions": len(history.decisions),
            "total_artifacts": len(history.artifacts),
            "active_agents": len(history.agents),
            "project_duration_hours": round(duration_hours, 2),
        }
