"""
GitHub Projects implementation of KanbanInterface.

Uses GitHub MCP Server to manage tasks.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.models import Priority, Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider

logger = logging.getLogger(__name__)


class GitHubKanban(KanbanInterface):
    """GitHub Projects kanban board implementation using MCP Server."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize GitHub MCP connection.

        Args
        ----
        config : Dict[str, Any]
            Dictionary containing:
                - mcp_function_caller: Function to call MCP tools
                - owner: Repository owner (user or org)
                - repo: Repository name
                - project_number: Project number (for v2 projects)
        """
        super().__init__(config)
        self.provider = KanbanProvider.GITHUB
        self.mcp_caller: Optional[Callable[[str, Dict[str, Any]], Awaitable[Any]]] = (
            config.get("mcp_function_caller")
        )
        self.owner = config.get("owner")
        self.repo = config.get("repo")
        self.project_number = config.get("project_number")

        if not self.mcp_caller:
            raise ValueError(
                "mcp_function_caller is required for GitHub MCP integration"
            )

    async def connect(self) -> bool:
        """Connect to GitHub MCP Server."""
        if not self.mcp_caller:
            return False
        try:
            # Test connection by getting authenticated user
            result = await self.mcp_caller("github.get_me", {})
            return "user" in result
        except Exception as e:
            import sys

            print(f"Failed to connect to GitHub MCP: {e}", file=sys.stderr)
            return False

    async def disconnect(self) -> None:
        """Disconnect from GitHub MCP."""
        # No persistent connection to close for MCP
        pass

    async def get_available_tasks(self) -> List[Task]:
        """Get unassigned tasks from backlog."""
        # Search for unassigned issues in the repository
        query = f"repo:{self.owner}/{self.repo} is:issue is:open no:assignee"

        if not self.mcp_caller:
            return []

        result = await self.mcp_caller(
            "github.search_issues",
            {"query": query, "sort": "created", "order": "desc", "perPage": 100},
        )

        tasks = []
        if result.get("items"):
            for issue in result["items"]:
                tasks.append(self._github_issue_to_task(issue))

        return tasks

    def _github_issue_to_task(self, issue: Dict[str, Any]) -> Task:
        """Convert GitHub issue to Task model."""
        # Extract labels
        labels = []
        if issue.get("labels"):
            if isinstance(issue["labels"], list):
                labels = [
                    label.get("name", "") if isinstance(label, dict) else str(label)
                    for label in issue["labels"]
                ]

        # Determine priority from labels
        priority = Priority.MEDIUM
        for label in labels:
            label_lower = label.lower()
            if "urgent" in label_lower or "critical" in label_lower:
                priority = Priority.URGENT
                break
            elif "high" in label_lower or "priority/high" in label_lower:
                priority = Priority.HIGH
                break
            elif "low" in label_lower or "priority/low" in label_lower:
                priority = Priority.LOW
                break

        # Determine status
        status = TaskStatus.TODO
        state = issue.get("state", "open")
        if state == "closed":
            status = TaskStatus.DONE
        else:
            # Check labels for status
            for label in labels:
                label_lower = label.lower()
                if "in progress" in label_lower or "in-progress" in label_lower:
                    status = TaskStatus.IN_PROGRESS
                    break
                elif "blocked" in label_lower:
                    status = TaskStatus.BLOCKED
                    break
                elif "ready" in label_lower:
                    status = TaskStatus.TODO
                    break

        # Parse dates
        created_at = issue.get("created_at", "")
        updated_at = issue.get("updated_at", "")

        if created_at:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        else:
            created_at = datetime.now(timezone.utc)

        if updated_at:
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        else:
            updated_at = datetime.now(timezone.utc)

        # Extract assignee
        assignee = None
        if issue.get("assignee"):
            assignee = issue["assignee"].get("login")
        elif issue.get("assignees") and len(issue["assignees"]) > 0:
            assignee = issue["assignees"][0].get("login")

        return Task(
            id=str(issue.get("number", issue.get("id", ""))),
            name=issue.get("title", "Untitled"),
            description=issue.get("body", ""),
            status=status,
            priority=priority,
            assigned_to=assignee,
            created_at=created_at,
            updated_at=updated_at,
            due_date=None,  # GitHub issues don't have built-in due dates
            estimated_hours=8.0,  # Default, could parse from body
            actual_hours=0.0,
            dependencies=[],
            labels=labels,
        )

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Get specific task by ID."""
        if not self.mcp_caller:
            return None
        try:
            # task_id should be the issue number
            result = await self.mcp_caller(
                "github.get_issue",
                {"owner": self.owner, "repo": self.repo, "issue_number": int(task_id)},
            )

            if result.get("issue"):
                return self._github_issue_to_task(result["issue"])
            return None
        except Exception:
            return None

    async def create_task(self, task_data: Dict[str, Any]) -> Task:
        """Create new issue in repository."""
        if not self.mcp_caller:
            raise ValueError("MCP caller not available")

        # Map priority to labels
        priority_labels = {
            Priority.URGENT: "priority/urgent",
            Priority.HIGH: "priority/high",
            Priority.MEDIUM: "priority/medium",
            Priority.LOW: "priority/low",
        }

        labels = task_data.get("labels", [])
        priority = task_data.get("priority", Priority.MEDIUM)
        if priority in priority_labels:
            labels.append(priority_labels[priority])

        result = await self.mcp_caller(
            "github.create_issue",
            {
                "owner": self.owner,
                "repo": self.repo,
                "title": task_data.get("name", "Untitled Task"),
                "body": task_data.get("description", ""),
                "labels": labels,
            },
        )

        if not result.get("issue"):
            raise Exception(
                f"Failed to create issue: {result.get('error', 'Unknown error')}"
            )

        return self._github_issue_to_task(result["issue"])

    async def update_task(
        self, task_id: str, updates: Dict[str, Any]
    ) -> Optional[Task]:
        """Update existing issue."""
        if not self.mcp_caller:
            raise ValueError("MCP caller not available")

        update_data = {
            "owner": self.owner,
            "repo": self.repo,
            "issue_number": int(task_id),
        }

        if "name" in updates:
            update_data["title"] = updates["name"]
        if "description" in updates:
            update_data["body"] = updates["description"]
        if "status" in updates:
            update_data["state"] = (
                "closed" if updates["status"] == TaskStatus.DONE else "open"
            )

        result = await self.mcp_caller("github.update_issue", update_data)

        if not result.get("issue"):
            raise Exception(
                f"Failed to update issue: {result.get('error', 'Unknown error')}"
            )

        return self._github_issue_to_task(result["issue"])

    async def assign_task(self, task_id: str, assignee_id: str) -> bool:
        """Assign issue to user."""
        if not self.mcp_caller:
            return False

        result = await self.mcp_caller(
            "github.update_issue",
            {
                "owner": self.owner,
                "repo": self.repo,
                "issue_number": int(task_id),
                "assignees": [assignee_id],
            },
        )

        return bool(result.get("success", False))

    async def move_task_to_column(self, task_id: str, column_name: str) -> bool:
        """Move task to specific status column."""
        if not self.mcp_caller:
            return False

        # Map column names to GitHub states/labels
        column_lower = column_name.lower()

        # Update issue state if moving to done
        if column_lower in ["done", "completed", "closed"]:
            await self.mcp_caller(
                "github.update_issue",
                {
                    "owner": self.owner,
                    "repo": self.repo,
                    "issue_number": int(task_id),
                    "state": "closed",
                },
            )
            return True
        elif column_lower in ["in progress", "in-progress"]:
            return await self._replace_status_label(task_id, "in-progress")
        elif column_lower == "blocked":
            return await self._replace_status_label(task_id, "blocked")

        # Codex P1 (round 4 on PR #834): a column this provider has no
        # mapping for (e.g. "declined" -- tracked separately in #835)
        # must not silently report success. The caller trusts this
        # return value to decide whether the status actually persisted.
        logger.warning(
            "[GithubKanban] move_task_to_column: no mapping for column "
            "'%s' on issue %s; status was not persisted",
            column_name,
            task_id,
        )
        return False

    # Labels that encode the board column. Anything else on the issue
    # (priority/*, domain/*, user-managed labels) is not ours to touch.
    _STATUS_LABEL_PREFIXES = ("blocked", "in-progress", "in progress", "ready")

    def _is_status_label(self, label: str) -> bool:
        """Return True when ``label`` encodes a board column.

        Parameters
        ----------
        label : str
            A GitHub label name.

        Returns
        -------
        bool
            True for ``blocked``, ``blocked/<severity>``, ``in-progress``,
            ``in progress`` and ``ready``; False for everything else.
        """
        lowered = label.lower()
        return any(lowered.startswith(prefix) for prefix in self._STATUS_LABEL_PREFIXES)

    async def _replace_status_label(self, task_id: str, status_label: str) -> bool:
        """Set the issue's status label without disturbing its other labels.

        ``github.update_issue`` replaces the whole label set, so the issue
        is read first and every non-status label is carried over (Codex P2
        on PR #770: the old ``labels: ["blocked"]`` write deleted priority,
        domain and user-managed labels on every lane death). If the issue
        cannot be read, the status label is written alone rather than not
        at all, because the column change is the thing callers rely on.

        Parameters
        ----------
        task_id : str
            Issue number as a string.
        status_label : str
            The status label to end up with (``blocked`` or ``in-progress``).

        Returns
        -------
        bool
            True when the update call reported success.
        """
        if not self.mcp_caller:
            return False
        kept: List[str] = []
        try:
            current = await self.mcp_caller(
                "github.get_issue",
                {"owner": self.owner, "repo": self.repo, "issue_number": int(task_id)},
            )
            raw_labels = (current.get("issue") or {}).get("labels") or []
            for label in raw_labels:
                name = label.get("name", "") if isinstance(label, dict) else str(label)
                if name and not self._is_status_label(name):
                    kept.append(name)
        except Exception as read_error:  # noqa: BLE001 - fall back to status-only
            logger.warning(
                "Could not read labels for issue %s before setting %s: %s",
                task_id,
                status_label,
                read_error,
            )
            kept = []
        result = await self.mcp_caller(
            "github.update_issue",
            {
                "owner": self.owner,
                "repo": self.repo,
                "issue_number": int(task_id),
                "labels": kept + [status_label],
            },
        )
        return bool(result.get("success", True))

    async def add_comment(self, task_id: str, comment: str) -> bool:
        """Add comment to issue."""
        if not self.mcp_caller:
            return False

        result = await self.mcp_caller(
            "github.add_issue_comment",
            {
                "owner": self.owner,
                "repo": self.repo,
                "issue_number": int(task_id),
                "body": comment,
            },
        )

        return bool(result.get("success", False))

    async def get_project_metrics(self) -> Dict[str, Any]:
        """Get project metrics."""
        if not self.mcp_caller:
            return {
                "total_tasks": 0,
                "backlog_tasks": 0,
                "in_progress_tasks": 0,
                "completed_tasks": 0,
                "blocked_tasks": 0,
            }

        metrics = {
            "total_tasks": 0,
            "backlog_tasks": 0,
            "in_progress_tasks": 0,
            "completed_tasks": 0,
            "blocked_tasks": 0,
        }

        # Get open issues
        open_result = await self.mcp_caller(
            "github.list_issues",
            {"owner": self.owner, "repo": self.repo, "state": "open", "perPage": 100},
        )

        # Get closed issues
        closed_result = await self.mcp_caller(
            "github.list_issues",
            {"owner": self.owner, "repo": self.repo, "state": "closed", "perPage": 100},
        )

        # Count open issues by labels
        if open_result.get("issues"):
            for issue in open_result["issues"]:
                labels = [
                    label.get("name", "").lower() for label in issue.get("labels", [])
                ]

                if "blocked" in labels:
                    metrics["blocked_tasks"] += 1
                elif "in-progress" in labels or "in progress" in labels:
                    metrics["in_progress_tasks"] += 1
                else:
                    metrics["backlog_tasks"] += 1

        # Count closed issues
        if closed_result.get("issues"):
            metrics["completed_tasks"] = len(closed_result["issues"])

        metrics["total_tasks"] = (
            metrics["backlog_tasks"]
            + metrics["in_progress_tasks"]
            + metrics["completed_tasks"]
            + metrics["blocked_tasks"]
        )

        return metrics

    async def report_blocker(
        self, task_id: str, blocker_description: str, severity: str = "medium"
    ) -> bool:
        """Report blocker on task."""
        if not self.mcp_caller:
            return False

        # Add blocker comment
        comment = f"🚫 **BLOCKER** ({severity.upper()}): {blocker_description}"
        await self.add_comment(task_id, comment)

        # Add blocker label using MCP
        try:
            await self.mcp_caller(
                "github.add_issue_labels",
                {
                    "owner": self.owner,
                    "repo": self.repo,
                    "issue_number": int(task_id),
                    "labels": [f"blocked/{severity}"],
                },
            )
        except Exception:
            # Fallback - just add blocked label
            await self.mcp_caller(
                "github.add_issue_labels",
                {
                    "owner": self.owner,
                    "repo": self.repo,
                    "issue_number": int(task_id),
                    "labels": ["blocked"],
                },
            )

        # Try to move to blocked column
        await self.move_task_to_column(task_id, "Blocked")

        return True

    async def update_task_progress(
        self, task_id: str, progress_data: Dict[str, Any]
    ) -> bool:
        """Update task progress."""
        if not self.mcp_caller:
            return False

        progress = progress_data.get("progress", 0)
        status = progress_data.get("status", "")
        message = progress_data.get("message", "")

        # Add progress comment
        comment = f"📊 **Progress Update**: {progress}%"
        if message:
            comment += f"\n\n{message}"

        await self.add_comment(task_id, comment)

        # Update column based on progress
        if progress >= 100:
            await self.move_task_to_column(task_id, "Done")
        elif progress > 0 and status == "in_progress":
            await self.move_task_to_column(task_id, "In Progress")

        return True
