"""
Project History Query Tools for Marcus MCP.

Exposes ProjectHistoryQuery API to agents via MCP protocol,
enabling agents to:
- Query task execution history
- Search decisions and their impact
- Find artifacts produced during execution
- Analyze agent performance
- Search conversations and timeline events
- Generate project summaries
"""

import logging
from typing import Any, Optional

from src.analysis.aggregator import ProjectHistoryAggregator
from src.analysis.query_api import ProjectHistoryQuery

logger = logging.getLogger(__name__)

#: How many decision rows a kind-filtered read scans before paging. The
#: store's own ceiling, so a filtered read sees everything an unfiltered
#: one could (#881).
_DECISION_SCAN_CAP = 10000


async def query_project_history(
    project_id: str,
    query_type: str,
    state: Any,
    **filters: Any,
) -> dict[str, Any]:
    """
    Query project history with flexible filtering and pagination.

    This unified MCP tool provides access to all project history queries.
    Agents can query tasks, decisions, artifacts, agents, timeline, and
    conversations.

    Parameters
    ----------
    project_id : str
        Project to query
    query_type : str
        Type of query to perform:
        - "summary": Get project summary statistics
        - "tasks": Find tasks (filters: status, agent_id, start_time, end_time)
        - "blocked_tasks": Find tasks with blockers
        - "task_dependencies": Get dependency chain (requires: task_id)
        - "decisions": Find decisions (filters: task_id, agent_id, affecting_task_id,
          kind; ``kind="decision"`` leaves opinions and the primer out)
        - "opinions": Opinions grouped by subject with the distinct-agent count
          (filters: subject, kind, since); pagination is over subjects
        - "artifacts": Find artifacts (filters: task_id, artifact_type, agent_id)
        - "agent_history": Get agent history (requires: agent_id)
        - "agent_metrics": Get agent performance metrics (requires: agent_id)
        - "timeline": Search timeline (filters: event_type, agent_id, task_id,
          start_time, end_time)
        - "conversations": Search conversations (filters: keyword, agent_id, task_id)  # noqa: E501
    state : Any
        Marcus server state
    **filters : Any
        Additional filters specific to query_type. Common pagination filters:
        - limit (int): Maximum results to return (default: 10000, max: 10000)
        - offset (int): Number of results to skip (default: 0)

    Returns
    -------
    dict[str, Any]
        Query results with success status and data. For paginated queries,
        includes pagination metadata (limit, offset, returned_count).

    Examples
    --------
    Get project summary:
    >>> await query_project_history("proj123", "summary", state)

    Find completed tasks:
    >>> await query_project_history("proj123", "tasks", state, status="completed")

    Find decisions by agent with pagination:
    >>> await query_project_history("proj123", "decisions", state, agent_id="agent1", limit=100, offset=0)  # noqa: E501

    Search conversations for keyword:
    >>> await query_project_history("proj123", "conversations", state, keyword="API")  # noqa: E501
    """
    try:
        # Extract pagination parameters from filters
        limit = filters.get("limit", 10000)
        offset = filters.get("offset", 0)

        # Validate pagination parameters
        if not isinstance(limit, int) or limit < 1:
            return {
                "success": False,
                "error": f"Invalid limit: {limit}. Must be positive integer.",
            }
        if not isinstance(offset, int) or offset < 0:
            return {
                "success": False,
                "error": f"Invalid offset: {offset}. Must be non-negative integer.",
            }

        # Cap limit at 10000 to prevent memory issues
        limit = min(limit, 10000)

        # Initialize aggregator and query API
        aggregator = ProjectHistoryAggregator()
        query_api = ProjectHistoryQuery(aggregator)

        # Route to appropriate query method
        if query_type == "summary":
            summary = await query_api.get_project_summary(project_id)
            return {"success": True, "data": summary}

        elif query_type == "tasks":
            # Check which filter is provided
            if "status" in filters:
                tasks = await query_api.find_tasks_by_status(
                    project_id, filters["status"]
                )
            elif "agent_id" in filters:
                tasks = await query_api.find_tasks_by_agent(
                    project_id, filters["agent_id"]
                )
            elif "start_time" in filters:
                # Parse datetime if needed
                from datetime import datetime, timezone

                start_time_raw = filters.get("start_time")
                if isinstance(start_time_raw, str):
                    start_time = datetime.fromisoformat(start_time_raw)
                elif isinstance(start_time_raw, datetime):
                    start_time = start_time_raw
                else:
                    return {
                        "success": False,
                        "error": "start_time required for timerange query",
                    }

                end_time_raw = filters.get("end_time")
                if isinstance(end_time_raw, str):
                    end_time = datetime.fromisoformat(end_time_raw)
                elif isinstance(end_time_raw, datetime):
                    end_time = end_time_raw
                else:
                    end_time = datetime.now(timezone.utc)

                tasks = await query_api.find_tasks_in_timerange(
                    project_id, start_time, end_time
                )
            else:
                # No filter - get all tasks
                history = await query_api.get_project_history(project_id)
                tasks = history.tasks

            # Convert to dict for serialization
            return {
                "success": True,
                "data": [t.to_dict() for t in tasks],
                "count": len(tasks),
            }

        elif query_type == "blocked_tasks":
            tasks = await query_api.find_blocked_tasks(project_id)
            return {
                "success": True,
                "data": [t.to_dict() for t in tasks],
                "count": len(tasks),
            }

        elif query_type == "task_dependencies":
            task_id = filters.get("task_id")
            if not task_id:
                return {
                    "success": False,
                    "error": "task_id required for task_dependencies query",
                }

            chain = await query_api.get_task_dependency_chain(project_id, task_id)
            return {
                "success": True,
                "data": [t.to_dict() for t in chain],
                "count": len(chain),
            }

        elif query_type == "decisions":
            # Opinions and the project primer are decision rows too
            # (``kind="assessment"`` and ``kind="objective"``), so a reader
            # that wants the crew's decisions alone asks for
            # ``kind="decision"`` (#880). Rows written before ``kind``
            # existed are decisions, the field's default. No ``kind``
            # keeps the mixed listing.
            wanted_kind = filters.get("kind")

            def _of_wanted_kind(rows: list[Any]) -> list[Any]:
                if wanted_kind is None:
                    return rows
                return [
                    r for r in rows if getattr(r, "kind", "decision") == wanted_kind
                ]

            # Check which filter is provided
            if "task_id" in filters:
                decisions = _of_wanted_kind(
                    await query_api.find_decisions_by_task(
                        project_id, filters["task_id"]
                    )
                )
            elif "agent_id" in filters:
                decisions = _of_wanted_kind(
                    await query_api.find_decisions_by_agent(
                        project_id, filters["agent_id"]
                    )
                )
            elif "affecting_task_id" in filters:
                decisions = _of_wanted_kind(
                    await query_api.find_decisions_affecting_task(
                        project_id, filters["affecting_task_id"]
                    )
                )
            elif wanted_kind is not None:
                # The store paginates, so filtering the page it returns is
                # not filtering: on a board busy with opinions the newest
                # rows are assessments and a ten-row page of them filters
                # down to nothing while decisions sit just behind it
                # (Codex on PR #881). The kind predicate therefore runs
                # over the whole history, within the 10000-row cap the
                # store already enforces, and the page is cut from the
                # matching rows, so an offset counts decisions rather than
                # the opinions between them.
                history = await query_api.get_project_history(
                    project_id,
                    decision_limit=_DECISION_SCAN_CAP,
                    decision_offset=0,
                )
                matching = _of_wanted_kind(history.decisions)
                decisions = matching[offset : offset + limit]
            else:
                # No filter - get all decisions with pagination
                history = await query_api.get_project_history(
                    project_id,
                    decision_limit=limit,
                    decision_offset=offset,
                )
                decisions = history.decisions

            return {
                "success": True,
                "data": [d.to_dict() for d in decisions],
                "count": len(decisions),
                "pagination": {"limit": limit, "offset": offset},
            }

        elif query_type == "opinions":
            # Deliberation layer B2: one entry per subject, counted by
            # distinct agents, most corroborated first. Pagination pages
            # over subjects, the unit a caller acts on.
            entries = await query_api.summarize_subjects(
                project_id,
                subject=filters.get("subject"),
                kind=filters.get("kind", "assessment"),
                since=filters.get("since"),
                window_minutes=filters.get("window_minutes"),
            )
            page = entries[offset : offset + limit]
            return {
                "success": True,
                "data": page,
                "count": len(page),
                "total_subjects": len(entries),
                "pagination": {"limit": limit, "offset": offset},
            }

        elif query_type == "artifacts":
            # Check which filter is provided
            if "task_id" in filters:
                artifacts = await query_api.find_artifacts_by_task(
                    project_id, filters["task_id"]
                )
            elif "artifact_type" in filters:
                artifacts = await query_api.find_artifacts_by_type(
                    project_id, filters["artifact_type"]
                )
            elif "agent_id" in filters:
                artifacts = await query_api.find_artifacts_by_agent(
                    project_id, filters["agent_id"]
                )
            else:
                # No filter - get all artifacts with pagination
                history = await query_api.get_project_history(
                    project_id,
                    artifact_limit=limit,
                    artifact_offset=offset,
                )
                artifacts = history.artifacts

            return {
                "success": True,
                "data": [a.to_dict() for a in artifacts],
                "count": len(artifacts),
                "pagination": {"limit": limit, "offset": offset},
            }

        elif query_type == "agent_history":
            agent_id = filters.get("agent_id")
            if not agent_id:
                return {
                    "success": False,
                    "error": "agent_id required for agent_history query",
                }

            agent = await query_api.get_agent_history(project_id, agent_id)
            if not agent:
                return {
                    "success": False,
                    "error": f"Agent {agent_id} not found in project {project_id}",
                }

            return {"success": True, "data": agent.to_dict()}

        elif query_type == "agent_metrics":
            agent_id = filters.get("agent_id")
            if not agent_id:
                return {
                    "success": False,
                    "error": "agent_id required for agent_metrics query",
                }

            metrics = await query_api.get_agent_performance_metrics(
                project_id, agent_id
            )
            return {"success": True, "data": metrics}

        elif query_type == "timeline":
            # Parse optional filters
            from datetime import datetime

            event_type_str = filters.get("event_type")
            event_type = event_type_str if isinstance(event_type_str, str) else None

            agent_id_str = filters.get("agent_id")
            agent_id = agent_id_str if isinstance(agent_id_str, str) else None

            task_id_str = filters.get("task_id")
            task_id = task_id_str if isinstance(task_id_str, str) else None

            timeline_start_raw = filters.get("start_time")
            timeline_start: Optional[datetime] = None
            if isinstance(timeline_start_raw, str):
                timeline_start = datetime.fromisoformat(timeline_start_raw)

            timeline_end_raw = filters.get("end_time")
            timeline_end: Optional[datetime] = None
            if isinstance(timeline_end_raw, str):
                timeline_end = datetime.fromisoformat(timeline_end_raw)

            events = await query_api.search_timeline(
                project_id,
                event_type=event_type,
                agent_id=agent_id,
                task_id=task_id,
                start_time=timeline_start,
                end_time=timeline_end,
            )

            # Convert events to dict manually
            event_dicts = []
            for event in events:
                event_dicts.append(
                    {
                        "timestamp": event.timestamp.isoformat(),
                        "event_type": event.event_type,
                        "agent_id": event.agent_id,
                        "task_id": event.task_id,
                        "description": event.description,
                        "details": event.details,
                    }
                )

            return {
                "success": True,
                "data": event_dicts,
                "count": len(event_dicts),
            }

        elif query_type == "conversations":
            keyword_raw = filters.get("keyword")
            keyword = keyword_raw if isinstance(keyword_raw, str) else None

            agent_id_raw = filters.get("agent_id")
            agent_id = agent_id_raw if isinstance(agent_id_raw, str) else None

            task_id_raw = filters.get("task_id")
            task_id = task_id_raw if isinstance(task_id_raw, str) else None

            messages = await query_api.search_conversations(
                project_id,
                keyword=keyword,
                agent_id=agent_id,
                task_id=task_id,
            )

            # Convert messages to dict
            message_dicts = []
            for msg in messages:
                message_dicts.append(
                    {
                        "timestamp": msg.timestamp.isoformat(),
                        "direction": msg.direction,
                        "agent_id": msg.agent_id,
                        "content": msg.content,
                        "metadata": msg.metadata,
                    }
                )

            return {
                "success": True,
                "data": message_dicts,
                "count": len(message_dicts),
            }

        else:
            return {
                "success": False,
                "error": f"Unknown query_type: {query_type}",
                "supported_types": [
                    "summary",
                    "tasks",
                    "blocked_tasks",
                    "task_dependencies",
                    "decisions",
                    "artifacts",
                    "agent_history",
                    "agent_metrics",
                    "timeline",
                    "conversations",
                ],
            }

    except Exception as e:
        logger.error(f"Error querying project history: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "query_type": query_type,
            "project_id": project_id,
        }


async def list_project_history_files(state: Any) -> dict[str, Any]:
    """
    List all projects with available history data from SQLite and conversation logs.

    Queries both SQLite database (decisions/artifacts) and conversation logs
    to find all projects with execution history.

    Parameters
    ----------
    state : Any
        Marcus server state

    Returns
    -------
    dict[str, Any]
        Dict with success status and list of projects with history data:
        - project_id: Project identifier (from conversation logs)
        - project_name: Human-readable name (defaults to project_id)
        - has_decisions: Whether project has decisions in SQLite
        - has_artifacts: Whether project has artifacts in SQLite
        - decision_count: Number of decisions
        - artifact_count: Number of artifacts
        - last_updated: ISO timestamp of last activity

    Examples
    --------
    >>> await list_project_history_files(state)
    {
        "success": True,
        "projects": [
            {
                "project_id": "proj123",
                "project_name": "proj123",
                "has_decisions": True,
                "has_artifacts": True,
                "decision_count": 15,
                "artifact_count": 8,
                "last_updated": "2025-11-08T12:00:00Z"
            }
        ],
        "count": 1
    }
    """
    try:
        from src.core.project_history import ProjectHistoryPersistence

        persistence = ProjectHistoryPersistence()

        # Get all unique project IDs from conversation logs
        project_ids = await persistence._get_all_project_ids_from_conversations()

        if not project_ids:
            logger.debug("No projects found in conversation logs")
            return {"success": True, "projects": [], "count": 0}

        projects = []

        # For each project, check SQLite for decisions and artifacts
        for project_id in project_ids:
            try:
                # Get task IDs for this project
                task_ids = await persistence._get_task_ids_from_conversations(
                    project_id
                )

                if not task_ids:
                    continue

                # Query decisions and artifacts from SQLite
                decisions = await persistence.load_decisions(
                    project_id, limit=10000, offset=0
                )
                artifacts = await persistence.load_artifacts(
                    project_id, limit=10000, offset=0
                )

                # Find latest timestamp
                last_updated = None
                all_timestamps = []

                for decision in decisions:
                    if decision.timestamp:
                        all_timestamps.append(decision.timestamp)

                for artifact in artifacts:
                    if artifact.timestamp:
                        all_timestamps.append(artifact.timestamp)

                if all_timestamps:
                    latest = max(all_timestamps)
                    last_updated = latest.isoformat()

                projects.append(
                    {
                        "project_id": project_id,
                        "project_name": project_id,  # Could enhance with actual name
                        "has_decisions": len(decisions) > 0,
                        "has_artifacts": len(artifacts) > 0,
                        "decision_count": len(decisions),
                        "artifact_count": len(artifacts),
                        "last_updated": last_updated,
                    }
                )

            except Exception as e:
                logger.debug(f"Error processing project {project_id}: {e}")
                continue

        return {"success": True, "projects": projects, "count": len(projects)}

    except Exception as e:
        logger.error(f"Error listing project history: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
