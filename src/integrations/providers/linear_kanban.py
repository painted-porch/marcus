"""
Linear implementation of KanbanInterface.

Uses Linear MCP Server to manage tasks and projects
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Union

from src.core.models import Priority, Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider

logger = logging.getLogger(__name__)


class LinearKanban(KanbanInterface):
    """Linear kanban board implementation using MCP Server."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Linear MCP connection.

        Args
        ----
        config : Dict[str, Any]
            Dictionary containing:
                - mcp_function_caller: Function to call MCP tools
                - team_id: Linear team ID
                - project_id: Optional Linear project ID
        """
        super().__init__(config)
        self.provider = KanbanProvider.LINEAR
        mcp_func = config.get("mcp_function_caller")
        if not callable(mcp_func):
            raise ValueError(
                "mcp_function_caller is required for Linear MCP integration"
            )
        self.mcp_caller: Callable[..., Any] = mcp_func
        self.team_id: Optional[str] = config.get("team_id")
        self.project_id: Optional[str] = config.get("project_id")

    async def connect(self) -> bool:
        """Connect to Linear MCP Server."""
        try:
            # Test connection by getting teams
            result = await self.mcp_caller("linear.get_teams", {})
            return bool(result.get("success", False))
        except Exception as e:
            import sys

            print(f"Failed to connect to Linear MCP: {e}", file=sys.stderr)
            return False

    async def disconnect(self) -> None:
        """Disconnect from Linear MCP."""
        # No persistent connection to close for MCP
        pass

    async def get_available_tasks(self) -> List[Task]:
        """Get unassigned tasks from backlog."""
        # Build search filter
        filter_obj = {
            "assignee": {"null": True},
            "state": {"type": {"in": ["backlog", "unstarted"]}},
        }

        if self.team_id:
            filter_obj["team"] = {"id": {"eq": [self.team_id]}}
        if self.project_id:
            filter_obj["project"] = {"id": {"eq": [self.project_id]}}

        result = await self.mcp_caller(
            "linear.search_issues",
            {
                "query": "",  # Empty query to get all matching filter
                "filter": filter_obj,
                "includeRelationships": True,
            },
        )

        tasks = []
        if result.get("issues"):
            for issue in result["issues"]:
                tasks.append(self._linear_issue_to_task(issue))

        return tasks

    async def get_all_tasks(self) -> List[Task]:
        """Get all tasks from the board regardless of status or assignment."""
        # Build search filter for all tasks
        filter_obj = {}

        if self.team_id:
            filter_obj["team"] = {"id": {"eq": [self.team_id]}}
        if self.project_id:
            filter_obj["project"] = {"id": {"eq": [self.project_id]}}

        result = await self.mcp_caller(
            "linear.search_issues",
            {
                "query": "",  # Empty query to get all matching filter
                "filter": filter_obj,
                "includeRelationships": True,
            },
        )

        tasks = []
        if result.get("issues"):
            for issue in result["issues"]:
                tasks.append(self._linear_issue_to_task(issue))

        return tasks

    def _linear_issue_to_task(self, issue: Dict[str, Any]) -> Task:
        """Convert Linear issue to Task model."""
        # Map Linear priority (0-4) to our Priority enum
        linear_priority = issue.get("priority", 3)
        priority_map = {
            1: Priority.URGENT,  # Urgent
            2: Priority.HIGH,  # High
            3: Priority.MEDIUM,  # Medium
            4: Priority.LOW,  # Low
            0: Priority.LOW,  # No priority
        }

        # Extract labels
        labels = issue.get("labels", [])
        if isinstance(labels, dict) and "nodes" in labels:
            labels = [label.get("name", "") for label in labels.get("nodes", [])]
        elif isinstance(labels, list):
            labels = [
                label if isinstance(label, str) else label.get("name", "")
                for label in labels
            ]

        # Map Linear state to TaskStatus. Linear's own "type" is one of
        # five fixed categories (backlog/unstarted/started/completed/
        # canceled) and carries no information about which named state
        # within that category an issue is in, so it alone cannot
        # distinguish "Blocked" or "Declined" from an ordinary state in
        # the same category. The state's own name is checked first for
        # those two (#835); any other name falls back to the type map,
        # unchanged from before.
        state = issue.get("state", {})
        state_name = (
            (state.get("name") or "") if isinstance(state, dict) else ""
        ).lower()
        status_map = {
            "backlog": TaskStatus.TODO,
            "unstarted": TaskStatus.TODO,
            "started": TaskStatus.IN_PROGRESS,
            "completed": TaskStatus.DONE,
            "canceled": TaskStatus.DONE,
        }
        if "declined" in state_name:
            status = TaskStatus.DECLINED
        elif "blocked" in state_name:
            status = TaskStatus.BLOCKED
        else:
            state_type = (
                state.get("type", "backlog") if isinstance(state, dict) else "backlog"
            )
            status = status_map.get(state_type, TaskStatus.TODO)

        # Parse dates
        created_at = issue.get("createdAt", "")
        updated_at = issue.get("updatedAt", "")

        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        else:
            created_at = datetime.now(timezone.utc)

        if updated_at and isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        else:
            updated_at = datetime.now(timezone.utc)

        return Task(
            id=issue.get("id", ""),
            name=issue.get("title", "Untitled"),
            description=issue.get("description", ""),
            status=status,
            priority=priority_map.get(linear_priority, Priority.MEDIUM),
            labels=labels,
            estimated_hours=issue.get("estimate", 0) or 8,
            assigned_to=(
                issue.get("assignee", {}).get("id") if issue.get("assignee") else None
            ),
            dependencies=[],
            created_at=created_at,
            updated_at=updated_at,
            due_date=None,
        )

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Get specific task by ID."""
        result = await self.mcp_caller(
            "linear.get_issue", {"issueId": task_id, "includeRelationships": True}
        )

        if not result.get("issue"):
            return None

        return self._linear_issue_to_task(result["issue"])

    async def create_task(self, task_data: Dict[str, Any]) -> Task:
        """Create new task in Linear."""
        # Map priority to Linear's scale
        priority = task_data.get("priority", Priority.MEDIUM)
        priority_map = {
            Priority.URGENT: 1,
            Priority.HIGH: 2,
            Priority.MEDIUM: 3,
            Priority.LOW: 4,
        }

        create_data = {
            "teamId": self.team_id,
            "title": task_data.get("name", "Untitled Task"),
            "description": task_data.get("description", ""),
            "priority": priority_map.get(priority, 3),
        }

        # Add optional fields
        if task_data.get("labels"):
            create_data["labelIds"] = task_data["labels"]

        result = await self.mcp_caller("linear.create_issue", create_data)

        if not result.get("issue"):
            raise Exception(
                f"Failed to create task: {result.get('error', 'Unknown error')}"
            )

        return self._linear_issue_to_task(result["issue"])

    async def update_task(
        self, task_id: str, updates: Dict[str, Any]
    ) -> Optional[Task]:
        """Update existing task."""
        update_data = {"issueId": task_id}

        if "name" in updates:
            update_data["title"] = updates["name"]
        if "description" in updates:
            update_data["description"] = updates["description"]
        if "priority" in updates:
            priority_map = {
                Priority.URGENT: "1",
                Priority.HIGH: "2",
                Priority.MEDIUM: "3",
                Priority.LOW: "4",
            }
            update_data["priority"] = priority_map.get(updates["priority"], "3")

        # update_task never touched status at all (#835): DONE and
        # IN_PROGRESS get their own workflow-state move through
        # update_task_progress, but a terminal-failure status
        # (BLOCKED, DECLINED) can only ever reach the board through
        # this call, so it is included here directly rather than as a
        # second round-trip through move_task_to_column.
        status = updates.get("status")
        if status == TaskStatus.BLOCKED:
            update_data["status"] = "Blocked"
        elif status == TaskStatus.DECLINED:
            update_data["status"] = "Declined"

        result = await self.mcp_caller("linear.update_issue", update_data)

        if not result.get("issue"):
            raise Exception(
                f"Failed to update task: {result.get('error', 'Unknown error')}"
            )

        return self._linear_issue_to_task(result["issue"])

    async def assign_task(self, task_id: str, assignee_id: str) -> bool:
        """Assign task to user."""
        result = await self.mcp_caller(
            "linear.update_issue", {"issueId": task_id, "assigneeId": assignee_id}
        )

        return bool(result.get("success", False))

    async def move_task_to_column(self, task_id: str, column_name: str) -> bool:
        """Move task to specific state."""
        # Map column names to Linear state names
        state_map = {
            "backlog": "Backlog",
            "todo": "Todo",
            "ready": "Ready",
            "in progress": "In Progress",
            "done": "Done",
            "completed": "Done",
            "blocked": "Blocked",
            "declined": "Declined",
        }

        status_name = state_map.get(column_name.lower())
        if status_name is None:
            # A column this provider has no mapping for must not
            # silently report success by sending Linear a status name
            # it will not recognize either. The caller trusts this
            # return value to decide whether the status actually
            # persisted.
            logger.warning(
                "[LinearKanban] move_task_to_column: no mapping for "
                "column '%s' on issue %s; status was not persisted",
                column_name,
                task_id,
            )
            return False

        # Update issue with new status
        result = await self.mcp_caller(
            "linear.update_issue", {"issueId": task_id, "status": status_name}
        )

        return bool(result.get("success", False))

    async def add_comment(self, task_id: str, comment: str) -> bool:
        """Add comment to task."""
        result = await self.mcp_caller(
            "linear.create_comment", {"issueId": task_id, "body": comment}
        )

        return bool(result.get("success", False))

    async def get_project_metrics(self) -> Dict[str, Any]:
        """Get project metrics."""
        # Get counts for different states
        metrics = {
            "total_tasks": 0,
            "backlog_tasks": 0,
            "in_progress_tasks": 0,
            "completed_tasks": 0,
            "blocked_tasks": 0,
        }

        # Search for different states
        state_queries = [
            (["backlog", "unstarted"], "backlog_tasks"),
            (["started"], "in_progress_tasks"),
            (["completed", "canceled"], "completed_tasks"),
        ]

        for states, metric_key in state_queries:
            filter_obj: Dict[str, Any] = {"state": {"type": {"in": states}}}
            if self.team_id:
                filter_obj["team"] = {"id": {"eq": [self.team_id]}}
            if self.project_id:
                filter_obj["project"] = {"id": {"eq": [self.project_id]}}

            result = await self.mcp_caller(
                "linear.search_issues",
                {"query": "", "filter": filter_obj, "includeRelationships": False},
            )

            count = len(result.get("issues", []))
            metrics[metric_key] = count
            metrics["total_tasks"] += count

        return metrics

    async def report_blocker(
        self, task_id: str, blocker_description: str, severity: str = "medium"
    ) -> bool:
        """Report blocker on task."""
        # Add blocker comment
        comment = f"🚫 BLOCKER ({severity.upper()}): {blocker_description}"
        await self.add_comment(task_id, comment)

        # Try to move task to blocked state if available
        try:
            await self.move_task_to_column(task_id, "blocked")
        except Exception:  # nosec B110
            # If blocked state doesn't exist, continue without error
            pass

        return True

    async def update_task_progress(
        self, task_id: str, progress_data: Dict[str, Any]
    ) -> bool:
        """Update task progress."""
        progress = progress_data.get("progress", 0)
        status = progress_data.get("status", "")
        message = progress_data.get("message", "")

        # Add progress comment
        comment = f"📊 Progress: {progress}%"
        if message:
            comment += f" - {message}"

        await self.add_comment(task_id, comment)

        # Update state based on progress
        if progress >= 100:
            await self.move_task_to_column(task_id, "Done")
        elif progress > 0 and status == "in_progress":
            await self.move_task_to_column(task_id, "In Progress")

        return True

    async def upload_attachment(
        self,
        task_id: str,
        filename: str,
        content: Union[str, bytes],
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload an attachment to a task.

        Linear MCP doesn't currently support direct file uploads,
        so we'll add the attachment content as a comment with metadata.
        """
        # Create a comment with the attachment info
        attachment_comment = f"📎 Attachment: {filename}"
        if content_type:
            attachment_comment += f" (type: {content_type})"

        # If content is small enough, include it inline
        if isinstance(content, str) and len(content) < 1000:
            attachment_comment += f"\n\n```\n{content}\n```"
        elif isinstance(content, bytes) and len(content) < 1000:
            try:
                decoded = content.decode("utf-8")
                attachment_comment += f"\n\n```\n{decoded}\n```"
            except UnicodeDecodeError:
                attachment_comment += f"\n\nBinary file ({len(content)} bytes)"
        else:
            attachment_comment += (
                f"\n\nLarge file ({len(content)} bytes) - content not shown"
            )

        success = await self.add_comment(task_id, attachment_comment)

        if success:
            return {
                "success": True,
                "data": {
                    "id": f"{task_id}_{filename}",
                    "filename": filename,
                    "url": None,  # Linear MCP doesn't provide direct URLs
                    "size": len(content) if isinstance(content, (str, bytes)) else 0,
                },
            }
        else:
            return {"success": False, "error": "Failed to add attachment comment"}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """
        Get all attachments for a task.

        Since Linear MCP doesn't have direct attachment support,
        this returns an empty list but maintains the interface.
        """
        return {
            "success": True,
            "data": [],  # No direct attachment support in Linear MCP
        }

    async def download_attachment(
        self, attachment_id: str, filename: str, task_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Download an attachment.

        Linear MCP doesn't support direct file attachments,
        so this returns an error message.
        """
        return {
            "success": False,
            "error": "Linear MCP doesn't support direct file attachments",
        }
