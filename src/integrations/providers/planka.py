"""
Planka implementation using KanbanClient.

Direct integration without the mcp_function_caller abstraction.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from src.core.models import Task, TaskStatus
from src.integrations.kanban_client_with_create import KanbanClientWithCreate
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider

logger = logging.getLogger(__name__)


class Planka(KanbanInterface):
    """Planka kanban board implementation using direct MCP client."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Planka connection.

        Args
        ----
            config: Dictionary containing optional configuration.
        """
        super().__init__(config)
        self.provider = KanbanProvider.PLANKA

        # Set environment variables from config before creating client
        if config:
            if "base_url" in config and config["base_url"]:
                os.environ["PLANKA_BASE_URL"] = config["base_url"]
            if "email" in config and config["email"]:
                os.environ["PLANKA_AGENT_EMAIL"] = config["email"]
            if "password" in config and config["password"]:
                os.environ["PLANKA_AGENT_PASSWORD"] = config["password"]
            if "project_id" in config and config["project_id"]:
                os.environ["PLANKA_PROJECT_ID"] = config["project_id"]
            if "board_id" in config and config["board_id"]:
                os.environ["PLANKA_BOARD_ID"] = config["board_id"]

        self.client = KanbanClientWithCreate()
        # Issue #854: carried down from project_context_manager's
        # _build_provider_config when this instance was constructed
        # from a registered ProjectConfig; None for a standalone
        # construction. See KanbanClient._card_to_task for how it is
        # used.
        self.client.registry_project_id = config.get("registry_project_id")
        self.connected = False
        # Don't print to stdout - it corrupts MCP protocol
        # Use logging instead if needed
        logger.info(
            f"[Planka] Initialized with board_id={self.client.board_id}, "
            f"project_id={self.client.project_id}"
        )

    @property
    def board_id(self) -> Optional[str]:
        """Get board ID from the client."""
        return self.client.board_id if self.client else None

    @property
    def project_id(self) -> Optional[str]:
        """Get project ID from the client."""
        return self.client.project_id if self.client else None

    def _load_workspace_state(self) -> Optional[dict[str, str]]:
        """Load project and board IDs from workspace state file.

        This method delegates to the underlying KanbanClient to maintain
        compatibility with validation and other systems that expect this
        method to be available on the kanban_client interface.

        Returns
        -------
        Optional[dict[str, str]]
            Dictionary with project_id, board_id, and project_root if
            available, or None if no workspace state file exists.
        """
        if not self.client:
            return None
        return self.client._load_workspace_state()

    async def connect(self) -> bool:
        """Connect to Planka via MCP."""
        try:
            # KanbanClient loads config automatically
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Planka: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Planka."""
        self.connected = False

    async def get_available_tasks(self) -> List[Task]:
        """Get unassigned tasks from backlog."""
        try:
            tasks = await self.client.get_available_tasks()
            return tasks
        except Exception as e:
            logger.error(f"Error getting tasks: {e}")
            return []

    async def get_all_tasks(self) -> List[Task]:
        """Get all tasks from the board regardless of status or assignment."""
        try:
            tasks = await self.client.get_all_tasks()
            return tasks
        except Exception as e:
            logger.error(f"Error getting all tasks: {e}")
            return []

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Get specific task by ID."""
        try:
            # Use get_all_tasks to find tasks in any status
            # (get_available_tasks only returns TODO/unassigned)
            tasks = await self.client.get_all_tasks()
            for task in tasks:
                if task.id == task_id:
                    return task
            return None
        except Exception as e:
            logger.error(f"Error getting task {task_id}: {e}")
            return None

    async def create_task(self, task_data: Dict[str, Any]) -> Task:
        """Create new task in Planka."""
        if not self.connected:
            await self.connect()

        try:
            # Use the extended client's create_task method
            return await self.client.create_task(task_data)
        except Exception as e:
            logger.error(f"Error creating task: {e}")
            raise

    async def update_task(
        self, task_id: str, updates: Dict[str, Any]
    ) -> Optional[Task]:
        """Update task status or properties."""
        try:
            logger.info(
                f"[Planka] update_task called with task_id={task_id}, updates={updates}"
            )

            # Handle assignment if provided
            if "assigned_to" in updates:
                logger.info(
                    f"[Planka] Assigning task {task_id} to {updates['assigned_to']}"
                )
                await self.client.assign_task(task_id, updates["assigned_to"])

            # Map status updates to column movements
            if "status" in updates:
                status = updates["status"]
                logger.info(
                    f"[Planka] Status update requested: {status} (type: {type(status)})"
                )

                # Map TaskStatus to column names. DECLINED (ADR-0013 K5,
                # Codex P2 on PR #834) moves to a "declined" list the same
                # best-effort way BLOCKED does: if the board has no such
                # list, move_task_to_column logs and returns False rather
                # than raising, matching every other unmapped column.
                status_to_column = {
                    TaskStatus.TODO: "backlog",
                    TaskStatus.IN_PROGRESS: "in progress",
                    TaskStatus.DONE: "done",
                    TaskStatus.BLOCKED: "blocked",
                    TaskStatus.DECLINED: "declined",
                }

                # Move to appropriate column if status changed
                if status in status_to_column:
                    column = status_to_column[status]
                    logger.info(f"[Planka] Moving task {task_id} to column: {column}")
                    await self.move_task_to_column(task_id, column)
                elif status == TaskStatus.DONE:
                    # Handle COMPLETED as alias for DONE
                    logger.info(f"[Planka] Completing task {task_id}")
                    await self.client.complete_task(task_id)

            # Get and return the updated task
            task = await self.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError(f"Task {task_id} not found after update")
            return task
        except Exception as e:
            logger.error(f"Error updating task {task_id}: {e}")
            # Return the current task state on error
            return await self.get_task_by_id(task_id)

    async def add_comment(self, task_id: str, comment: str) -> bool:
        """Add comment to task."""
        try:
            await self.client.add_comment(task_id, comment)
            return True
        except Exception as e:
            logger.error(f"Error adding comment to task {task_id}: {e}")
            return False

    async def get_agent_tasks(self, agent_id: str) -> List[Task]:
        """Get all tasks assigned to a specific agent."""
        try:
            # Get all tasks and filter by assignment
            await self.client.get_board_summary()
            # This would need to be implemented based on board structure
            return []
        except Exception as e:
            logger.error(f"Error getting agent tasks: {e}")
            return []

    async def get_board_summary(self) -> Dict[str, Any]:
        """Get overall board statistics and summary."""
        try:
            summary = await self.client.get_board_summary()
            return summary
        except Exception as e:
            logger.error(f"Error getting board summary: {e}")
            return {}

    async def assign_task(self, task_id: str, assignee_id: str) -> bool:
        """Assign a task to a worker."""
        try:
            # KanbanClient.assign_task already moves the task to "In Progress"
            await self.client.assign_task(task_id, assignee_id)
            return True
        except Exception as e:
            logger.error(f"Error assigning task {task_id}: {e}")
            return False

    async def move_task_to_column(self, task_id: str, column_name: str) -> bool:
        """Move task to a specific column/status."""
        try:
            # Use KanbanClient's update_task_status for column movements
            # Map column names to status names that KanbanClient understands
            column_to_status = {
                "backlog": "todo",
                "todo": "todo",
                "in progress": "in_progress",
                "blocked": "blocked",
                "done": "done",
                "completed": "done",
                "declined": "declined",
            }

            status = column_to_status.get(column_name.lower(), column_name.lower())
            await self.client.update_task_status(task_id, status)
            return True
        except Exception as e:
            logger.error(f"Error moving task {task_id} to {column_name}: {e}")
            return False

    async def get_project_metrics(self) -> Dict[str, Any]:
        """Get project metrics and statistics."""
        try:
            summary = await self.client.get_board_summary()
            # Extract metrics from summary
            return {
                "total_tasks": summary.get("totalCards", 0),
                "backlog_tasks": summary.get("backlogCount", 0),
                "in_progress_tasks": summary.get("inProgressCount", 0),
                "completed_tasks": summary.get("doneCount", 0),
                "blocked_tasks": 0,  # Not tracked in simple client
            }
        except Exception as e:
            logger.error(f"Error getting project metrics: {e}")
            return {
                "total_tasks": 0,
                "backlog_tasks": 0,
                "in_progress_tasks": 0,
                "completed_tasks": 0,
                "blocked_tasks": 0,
            }

    async def report_blocker(
        self, task_id: str, blocker_description: str, severity: str = "medium"
    ) -> bool:
        """Report a blocker on a task."""
        try:
            # Add blocker as a comment
            comment = f"🚫 BLOCKER ({severity.upper()}): {blocker_description}"
            await self.client.add_comment(task_id, comment)
            return True
        except Exception as e:
            logger.error(f"Error reporting blocker on task {task_id}: {e}")
            return False

    async def update_task_progress(
        self, task_id: str, progress_data: Dict[str, Any]
    ) -> bool:
        """Update task progress."""
        try:
            # Add progress as a comment
            progress = progress_data.get("progress", 0)
            message = progress_data.get("message", "")
            comment = f"📊 Progress: {progress}% - {message}"
            await self.client.add_comment(task_id, comment)

            # Handle status changes
            status = progress_data.get("status")
            if status and progress == 100:
                await self.client.complete_task(task_id)

            return True
        except Exception as e:
            logger.error(f"Error updating task progress for {task_id}: {e}")
            return False

    async def upload_attachment(
        self,
        task_id: str,
        filename: str,
        content: Any,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload an attachment to a task.

        Note: Attachment functionality not yet implemented for Planka integration.
        This is a placeholder to satisfy the abstract interface.

        Args
        ----
            task_id: The task identifier.
            filename: Name for the attachment.
            content: File content.
            content_type: MIME type.

        Returns
        -------
            Dict with success=False indicating not implemented.
        """
        return {
            "success": False,
            "error": "Attachment upload not implemented for Planka integration",
        }

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """
        Get all attachments for a task.

        Note: Attachment functionality not yet implemented for Planka integration.
        This is a placeholder to satisfy the abstract interface.

        Args
        ----
            task_id: The task identifier.

        Returns
        -------
            Dict with empty attachments list.
        """
        return {
            "success": True,
            "data": [],
            "message": "Attachment functionality not yet implemented",
        }

    async def download_attachment(
        self, attachment_id: str, filename: str, task_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Download an attachment.

        Note: Attachment functionality not yet implemented for Planka integration.
        This is a placeholder to satisfy the abstract interface.

        Args
        ----
            attachment_id: The attachment ID.
            filename: The filename.
            task_id: Optional task ID.

        Returns
        -------
            Dict with success=False indicating not implemented.
        """
        return {
            "success": False,
            "error": "Attachment download not implemented for Planka integration",
        }
