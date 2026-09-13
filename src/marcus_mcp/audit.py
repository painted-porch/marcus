"""
Enhanced audit logging for Marcus MCP.

Provides comprehensive logging of all client actions for debugging,
compliance, and usage analytics.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles

from src.core.paths import marcus_data_dir


class AuditLogger:
    """Handles audit logging for Marcus operations."""

    def __init__(self, log_dir: Optional[Path] = None):
        """
        Initialize the audit logger.

        Parameters
        ----------
        log_dir : Optional[Path]
            Directory for audit logs. Defaults to data/audit_logs/
        """
        self.log_dir = log_dir or marcus_data_dir() / "audit_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Create daily log file
        self.log_file = (
            self.log_dir
            / f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        )

    async def log_event(
        self,
        event_type: str,
        client_id: Optional[str],
        client_type: Optional[str],
        tool_name: Optional[str],
        details: Dict[str, Any],
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """
        Log an audit event.

        Parameters
        ----------
        event_type : str
            Type of event (e.g., "tool_call", "registration", "error")
        client_id : Optional[str]
            ID of the client performing the action
        client_type : Optional[str]
            Type of client (observer, developer, agent, admin)
        tool_name : Optional[str]
            Name of the tool being called
        details : Dict[str, Any]
            Additional event details
        success : bool
            Whether the operation succeeded
        error : Optional[str]
            Error message if operation failed
        """
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "client_id": client_id,
            "client_type": client_type,
            "tool_name": tool_name,
            "success": success,
            "details": details,
        }

        if error:
            event["error"] = error

        # Add environment context
        event["context"] = {
            "host": os.environ.get("HOSTNAME", "unknown"),
            "transport": os.environ.get("MARCUS_TRANSPORT", "stdio"),
            "version": os.environ.get("MARCUS_VERSION", "unknown"),
        }

        # Write to log file
        async with aiofiles.open(self.log_file, mode="a") as f:
            await f.write(json.dumps(event) + "\n")

    async def log_registration(
        self,
        client_id: str,
        client_type: str,
        role: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a client registration event."""
        await self.log_event(
            event_type="client_registration",
            client_id=client_id,
            client_type=client_type,
            tool_name=None,
            details={
                "role": role,
                "metadata": metadata or {},
            },
        )

    async def log_tool_call(
        self,
        client_id: Optional[str],
        client_type: Optional[str],
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
        duration_ms: float,
        success: bool = True,
        error: Optional[str] = None,
        task_count: Optional[int] = None,
    ) -> None:
        """Log a tool call event."""
        details: Dict[str, Any] = {
            "arguments": arguments,
            "result": result if success else None,
            "duration_ms": duration_ms,
        }
        if task_count is not None:
            details["task_count"] = task_count
        await self.log_event(
            event_type="tool_call",
            client_id=client_id,
            client_type=client_type,
            tool_name=tool_name,
            details=details,
            success=success,
            error=error,
        )

    async def log_access_denied(
        self,
        client_id: Optional[str],
        client_type: Optional[str],
        tool_name: str,
        reason: str,
        duration_ms: float,
        task_count: Optional[int] = None,
    ) -> None:
        """Log an access denied event."""
        details: Dict[str, Any] = {"reason": reason, "duration_ms": duration_ms}
        if task_count is not None:
            details["task_count"] = task_count
        await self.log_event(
            event_type="access_denied",
            client_id=client_id,
            client_type=client_type,
            tool_name=tool_name,
            details=details,
            success=False,
        )

    async def log_session(
        self,
        event_type: str,  # "session_start", "session_end"
        session_id: str,
        transport: str,
        client_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log session lifecycle events."""
        await self.log_event(
            event_type=event_type,
            client_id=client_id,
            client_type=None,
            tool_name=None,
            details={
                "session_id": session_id,
                "transport": transport,
                "metadata": metadata or {},
            },
        )

    async def get_usage_stats(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Generate usage statistics from audit logs.

        Parameters
        ----------
        start_date : Optional[datetime]
            Start of analysis period
        end_date : Optional[datetime]
            End of analysis period

        Returns
        -------
        Dict[str, Any]
            Usage statistics
        """
        stats: Dict[str, Any] = {
            "total_events": 0,
            "by_client_type": {},
            "by_tool": {},
            "by_event_type": {},
            "errors": 0,
            "unique_clients": set(),
        }

        # Read all relevant log files
        for log_file in self.log_dir.glob("audit_*.jsonl"):
            async with aiofiles.open(log_file, mode="r") as f:
                async for line in f:
                    try:
                        event: Dict[str, Any] = json.loads(line.strip())

                        # Check date range
                        event_time = datetime.fromisoformat(event["timestamp"])
                        if start_date and event_time < start_date:
                            continue
                        if end_date and event_time > end_date:
                            continue

                        # Update statistics
                        stats["total_events"] += 1

                        if event.get("client_id"):
                            stats["unique_clients"].add(event["client_id"])

                        if event.get("client_type"):
                            stats["by_client_type"][event["client_type"]] = (
                                stats["by_client_type"].get(event["client_type"], 0) + 1
                            )

                        if event.get("tool_name"):
                            stats["by_tool"][event["tool_name"]] = (
                                stats["by_tool"].get(event["tool_name"], 0) + 1
                            )

                        stats["by_event_type"][event["event_type"]] = (
                            stats["by_event_type"].get(event["event_type"], 0) + 1
                        )

                        if not event.get("success", True):
                            stats["errors"] += 1

                    except json.JSONDecodeError:
                        continue

        # Convert set to count
        stats["unique_clients"] = len(stats["unique_clients"])

        return stats


# Global audit logger instance
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """Get the global audit logger instance."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
