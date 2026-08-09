"""
Token-based Cost Tracking for Marcus AI Usage.

Tracks real-time AI token consumption, spend rates, and project costs
based on actual usage rather than naive hourly estimates.
"""

import asyncio
import json
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

from src.core.paths import marcus_data_dir


class TokenTracker:
    """
    Tracks AI token usage per project with real-time cost monitoring.

    Features:
    - Per-project token tracking
    - Real-time spend rate calculation
    - Cost projections based on current usage
    - Token usage history and analytics
    """

    def __init__(self, cost_per_1k_tokens: float = 0.03):
        """
        Initialize token tracker.

        Parameters
        ----------
        cost_per_1k_tokens : float
            Cost per 1000 tokens (default $0.03 for Claude)
        """
        self.cost_per_1k_tokens = cost_per_1k_tokens

        # Project tracking
        self.project_tokens: Dict[str, int] = defaultdict(int)
        self.project_costs: Dict[str, float] = defaultdict(float)

        # Real-time tracking with sliding windows
        self.token_history: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=1000)  # Keep last 1000 token events
        )

        # Session tracking
        self.session_start_times: Dict[str, datetime] = {}
        self.session_tokens: Dict[str, int] = defaultdict(int)

        # Rate tracking
        self.spend_rates: Dict[str, List[float]] = defaultdict(list)

        # Persistence
        self.data_file = marcus_data_dir() / "token_usage.json"
        self.load_historical_data()

        # Start background rate calculator
        self._rate_task: Optional[asyncio.Task[None]] = None

    def load_historical_data(self) -> None:
        """Load historical token usage data."""
        if self.data_file.exists():
            try:
                with open(self.data_file, "r") as f:
                    data = json.load(f)
                    self.project_tokens = defaultdict(
                        int, data.get("project_tokens", {})
                    )
                    self.project_costs = defaultdict(
                        float, data.get("project_costs", {})
                    )
            except Exception as e:
                print(f"Failed to load token history: {e}", file=sys.stderr)

    def save_data(self) -> None:
        """Persist token usage data."""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "project_tokens": dict(self.project_tokens),
            "project_costs": dict(self.project_costs),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.data_file, "w") as f:
            json.dump(data, f, indent=2)

    async def track_tokens(
        self,
        project_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str = "claude-3-sonnet",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Track token usage for a project.

        Parameters
        ----------
        project_id : str
            Project identifier
        input_tokens : int
            Number of input tokens
        output_tokens : int
            Number of output tokens
        model : str
            AI model used
        metadata : Optional[Dict[str, Any]]
            Additional context (task_id, agent_id, etc.)

        Returns
        -------
        Dict[str, Any]
            Dict with usage stats and cost info
        """
        total_tokens = input_tokens + output_tokens
        timestamp = datetime.now(timezone.utc)

        # Update totals
        self.project_tokens[project_id] += total_tokens
        cost = (total_tokens / 1000) * self.cost_per_1k_tokens
        self.project_costs[project_id] += cost

        # Track for rate calculation
        self.token_history[project_id].append(
            {
                "timestamp": timestamp,
                "tokens": total_tokens,
                "cost": cost,
                "metadata": metadata or {},
            }
        )

        # Update session tracking
        if project_id not in self.session_start_times:
            self.session_start_times[project_id] = timestamp
        self.session_tokens[project_id] += total_tokens

        # Calculate current stats
        stats = self.get_project_stats(project_id)

        # Save periodically
        if len(self.token_history[project_id]) % 10 == 0:
            self.save_data()

        return stats

    def get_project_stats(self, project_id: str) -> Dict[str, Any]:
        """
        Get comprehensive stats for a project.

        Parameters
        ----------
        project_id : str
            Project identifier

        Returns
        -------
        Dict[str, Any]
            Dict containing:
            - total_tokens: Total tokens used
            - total_cost: Total cost incurred
            - current_spend_rate: Tokens/hour over last 5 minutes
            - average_spend_rate: Overall tokens/hour
            - projected_cost: Estimated total cost at current rate
            - session_duration: Time since first token
        """
        if project_id not in self.project_tokens:
            return {
                "total_tokens": 0,
                "total_cost": 0.0,
                "current_spend_rate": 0.0,
                "average_spend_rate": 0.0,
                "projected_cost": 0.0,
                "session_duration": 0,
            }

        # Basic stats
        total_tokens = self.project_tokens[project_id]
        total_cost = self.project_costs[project_id]

        # Calculate spend rates
        current_rate = self._calculate_current_spend_rate(project_id)
        avg_rate = self._calculate_average_spend_rate(project_id)

        # Project cost based on current burn rate
        projected_cost = self._project_total_cost(project_id, current_rate)

        # Session duration
        if project_id in self.session_start_times:
            duration = (
                datetime.now(timezone.utc) - self.session_start_times[project_id]
            ).total_seconds()
        else:
            duration = 0

        return {
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "current_spend_rate": round(current_rate, 2),  # tokens/hour
            "average_spend_rate": round(avg_rate, 2),
            "projected_cost": round(projected_cost, 2),
            "session_duration": int(duration),
            "cost_per_hour": round((current_rate / 1000) * self.cost_per_1k_tokens, 2),
        }

    def _calculate_current_spend_rate(self, project_id: str) -> float:
        """Calculate current spend rate (tokens/hour) over last 5 minutes."""
        if project_id not in self.token_history:
            return 0.0

        history = list(self.token_history[project_id])
        if len(history) < 2:
            return 0.0

        # Get events from last 5 minutes
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        recent_events = [e for e in history if e["timestamp"] > cutoff]

        if len(recent_events) < 2:
            # Fall back to last 10 events if not enough recent
            recent_events = history[-10:]

        if len(recent_events) < 2:
            return 0.0

        # Calculate rate
        time_span = (
            recent_events[-1]["timestamp"] - recent_events[0]["timestamp"]
        ).total_seconds()
        if time_span == 0:
            return 0.0

        total_tokens = sum(e["tokens"] for e in recent_events)
        tokens_per_second = total_tokens / time_span
        tokens_per_hour = tokens_per_second * 3600

        return float(tokens_per_hour)

    def _calculate_average_spend_rate(self, project_id: str) -> float:
        """Calculate average spend rate over entire session."""
        if project_id not in self.session_start_times:
            return 0.0

        duration = (
            datetime.now(timezone.utc) - self.session_start_times[project_id]
        ).total_seconds()
        if duration == 0:
            return 0.0

        tokens_per_second = self.session_tokens[project_id] / duration
        return tokens_per_second * 3600

    def _project_total_cost(self, project_id: str, current_rate: float) -> float:
        """Project total cost based on current burn rate."""
        # This is a simple projection - could be enhanced with:
        # - Task completion estimates
        # - Historical patterns
        # - Complexity analysis

        # For now, assume 20% of tasks complete = 20% of tokens used
        # This would integrate with task tracking
        current_cost = self.project_costs[project_id]

        # Rough estimate: if we've spent X so far at current rate,
        # project 5x for a full project (assuming 20% complete)
        # This should be replaced with actual task completion percentage
        if current_cost > 0:
            return current_cost * 5  # Placeholder multiplier

        return 0.0

    def get_all_projects_summary(self) -> Dict[str, Any]:
        """Get summary of all tracked projects."""
        summary = {}
        total_tokens = 0
        total_cost = 0.0

        for project_id in self.project_tokens:
            stats = self.get_project_stats(project_id)
            summary[project_id] = stats
            total_tokens += stats["total_tokens"]
            total_cost += stats["total_cost"]

        return {
            "projects": summary,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 2),
            "active_projects": len(
                [p for p in summary.values() if p["current_spend_rate"] > 0]
            ),
        }

    async def start_monitoring(self) -> None:
        """Start background monitoring task."""
        if self._rate_task is None:
            self._rate_task = asyncio.create_task(self._monitor_rates())

    async def stop_monitoring(self) -> None:
        """Stop background monitoring."""
        if self._rate_task:
            self._rate_task.cancel()
            self._rate_task = None

    async def _monitor_rates(self) -> None:
        """Background task to monitor spend rates and alert on anomalies."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute

                for project_id in self.project_tokens:
                    stats = self.get_project_stats(project_id)
                    current_rate = stats["current_spend_rate"]

                    # Track rate history
                    self.spend_rates[project_id].append(current_rate)

                    # Keep only last hour of rates
                    if len(self.spend_rates[project_id]) > 60:
                        self.spend_rates[project_id] = self.spend_rates[project_id][
                            -60:
                        ]

                    # Check for anomalies
                    if len(self.spend_rates[project_id]) > 10:
                        avg_rate = sum(self.spend_rates[project_id][-10:]) / 10
                        if current_rate > avg_rate * 2 and current_rate > 10000:
                            # Alert: Spending spike detected
                            msg = (
                                f"⚠️ Token spend spike for {project_id}: "
                                f"{current_rate:.0f} tokens/hour"
                            )
                            print(msg, file=sys.stderr)

                # Periodic save
                self.save_data()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in rate monitor: {e}", file=sys.stderr)


# Global instance
token_tracker = TokenTracker()
