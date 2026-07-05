"""
Assignment Lease System for automatic task recovery.

This module implements a lease-based assignment system where tasks are assigned
with time-limited leases that must be renewed through progress reports. Tasks
with expired leases are automatically returned to the TODO state for reassignment.

Key features:
- Automatic lease renewal on progress reports
- Configurable lease durations based on task complexity
- Escalation for tasks with excessive renewals
- Integration with assignment persistence
"""

import asyncio
import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from src.core.assignment_persistence import AssignmentPersistence
from src.core.event_loop_utils import EventLoopLockManager
from src.core.models import RecoveryInfo, Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface

logger = logging.getLogger(__name__)

# Merge-conflict-aware lease extension (dashboard-v73 fix).
#
# When an agent's lease is about to expire and the agent's worktree has
# unresolved git merge conflicts, Marcus grants a one-shot extension to
# give the agent more time to finish conflict resolution. The extension
# is bounded so a permanently-stuck agent cannot hold a lease forever.
#
# Truth-grounded: the worktree git state IS the source of truth — no
# new agent API surface, no agent self-declaration, agent cannot lie
# or forget. Same mechanism handles the mid-rebase case for free.
MAX_MERGE_CONFLICT_EXTENSIONS = 2  # max grants per lease (=10 min total)
MERGE_CONFLICT_EXTENSION_SECONDS = 300  # 5 min per grant
MERGE_CONFLICT_GIT_TIMEOUT_SECONDS = 10.0  # cap git status to bound the lease loop

# Git porcelain status prefixes that indicate unmerged paths. Any line
# whose first two characters match one of these is an unresolved merge:
#   UU = both modified         (most common — same line edited on both sides)
#   AA = both added            (same path created independently)
#   DD = both deleted          (sanity-edge — both branches removed it)
#   DU/UD = deleted vs modified  (one side removed, other modified)
#   AU/UA = added vs modified    (one side created, other modified pre-existing)
# See ``man git-status`` "Output → Short Format" for the full grammar.
_GIT_UNMERGED_PREFIXES = ("UU", "AA", "DD", "DU", "UD", "AU", "UA")


class LeaseStatus(Enum):
    """Status of an assignment lease."""

    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"  # Less than 1 hour remaining
    EXPIRED = "expired"
    RENEWED = "renewed"


@dataclass
class AssignmentLease:
    """Represents a time-limited assignment lease."""

    task_id: str
    agent_id: str
    assigned_at: datetime
    lease_expires: datetime
    last_renewed: datetime
    renewal_count: int = 0
    estimated_hours: float = 4.0  # From task estimation
    progress_percentage: int = 0
    last_progress_message: str = ""
    grace_period_seconds: Optional[float] = None  # Per-lease adaptive grace
    update_timestamps: list[datetime] = field(default_factory=list)
    # Number of merge-conflict extensions already granted to this
    # lease. Bounded by ``MAX_MERGE_CONFLICT_EXTENSIONS`` so a stuck
    # agent cannot hold the lease forever.
    merge_conflict_extensions: int = 0

    @property
    def median_update_interval(self) -> Optional[float]:
        """Calculate median seconds between progress updates.

        Returns
        -------
        Optional[float]
            Median interval in seconds, or None if fewer than 2 timestamps.
        """
        if len(self.update_timestamps) < 2:
            return None
        sorted_ts = sorted(self.update_timestamps)
        intervals = [
            (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
            for i in range(1, len(sorted_ts))
        ]
        return statistics.median(intervals)

    @property
    def time_remaining(self) -> timedelta:
        """Calculate time remaining on lease."""
        return self.lease_expires - datetime.now(timezone.utc)

    @property
    def is_expired(self) -> bool:
        """Check if lease has expired."""
        return datetime.now(timezone.utc) > self.lease_expires

    @property
    def is_expiring_soon(self) -> bool:
        """Check if lease expires within 1 hour."""
        return self.time_remaining < timedelta(hours=1)

    @property
    def status(self) -> LeaseStatus:
        """Get current lease status."""
        if self.is_expired:
            return LeaseStatus.EXPIRED
        elif self.is_expiring_soon:
            return LeaseStatus.EXPIRING_SOON
        else:
            return LeaseStatus.ACTIVE

    def calculate_renewal_duration(
        self, lease_manager: Optional["AssignmentLeaseManager"] = None
    ) -> timedelta:
        """
        Calculate renewal duration based on progress and history.

        Parameters
        ----------
            lease_manager
                Optional reference to lease manager for config.

        Returns
        -------
            Renewal duration (adaptive based on multiple factors)
        """
        base_hours = 4.0

        # Adjust based on progress
        if self.progress_percentage > 75:
            # Near completion, shorter renewal
            base_hours = 2.0
        elif self.progress_percentage > 50:
            base_hours = 3.0
        elif self.progress_percentage < 25 and self.renewal_count > 2:
            # Low progress with multiple renewals - might be stuck
            base_hours = 2.0

        # Adjust based on task complexity
        if self.estimated_hours > 8:
            # Complex task, allow more time
            base_hours *= 1.5

        # Apply renewal decay if configured
        if lease_manager and hasattr(lease_manager, "renewal_decay_factor"):
            decay_factor = lease_manager.renewal_decay_factor**self.renewal_count
            base_hours *= decay_factor

        # Cap renewals for tasks that are taking too long
        if self.renewal_count > 5:
            base_hours = min(base_hours, 2.0)

        # Apply bounds if lease manager available
        if lease_manager:
            base_hours = max(
                lease_manager.min_lease_hours,
                min(lease_manager.max_lease_hours, base_hours),
            )

        return timedelta(hours=base_hours)


def _ensure_timezone_aware(dt: datetime) -> datetime:
    """
    Ensure a datetime is timezone-aware (UTC).

    Normalizes naive datetimes from old persistence data to UTC.
    This prevents TypeErrors when comparing with timezone-aware datetimes.

    Parameters
    ----------
    dt : datetime
        Datetime to normalize (may be naive or aware)

    Returns
    -------
    datetime
        Timezone-aware datetime in UTC
    """
    if dt.tzinfo is None:
        # Naive datetime - assume it was meant to be UTC
        return dt.replace(tzinfo=timezone.utc)
    return dt


class AssignmentLeaseManager:
    """Manages assignment leases with automatic expiration and renewal."""

    def __init__(
        self,
        kanban_client: KanbanInterface,
        assignment_persistence: AssignmentPersistence,
        default_lease_hours: float = 0.0667,  # 240s (matches Phase 1)
        max_renewals: int = 10,
        warning_threshold_hours: float = 0.0167,  # 1 min (was 1.0 hr)
        priority_multipliers: Optional[Dict[str, float]] = None,
        complexity_multipliers: Optional[Dict[str, float]] = None,
        grace_period_minutes: float = 1.0,  # 60 seconds grace period
        renewal_decay_factor: float = 0.9,
        min_lease_hours: float = 0.05,  # Minimum 180 seconds
        max_lease_hours: float = 0.1,  # Maximum 360 seconds (Phase 3)
        stuck_task_threshold_renewals: int = 5,
        enable_adaptive_leases: bool = True,
        task_list: Optional[List[Task]] = None,
        silence_multiplier: float = 5.0,
        max_silent_recoveries: int = 3,
    ):
        """
        Initialize the lease manager.

        Parameters
        ----------
            kanban_client
                Interface to kanban board.
            assignment_persistence
                Assignment persistence layer.
            default_lease_hours
                Default lease duration in hours.
            max_renewals
                Maximum allowed renewals before escalation.
            warning_threshold_hours
                Hours before expiry to warn.
            priority_multipliers
                Lease duration multipliers by priority.
            complexity_multipliers
                Lease duration multipliers by label/type.
            grace_period_minutes
                Grace period in minutes (float) after expiry before recovery.
            renewal_decay_factor
                Factor to reduce renewal duration over time.
            min_lease_hours
                Minimum allowed lease duration.
            max_lease_hours
                Maximum allowed lease duration.
            stuck_task_threshold_renewals
                Renewals before considering task stuck.
            enable_adaptive_leases
                Enable smart lease duration adjustments.
            task_list
                Optional reference to project tasks for recovery info updates.
            max_silent_recoveries
                Circuit breaker (issue #667 Fix 3a): number of CONSECUTIVE
                silent recoveries (lease expired with zero progress updates,
                ``renewal_count == 0``) after which the task is marked
                BLOCKED instead of returned to TODO. Stops the
                reclaim/re-assign runaway loop regardless of who supplies
                agents. A recovery where the agent reported progress resets
                the streak.
        """
        self.kanban_client = kanban_client
        self.assignment_persistence = assignment_persistence
        self.task_list = task_list if task_list is not None else []
        self.default_lease_hours = default_lease_hours
        self.max_renewals = max_renewals
        self.warning_threshold_hours = warning_threshold_hours

        # Advanced configuration
        self.priority_multipliers = priority_multipliers or {
            "critical": 0.5,  # Shorter leases for urgent tasks
            "high": 0.75,
            "medium": 1.0,
            "low": 1.5,
        }
        self.complexity_multipliers = complexity_multipliers or {
            "simple": 0.5,
            "complex": 1.5,
            "research": 2.0,
            "epic": 3.0,
        }
        self.grace_period_minutes = grace_period_minutes
        self.renewal_decay_factor = renewal_decay_factor
        self.min_lease_hours = min_lease_hours
        self.max_lease_hours = max_lease_hours
        self.stuck_task_threshold_renewals = stuck_task_threshold_renewals
        self.enable_adaptive_leases = enable_adaptive_leases
        self.silence_multiplier = silence_multiplier
        self.max_silent_recoveries = max_silent_recoveries

        # Circuit breaker state (issue #667 Fix 3a): consecutive SILENT
        # recovery count per task. In-memory by design — the observed
        # runaway (81 registrations over 3+ hours) happened inside one
        # server process. Restart-durable counters ride with the
        # session-model registration-persistence work (epic #706).
        self._silent_recovery_streaks: Dict[str, int] = {}

        # Active leases tracked in memory
        self.active_leases: Dict[str, AssignmentLease] = {}

        # Observability counters — every increment indicates a latent
        # coordination bug. Surfaced via logs at WARNING level and
        # available for MLflow/metrics export.
        self.recoveries_skipped_terminal_status: int = 0

        # Optional callback invoked after a successful recovery
        # Server sets this to clean up in-memory tracking structures
        # (agent_tasks, tasks_being_assigned) that lease manager
        # can't access directly.
        self.on_recovery_callback: Optional[Callable[[str, str], None]] = None

        # Track lease history for analysis (max 1000 entries to prevent memory leak)
        self.lease_history: Deque[Dict[str, Any]] = deque(maxlen=1000)

        # Lock manager for event loop safe operations
        self._lock_manager = EventLoopLockManager()

    @property
    def lease_lock(self) -> asyncio.Lock:
        """Get lease lock for the current event loop."""
        return self._lock_manager.get_lock()

    def update_task_list(self, task_list: List[Task]) -> None:
        """
        Update the task list reference.

        Called by MarcusServer when project_tasks is refreshed.

        Parameters
        ----------
        task_list : List[Task]
            Updated list of project tasks
        """
        self.task_list = task_list

    async def create_lease(
        self, task_id: str, agent_id: str, task: Optional[Task] = None
    ) -> AssignmentLease:
        """
        Create a new assignment lease.

        Parameters
        ----------
            task_id
                ID of the task being assigned.
            agent_id
                ID of the agent receiving assignment.
            task
                Optional task object for additional context.

        Returns
        -------
            Created assignment lease
        """
        async with self.lease_lock:
            now = datetime.now(timezone.utc)

            # Calculate initial lease duration
            # Use progressive timeout phase-1 if in aggressive mode
            if self.default_lease_hours < 1.0:
                # Aggressive mode: Use phase-1 timeout for unproven agents
                lease_seconds, _ = self.calculate_adaptive_timeout(
                    progress=0, update_count=0, has_recent_activity=False
                )
                base_hours = lease_seconds / 3600
            else:
                # Conservative mode: Use default or task-based estimation
                base_hours = self.default_lease_hours

            # Only use task-based estimation if default is conservative (> 1 hour)
            if task and self.enable_adaptive_leases and self.default_lease_hours > 1.0:
                # Use task estimation if available
                if task.estimated_hours:
                    base_hours = task.estimated_hours

                # Apply priority multiplier
                if hasattr(task, "priority"):
                    priority_mult = self.priority_multipliers.get(
                        task.priority.value.lower(), 1.0
                    )
                    base_hours *= priority_mult

                # Apply complexity multiplier based on labels
                if hasattr(task, "labels"):
                    for label in task.labels:
                        label_lower = label.lower()
                        if label_lower in self.complexity_multipliers:
                            base_hours *= self.complexity_multipliers[label_lower]
                            break  # Use first matching complexity

            # Enforce min/max bounds
            base_hours = max(
                self.min_lease_hours, min(self.max_lease_hours, base_hours)
            )

            initial_duration = timedelta(hours=base_hours)

            lease = AssignmentLease(
                task_id=task_id,
                agent_id=agent_id,
                assigned_at=now,
                lease_expires=now + initial_duration,
                last_renewed=now,
                estimated_hours=task.estimated_hours if task else base_hours,
            )

            # Store lease
            self.active_leases[task_id] = lease

            # Update assignment persistence with lease info
            await self._persist_lease(lease)

            # Log lease creation
            logger.info(
                f"Created lease for task {task_id} to agent {agent_id} "
                f"(expires: {lease.lease_expires.isoformat()})"
            )

            # Track in history
            self.lease_history.append(
                {
                    "event": "lease_created",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "timestamp": now.isoformat(),
                    "expires": lease.lease_expires.isoformat(),
                }
            )

            return lease

    async def renew_lease(
        self, task_id: str, progress: int, message: str = ""
    ) -> Optional[AssignmentLease]:
        """
        Renew an existing lease based on progress report.

        Uses progressive timeout strategy to adapt lease duration based on
        task progress and agent reliability.

        Parameters
        ----------
            task_id
                ID of the task.
            progress
                Current progress percentage.
            message
                Progress message.

        Returns
        -------
            Renewed lease or None if not found/expired
        """
        async with self.lease_lock:
            lease = self.active_leases.get(task_id)
            if not lease:
                logger.warning(f"No active lease found for task {task_id}")
                return None

            if lease.is_expired:
                # Capture the progress value before giving up on the
                # renewal. The lease itself cannot be renewed — that
                # decision belongs to the monitor — but
                # ``lease.progress_percentage`` must reflect the
                # agent's latest self-reported value so that when the
                # monitor eventually recovers this lease, the
                # recovery context carries the agent's actual
                # progress, not a stale pre-expiry snapshot.
                #
                # Issue #342 (dashboard-v70 Epictetus audit): agent
                # reported 25% then 50%, but the 50% report arrived
                # after the lease silently expired. Without this
                # capture, the progress value is dropped on the
                # floor here and the recovering agent sees 25% (or
                # 0%) and rebuilds from scratch — dashboard-v70
                # produced 341 lines of ghost source + 506 lines of
                # ghost tests this way.
                #
                # Guard with ``>`` so we never regress the snapshot
                # (e.g. if two out-of-order late reports arrive, the
                # higher value wins).
                if progress > lease.progress_percentage:
                    old_progress = lease.progress_percentage
                    lease.progress_percentage = progress
                    if message:
                        lease.last_progress_message = message
                    logger.info(
                        f"Captured late progress on expired lease "
                        f"{task_id}: {old_progress}% → {progress}% "
                        f"(lease still expired, recovery context "
                        f"will use the updated snapshot)"
                    )
                logger.warning(f"Cannot renew expired lease for task {task_id}")
                return None

            # Update progress
            lease.progress_percentage = progress
            lease.last_progress_message = message

            # Track update timestamp for cadence-based recovery
            lease.update_timestamps.append(datetime.now(timezone.utc))

            # Use progressive timeout if in aggressive mode (< 1 hour default)
            if self.default_lease_hours < 1.0:
                # Progressive timeout mode: calculate based on progress
                lease_seconds, grace_seconds = self.calculate_adaptive_timeout(
                    progress=progress,
                    update_count=lease.renewal_count + 1,  # +1 for this renewal
                    has_recent_activity=True,  # Just reported progress
                )
                renewal_duration = timedelta(seconds=lease_seconds)
                lease.grace_period_seconds = float(grace_seconds)

                logger.info(
                    f"Progressive timeout for {task_id}: {lease_seconds}s "
                    f"+ {grace_seconds}s grace "
                    f"(progress={progress}%, updates={lease.renewal_count + 1})"
                )
            else:
                # Conservative mode: use old calculation logic
                renewal_duration = lease.calculate_renewal_duration(self)

            # Renew lease
            lease.last_renewed = datetime.now(timezone.utc)
            lease.lease_expires = lease.last_renewed + renewal_duration
            lease.renewal_count += 1

            # Check for excessive renewals
            if lease.renewal_count >= self.max_renewals:
                logger.warning(
                    f"Task {task_id} has been renewed {lease.renewal_count} times. "
                    f"Consider escalation or reassignment."
                )

            # Update persistence
            await self._persist_lease(lease)

            # Log renewal
            logger.info(
                f"Renewed lease for task {task_id} "
                f"(progress: {progress}%, expires: {lease.lease_expires.isoformat()})"
            )

            # Track in history
            self.lease_history.append(
                {
                    "event": "lease_renewed",
                    "task_id": task_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "progress": progress,
                    "renewal_count": lease.renewal_count,
                    "new_expiry": lease.lease_expires.isoformat(),
                }
            )

            return lease

    async def extend_for_validation(
        self, task_id: str, window_seconds: int = 600
    ) -> Optional[AssignmentLease]:
        """Extend a lease by a validation window after the agent reports completion.

        When an agent reports ``status=completed``, Marcus's smoke
        gate runs (install, build, run app, hit endpoints, contract
        verifications) and may reject the completion. If rejected,
        the agent is expected to remediate and re-report. The total
        validation + remediation cycle routinely exceeds the
        progressive-timeout phase-4 ceiling of 7m30s.

        Before this method existed, ``report_task_progress`` skipped
        lease renewal entirely on ``status=completed`` (see
        ``src/marcus_mcp/tools/task.py``), so the post-completion
        cycle ran under an expiring lease. That caused
        ``agent_unicorn_1_11`` on the snake_game project to lose
        its lease mid-validation, have its late completion rejected
        as stale (Issue #343), and have its verification artifacts
        discarded. See issue #667 Fix 1.

        Semantics:

        - The lease's ``lease_expires`` is set to
          ``now + window_seconds``. Anchored on now, NOT extended on
          top of any prior expiry — multiple completion reports
          don't stack into hours of lease.
        - ``progress_percentage`` is preserved (not reset to renewal
          curve). Otherwise the next ``touch_lease`` would re-enter
          Phase 1's short window and the validation coverage would
          collapse.
        - Returns ``None`` if the lease is missing or already expired,
          mirroring ``renew_lease``. Late completions on expired
          leases are handled by Fix 2's transactional-accept path.

        Parameters
        ----------
        task_id : str
            ID of the task whose lease to extend.
        window_seconds : int, optional
            Length of the validation window in seconds. Default 600
            (10 minutes). Configurable per task type via the caller.

        Returns
        -------
        Optional[AssignmentLease]
            The extended lease, or None if no active lease or already
            expired.
        """
        async with self.lease_lock:
            lease = self.active_leases.get(task_id)
            if not lease:
                logger.warning(
                    f"No active lease found for task {task_id} on "
                    f"validation-window extension request"
                )
                return None

            if lease.is_expired:
                logger.warning(
                    f"Cannot extend expired lease for task {task_id} "
                    f"into validation window (Fix 2 transactional "
                    f"late-accept path may still recover this work)"
                )
                return None

            now = datetime.now(timezone.utc)
            lease.last_renewed = now
            lease.lease_expires = now + timedelta(seconds=window_seconds)
            # ``progress_percentage`` deliberately NOT touched —
            # completion was already reported at 100% and the next
            # ``touch_lease`` should continue from phase 4, not
            # restart from phase 1.
            lease.update_timestamps.append(now)

            await self._persist_lease(lease)

            logger.info(
                f"Extended lease for task {task_id} by validation window "
                f"({window_seconds}s, expires: "
                f"{lease.lease_expires.isoformat()})"
            )

            self.lease_history.append(
                {
                    "event": "lease_extended_for_validation",
                    "task_id": task_id,
                    "timestamp": now.isoformat(),
                    "window_seconds": window_seconds,
                    "new_expiry": lease.lease_expires.isoformat(),
                }
            )

            return lease

    async def touch_lease(self, agent_id: str) -> bool:
        """
        Extend an agent's lease without changing progress.

        Called on any MCP tool activity to prove the agent is alive.
        This is a lightweight alternative to renew_lease that doesn't
        require progress data or update cadence tracking.

        Parameters
        ----------
        agent_id : str
            ID of the agent whose lease to extend.

        Returns
        -------
        bool
            True if a lease was touched, False if no active lease found.
        """
        async with self.lease_lock:
            # Find lease by agent_id
            lease = None
            for active_lease in self.active_leases.values():
                if active_lease.agent_id == agent_id:
                    lease = active_lease
                    break

            if not lease or lease.is_expired:
                return False

            # Extend by the current phase timeout
            lease_seconds, _ = self.calculate_adaptive_timeout(
                progress=lease.progress_percentage,
                update_count=max(lease.renewal_count, 1),
                has_recent_activity=True,
            )
            now = datetime.now(timezone.utc)
            lease.last_renewed = now
            lease.lease_expires = now + timedelta(seconds=lease_seconds)

            # Update timestamp for cadence tracking
            lease.update_timestamps.append(now)

            logger.debug(
                f"Touched lease for agent {agent_id} "
                f"(task {lease.task_id}, "
                f"expires: {lease.lease_expires.isoformat()})"
            )
            return True

    async def check_expired_leases(self) -> List[AssignmentLease]:
        """
        Check for expired leases that need recovery.

        Two-phase to avoid holding ``lease_lock`` during git subprocess
        I/O (Codex P2 on PR #350). Holding the global lock during
        per-lease ``git status`` calls would serialize every concurrent
        ``renew_lease`` and ``touch_lease`` for the duration of the
        slowest probe, and could cause active agents' renewals to
        starve and look expired in the next cycle.

        Phase 1 (lock held, no I/O):
            Snapshot leases that have crossed the grace deadline.

        Phase 2 (lock released, may do I/O):
            For each candidate, try the merge-conflict extension. The
            extension helper does a git probe outside the lock and
            briefly re-acquires the lock only to mutate + persist the
            lease atomically.

        Before returning a lease as expired, the merge-conflict
        extension may grant up to ``MAX_MERGE_CONFLICT_EXTENSIONS``
        extensions of ``MERGE_CONFLICT_EXTENSION_SECONDS`` each when
        the agent's worktree has unresolved git conflicts. See the
        constants at the top of this module for the rationale.

        Returns
        -------
            List of expired leases (considering grace period)
        """
        expired_leases: List[AssignmentLease] = []
        now = datetime.now(timezone.utc)
        default_grace_delta = timedelta(minutes=self.grace_period_minutes)

        # Phase 1: collect candidates under lock, no I/O.
        candidates: List[tuple[AssignmentLease, datetime]] = []
        async with self.lease_lock:
            for task_id, lease in list(self.active_leases.items()):
                if not lease.is_expired:
                    continue
                # Use per-lease adaptive grace if set, else global default
                if lease.grace_period_seconds is not None:
                    grace_delta = timedelta(seconds=lease.grace_period_seconds)
                else:
                    grace_delta = default_grace_delta
                grace_deadline = lease.lease_expires + grace_delta
                if now > grace_deadline:
                    candidates.append((lease, grace_deadline))
                else:
                    logger.debug(
                        f"Lease expired but in grace period: task {task_id} "
                        f"(expires fully at: {grace_deadline.isoformat()})"
                    )

        # Phase 2: probe + extend OUTSIDE the lock so concurrent
        # renew_lease/touch_lease calls aren't blocked on git I/O.
        # Last-chance check: is the agent stuck on a merge conflict?
        # dashboard-v73 demonstrated this case — agent was actively
        # resolving conflicts when the lease expired, work was lost
        # on recovery.
        for lease, grace_deadline in candidates:
            extended = await self._try_extend_for_merge_conflict(lease)
            if extended:
                continue
            # Re-check: a concurrent renew_lease may have advanced the
            # lease while the git probe ran. If the lease is no longer
            # expired, skip recovery — the agent is still active.
            if not lease.is_expired:
                continue
            expired_leases.append(lease)
            logger.info(
                f"Found expired lease: task {lease.task_id} "
                f"(expired: {lease.lease_expires.isoformat()}, "
                f"grace ended: {grace_deadline.isoformat()})"
            )

        # Diagnostic: surface the gap between what is_expired counts and
        # what actually becomes a recovery candidate. When these disagree,
        # an expired lease is being silently filtered (grace, extension,
        # or a concurrent renew). Enable via MARCUS_DEBUG_LEASE=1.
        raw_expired = [tid for tid, ls in self.active_leases.items() if ls.is_expired]
        logger.debug(
            f"check_expired_leases: {len(raw_expired)} lease(s) is_expired="
            f"{raw_expired}, {len(candidates)} past grace, "
            f"{len(expired_leases)} returned for recovery="
            f"{[ls.task_id for ls in expired_leases]}"
        )

        return expired_leases

    def _resolve_worktree_path(self, lease: AssignmentLease) -> Optional[Path]:
        """
        Resolve the worktree path for an agent's lease.

        Marcus's worktree convention (set by the experiment runner at
        ``dev-tools/experiments/runners/spawn_agents.py``) places agent
        worktrees at ``<experiment_dir>/worktrees/<agent_id>``, where
        ``experiment_dir`` is the parent of the project ``implementation``
        directory. The kanban client exposes the project root via its
        workspace state.

        Defensive: any failure (no workspace state method, malformed
        state, missing or non-string project_root, missing worktree
        directory) returns None. The caller treats None as "not
        eligible for merge-conflict extension" and proceeds with
        normal recovery.

        Parameters
        ----------
        lease : AssignmentLease
            The lease whose agent's worktree we want to find.

        Returns
        -------
        Optional[Path]
            The worktree path if it exists on disk, or None for any
            unresolvable case (single-branch projects, non-experiment
            runs, mocked kanban clients in tests).
        """
        load_state = getattr(self.kanban_client, "_load_workspace_state", None)
        if not callable(load_state):
            return None
        try:
            workspace_state = load_state()
            if not isinstance(workspace_state, dict):
                return None
            project_root = workspace_state.get("project_root")
            if not isinstance(project_root, str) or not project_root:
                return None
            # Marcus convention: worktrees live as siblings to implementation/
            worktree = Path(project_root).parent / "worktrees" / lease.agent_id
            if not worktree.exists():
                return None
            return worktree
        except Exception as e:
            logger.debug(f"Could not resolve worktree path for {lease.task_id}: {e}")
            return None

    async def _has_unresolved_conflicts(self, worktree: Path) -> bool:
        """
        Check whether a worktree has unresolved merge conflicts.

        Runs ``git status --porcelain`` and looks for status codes that
        indicate unmerged paths (UU, AA, DD, DU, UD, AU, UA). Bounded
        by ``MERGE_CONFLICT_GIT_TIMEOUT_SECONDS`` so a wedged git
        process cannot stall the lease loop. Any subprocess error,
        timeout, or non-git worktree returns False — when in doubt,
        assume no conflict and let the lease expire normally.

        Parameters
        ----------
        worktree : Path
            Worktree directory to inspect.

        Returns
        -------
        bool
            True if at least one unmerged path is present.
        """
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=MERGE_CONFLICT_GIT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"git status timed out after "
                    f"{MERGE_CONFLICT_GIT_TIMEOUT_SECONDS}s for {worktree}; "
                    f"treating as no-conflict"
                )
                # Best-effort cleanup; subprocess kill returns immediately
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                return False
            if proc.returncode != 0:
                return False
        except Exception as e:
            logger.debug(f"git status failed for {worktree}: {e}")
            return False

        for line in stdout.decode("utf-8", errors="replace").splitlines():
            # Porcelain format: XY <path>. The XY is the first 2 chars.
            if len(line) >= 2 and line[:2] in _GIT_UNMERGED_PREFIXES:
                return True
        return False

    async def _try_extend_for_merge_conflict(self, lease: AssignmentLease) -> bool:
        """
        Grant a merge-conflict extension if eligible.

        Called from ``check_expired_leases`` OUTSIDE the lease lock so
        the git subprocess probe doesn't serialize concurrent
        ``renew_lease``/``touch_lease`` calls (Codex P2 on PR #350).

        Phases:
          1. Eligibility precheck (no lock, no I/O): cap, worktree path
          2. Git probe (no lock, async subprocess I/O bounded by
             ``MERGE_CONFLICT_GIT_TIMEOUT_SECONDS``)
          3. Apply extension under the lock, then persist outside it.
             The under-lock section re-verifies three conditions before
             mutating the lease:

             a. Lease is still in ``active_leases`` (not removed by a
                concurrent recovery path).
             b. Lease is still expired (guards against ``renew_lease``
                succeeding while the git probe ran — if the agent
                renewed, skip the extension so we don't burn a
                conflict-extension slot on an active lease).
             c. Extension cap not yet reached.

             The expiry timestamp is taken at grant time inside the
             lock, not at the start of the scan, so each candidate
             receives the full intended extension window regardless of
             how long prior candidates' git probes took.

        On success, extends the lease by
        ``MERGE_CONFLICT_EXTENSION_SECONDS``, increments
        ``lease.merge_conflict_extensions``, and persists the lease so
        a service restart during the extension window does not reload
        the old expiry and trigger immediate recovery (Codex P1 on
        PR #350).

        Parameters
        ----------
        lease : AssignmentLease
            The expired lease being considered for recovery.

        Returns
        -------
        bool
            True if extension granted, False otherwise.
        """
        # Phase 1: cheap eligibility checks (no lock, no I/O).
        if lease.merge_conflict_extensions >= MAX_MERGE_CONFLICT_EXTENSIONS:
            return False
        worktree = self._resolve_worktree_path(lease)
        if worktree is None:
            return False

        # Phase 2: git probe (no lock; bounded by timeout).
        if not await self._has_unresolved_conflicts(worktree):
            return False

        # Phase 3: apply the extension atomically under the lease lock.
        # Three defensive re-checks before mutating:
        # (a) lease still in active_leases — not removed by concurrent recovery
        # (b) lease still expired — guards against renew_lease winning the race
        #     while the git probe ran; if the agent renewed successfully, skip
        #     the extension so we don't burn a conflict-extension slot on an
        #     active lease (Codex P1 review, PR #384)
        # (c) extension cap not yet reached (re-check under lock)
        # grant_time is taken here, not at scan start, so each candidate
        # receives the full intended extension regardless of prior probe
        # durations (Codex P2 review, PR #384).
        async with self.lease_lock:
            if lease.task_id not in self.active_leases:
                return False
            if not lease.is_expired:
                return False
            if lease.merge_conflict_extensions >= MAX_MERGE_CONFLICT_EXTENSIONS:
                return False

            grant_time = datetime.now(timezone.utc)
            extension = timedelta(seconds=MERGE_CONFLICT_EXTENSION_SECONDS)
            lease.lease_expires = grant_time + extension
            lease.last_renewed = grant_time
            lease.merge_conflict_extensions += 1

            self.lease_history.append(
                {
                    "event": "merge_conflict_extension",
                    "task_id": lease.task_id,
                    "agent_id": lease.agent_id,
                    "timestamp": grant_time.isoformat(),
                    "extension_count": lease.merge_conflict_extensions,
                    "new_expiry": lease.lease_expires.isoformat(),
                    "worktree": str(worktree),
                }
            )

        # Persist OUTSIDE the lock — _persist_lease does I/O against
        # assignment_persistence and we already hold a consistent
        # in-memory snapshot of the lease state.
        await self._persist_lease(lease)

        logger.info(
            f"Granted merge-conflict extension for task {lease.task_id} "
            f"to agent {lease.agent_id} "
            f"(extension {lease.merge_conflict_extensions}/"
            f"{MAX_MERGE_CONFLICT_EXTENSIONS}, "
            f"new expiry: {lease.lease_expires.isoformat()}, "
            f"unresolved conflicts in {worktree})"
        )
        return True

    def _find_task(self, task_id: str) -> Optional[Task]:
        """
        Find a task by ID in the task list.

        Parameters
        ----------
            task_id
                Task ID to find.

        Returns
        -------
            Task object if found, None otherwise
        """
        for task in self.task_list:
            if task.id == task_id:
                return task
        return None

    async def recover_expired_lease(self, lease: AssignmentLease) -> bool:
        """
        Recover a task with an expired lease.

        Implements dual-write pattern:
        1. Updates task model with structured RecoveryInfo (source of truth)
        2. Posts to Kanban comments for audit trail (observability)

        Parameters
        ----------
            lease
                The expired lease to recover.

        Returns
        -------
            True if recovery successful
        """
        try:
            logger.info(
                f"Recovering task {lease.task_id} from agent {lease.agent_id} "
                f"(expired: {lease.lease_expires.isoformat()})"
            )

            # Circuit breaker (issue #667 Fix 3a): a SILENT recovery is one
            # where the agent never reported progress (renewal_count == 0 —
            # the `updates=0` signature of the snake_game runaway). After
            # max_silent_recoveries consecutive silent recoveries, mark the
            # task BLOCKED instead of returning it to TODO: the failure is
            # almost certainly external (LLM/API outage, broken env), and
            # every fresh agent will hit it identically. BLOCKED tasks are
            # never offered by request_next_task, so the loop cannot
            # continue — regardless of who is supplying agents
            # (Invariant #1: the guard must live in Marcus, not a spawner).
            if lease.renewal_count == 0:
                streak = self._silent_recovery_streaks.get(lease.task_id, 0) + 1
                self._silent_recovery_streaks[lease.task_id] = streak
                if streak >= self.max_silent_recoveries:
                    return await self._block_task_after_silent_recoveries(lease, streak)
            else:
                # An agent that reported progress but still expired is a
                # slow-but-alive pattern (see #703), not the silent-failure
                # pattern — reset the streak.
                self._silent_recovery_streaks.pop(lease.task_id, None)

            # Calculate time spent
            now = datetime.now(timezone.utc)
            time_spent = now - lease.assigned_at
            time_spent_minutes = time_spent.total_seconds() / 60

            # Create structured recovery info
            # In worktree mode, each agent works on branch marcus/<agent_id>
            previous_branch = f"marcus/{lease.agent_id}"

            # ``lease.progress_percentage`` reflects the agent's
            # latest self-reported progress including late reports
            # that arrived after the lease silently expired. The
            # expired-lease progress capture in ``renew_lease``
            # (Issue #342 fix) updates the snapshot even when the
            # lease itself cannot be renewed, so recovery context
            # now shows what the agent actually reached rather than
            # a pre-expiry snapshot. Dashboard-v70's ghost code was
            # caused by the pre-fix renew_lease path silently
            # dropping the 50% progress report when the lease had
            # already expired.
            # Emit lease_expired telemetry (Marcus #416, Stage 4 of
            # #9).  Fires before the recovery handoff so we record
            # the expiry event even if the recovery path fails
            # downstream.  ``recovery_attempted=True`` — Marcus is
            # putting the task back on the board now; whether the
            # next agent finishes it is observed separately via
            # ``task_completed`` (Kaia review 3 honesty fix).
            try:
                from src.telemetry.events import fire_lease_expired

                fire_lease_expired(
                    task_held_minutes=int(time_spent_minutes),
                    progress_pct_at_expiry=int(lease.progress_percentage),
                    recovery_attempted=True,
                )
            except Exception:  # noqa: BLE001 - never crash recovery
                pass

            recovery_info = RecoveryInfo(
                recovered_at=now,
                recovered_from_agent=lease.agent_id,
                previous_progress=lease.progress_percentage,
                time_spent_minutes=time_spent_minutes,
                recovery_reason="lease_expired",
                previous_agent_branch=previous_branch,
                instructions=(
                    f"⚠️ **RECOVERY ADDENDUM** - This task was recovered "
                    f"from agent {lease.agent_id}\n\n"
                    f"**FIRST: Pick up committed work from the previous "
                    f"agent:**\n"
                    f"```\n"
                    f"git merge {previous_branch} --no-edit\n"
                    f"```\n"
                    f"This merges any commits the previous agent made "
                    f"before they disconnected.\n\n"
                    f"**Then check what was done:**\n"
                    f"1. Run `git log {previous_branch}` to see their "
                    f"commits\n"
                    f"2. Check for artifacts or design documents\n"
                    f"3. Previous agent reached "
                    f"{lease.progress_percentage}%\n"
                    f"4. **Continue from where they left off** - "
                    f"don't restart from scratch\n\n"
                    + (
                        "**Previous agent's last progress note:**\n"
                        + "\n".join(
                            f"> {line}"
                            for line in lease.last_progress_message.strip().splitlines()
                        )
                        + "\n\nUse this note to understand what was built"
                        " and what remains. Wire any completed components"
                        " into the entry point if they are not yet"
                        " reachable.\n\n"
                        if lease.last_progress_message.strip()
                        else f"**Previous agent left no progress notes.**\n"
                        f"Check `git log {previous_branch}` and `git "
                        f"diff main {previous_branch}` to discover what "
                        f"was committed. Do NOT re-implement files that "
                        f"already exist on the branch — read them "
                        f"first.\n\n"
                    )
                    + f"**Recovery Context:**\n"
                    f"- Previous agent: {lease.agent_id}\n"
                    f"- Previous branch: {previous_branch}\n"
                    f"- Time they spent: "
                    f"{time_spent_minutes:.1f} minutes\n"
                    f"- Recovery reason: lease expired (no progress "
                    f"updates)\n"
                    f"- Your task: Complete the ORIGINAL task "
                    f"requirements, building on existing work\n"
                ),
                recovery_expires_at=now + timedelta(hours=24),
            )

            # 1. Update task model (source of truth) if task is available
            task = self._find_task(lease.task_id)
            if task:
                task.recovery_info = recovery_info
                # Clear ownership so task re-enters the assignment pool
                task.assigned_to = None
                logger.info(
                    f"Updated task {lease.task_id} model with recovery "
                    f"info, cleared assigned_to"
                )
            else:
                logger.warning(
                    f"Task {lease.task_id} not found in task list for "
                    f"recovery info update"
                )

            # 2. Dual-write to Kanban for audit trail
            # Don't fail entire recovery if Kanban write fails
            try:
                await self._create_recovery_handoff_comment(lease, recovery_info)
            except Exception as e:
                logger.warning(f"Failed to write recovery comment to Kanban: {e}")
                # Continue - task model update is what matters

            # Remove from active leases
            async with self.lease_lock:
                if lease.task_id in self.active_leases:
                    del self.active_leases[lease.task_id]

            # Remove assignment from persistence
            await self.assignment_persistence.remove_assignment(lease.agent_id)

            # Invoke recovery callback so server can clean in-memory state
            if self.on_recovery_callback is not None:
                try:
                    self.on_recovery_callback(lease.agent_id, lease.task_id)
                except Exception as e:
                    logger.warning(
                        f"Recovery callback failed for task " f"{lease.task_id}: {e}"
                    )

            # Reset task on board: status → TODO, clear assigned_to
            try:
                if hasattr(self.kanban_client, "update_task"):
                    await self.kanban_client.update_task(
                        lease.task_id,
                        {"status": TaskStatus.TODO, "assigned_to": None},
                    )
                elif hasattr(self.kanban_client, "update_task_status"):
                    # Fallback: at least reset status
                    await self.kanban_client.update_task_status(
                        lease.task_id, TaskStatus.TODO
                    )
                    logger.warning(
                        f"Kanban client lacks update_task — "
                        f"assigned_to not cleared on board for "
                        f"{lease.task_id}"
                    )
                else:
                    logger.warning(
                        f"Kanban client does not support task updates, "
                        f"task {lease.task_id} not updated on board"
                    )
            except Exception as e:
                logger.error(f"Failed to reset task {lease.task_id} on board: {e}")
                # Don't fail entire recovery if board update fails
                # In-memory state is already updated

            # Track in history
            self.lease_history.append(
                {
                    "event": "lease_recovered",
                    "task_id": lease.task_id,
                    "agent_id": lease.agent_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "progress_at_recovery": lease.progress_percentage,
                    "total_renewals": lease.renewal_count,
                }
            )

            return True

        except Exception as e:
            logger.error(f"Error recovering lease for task {lease.task_id}: {e}")
            return False

    async def _block_task_after_silent_recoveries(
        self, lease: AssignmentLease, streak: int
    ) -> bool:
        """Mark a task BLOCKED after N consecutive silent recoveries.

        Terminal arm of the circuit breaker (issue #667 Fix 3a). Instead
        of resetting the task to TODO (which would let yet another agent
        claim it and fail identically), flip it to BLOCKED, surface a
        diagnostic comment on the board, and perform the same
        coordination-state cleanup as a normal recovery so no in-memory
        agent state leaks (the issue #485 failure class).

        A human (or a later automated policy) can reset the task to TODO
        to retry; the streak counter is cleared here so a reset starts
        fresh.

        Parameters
        ----------
        lease : AssignmentLease
            The expired lease whose recovery tripped the breaker.
        streak : int
            The consecutive-silent-recovery count that tripped it.

        Returns
        -------
        bool
            True if the BLOCKED transition was applied (cleanup errors on
            non-authoritative writes are logged, not raised).
        """
        logger.warning(
            f"Circuit breaker: task {lease.task_id} has been recovered "
            f"{streak} consecutive times with zero progress updates "
            f"(last agent: {lease.agent_id}). Marking BLOCKED instead of "
            f"TODO — likely external failure (LLM/API outage, broken "
            f"environment). Human investigation required."
        )

        # 1. Update task model (source of truth)
        task = self._find_task(lease.task_id)
        if task:
            task.status = TaskStatus.BLOCKED
            task.assigned_to = None

        # 2. Remove from active leases
        async with self.lease_lock:
            if lease.task_id in self.active_leases:
                del self.active_leases[lease.task_id]

        # 3. Remove assignment from persistence
        await self.assignment_persistence.remove_assignment(lease.agent_id)

        # 4. Invoke recovery callback so server cleans in-memory state
        if self.on_recovery_callback is not None:
            try:
                self.on_recovery_callback(lease.agent_id, lease.task_id)
            except Exception as e:
                logger.warning(
                    f"Recovery callback failed for task {lease.task_id}: {e}"
                )

        # 5. Board: status → BLOCKED, clear ownership
        try:
            if hasattr(self.kanban_client, "update_task"):
                await self.kanban_client.update_task(
                    lease.task_id,
                    {"status": TaskStatus.BLOCKED, "assigned_to": None},
                )
            elif hasattr(self.kanban_client, "update_task_status"):
                await self.kanban_client.update_task_status(
                    lease.task_id, TaskStatus.BLOCKED
                )
        except Exception as e:
            logger.error(f"Failed to mark task {lease.task_id} BLOCKED on board: {e}")

        # 5b. Provider-portable BLOCKED transition (Codex P2 on PR #707).
        # GitHub- and Linear-backed boards do NOT persist status through
        # update_task (GitHubKanban.update_task only opens/closes the
        # issue; LinearKanban.update_task ignores status) — their
        # blocked-state mechanics live in move_task_to_column ("blocked"
        # → GitHub `blocked` label / Linear "Blocked" state / SQLite
        # direct status match). Without this, the next project refresh
        # would re-read the task as open/TODO on those providers and the
        # runaway loop this breaker exists to stop would resume.
        try:
            if hasattr(self.kanban_client, "move_task_to_column"):
                await self.kanban_client.move_task_to_column(lease.task_id, "blocked")
        except Exception as e:
            logger.warning(
                f"move_task_to_column('blocked') failed for {lease.task_id}: {e}"
            )

        # 6. Diagnostic comment (observability dual-write)
        try:
            await self.kanban_client.add_comment(
                lease.task_id,
                (
                    f"🛑 **CIRCUIT BREAKER — task BLOCKED by Marcus**\n\n"
                    f"{streak} consecutive agents claimed this task and "
                    f"reported no progress before their lease expired. "
                    f"This pattern almost always means an external "
                    f"failure every fresh agent hits identically — an "
                    f"LLM/API outage, a broken environment, or a task "
                    f"that cannot run as specified — so retrying with "
                    f"more agents only burns cost (issue #667).\n\n"
                    f"**What to do:** investigate the root cause, then "
                    f"reset this task's status to TODO to retry.\n\n"
                    f"- Last agent: {lease.agent_id}\n"
                    f"- Silent recoveries: {streak} "
                    f"(threshold: {self.max_silent_recoveries})\n"
                ),
            )
        except Exception as e:
            logger.warning(
                f"Failed to write circuit-breaker comment for " f"{lease.task_id}: {e}"
            )

        # 7. Auditable history event
        self.lease_history.append(
            {
                "event": "task_blocked_circuit_breaker",
                "task_id": lease.task_id,
                "agent_id": lease.agent_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "consecutive_silent_recoveries": streak,
                "threshold": self.max_silent_recoveries,
            }
        )

        # 8. Clear the streak — a human reset to TODO starts fresh
        self._silent_recovery_streaks.pop(lease.task_id, None)

        return True

    async def get_expiring_leases(self) -> List[AssignmentLease]:
        """
        Get leases that are expiring soon.

        Returns
        -------
            List of leases expiring within warning threshold
        """
        expiring = []

        async with self.lease_lock:
            for lease in self.active_leases.values():
                if lease.is_expiring_soon and not lease.is_expired:
                    expiring.append(lease)

        return expiring

    async def _persist_lease(self, lease: AssignmentLease) -> None:
        """Persist lease information to assignment persistence."""
        assignment = await self.assignment_persistence.get_assignment(lease.agent_id)
        if assignment:
            assignment["lease_expires"] = lease.lease_expires.isoformat()
            assignment["lease_renewed_at"] = lease.last_renewed.isoformat()
            assignment["renewal_count"] = lease.renewal_count
            assignment["progress_percentage"] = lease.progress_percentage
            assignment["last_progress_update"] = datetime.now(timezone.utc).isoformat()
            assignment["update_timestamps"] = [
                ts.isoformat() for ts in lease.update_timestamps
            ]
            # Persist merge-conflict extension counter so the cap
            # survives a service restart during the extension window
            # (Codex P1 on PR #350).
            assignment["merge_conflict_extensions"] = lease.merge_conflict_extensions
            await self.assignment_persistence.save_assignment(
                lease.agent_id,
                lease.task_id,
                assignment.get("assigned_at", datetime.now(timezone.utc).isoformat()),
            )

    async def load_active_leases(self) -> None:
        """Load active leases from persistence on startup."""
        assignments = await self.assignment_persistence.load_assignments()

        for agent_id, assignment in assignments.items():
            task_id = assignment["task_id"]

            # Reconstruct lease from assignment
            # Normalize naive datetimes to UTC for backwards compatibility
            assigned_at = _ensure_timezone_aware(
                datetime.fromisoformat(assignment["assigned_at"])
            )
            lease_expires = _ensure_timezone_aware(
                datetime.fromisoformat(
                    assignment.get(
                        "lease_expires", datetime.now(timezone.utc).isoformat()
                    )
                )
            )
            last_renewed = _ensure_timezone_aware(
                datetime.fromisoformat(
                    assignment.get("lease_renewed_at", assignment["assigned_at"])
                )
            )

            # Restore update timestamps for cadence-based recovery
            raw_timestamps = assignment.get("update_timestamps", [])
            update_timestamps = [
                _ensure_timezone_aware(datetime.fromisoformat(ts))
                for ts in raw_timestamps
            ]

            lease = AssignmentLease(
                task_id=task_id,
                agent_id=agent_id,
                assigned_at=assigned_at,
                lease_expires=lease_expires,
                last_renewed=last_renewed,
                renewal_count=assignment.get("renewal_count", 0),
                progress_percentage=assignment.get("progress_percentage", 0),
                update_timestamps=update_timestamps,
                # Restore merge-conflict extension counter so the cap
                # survives a restart during the extension window
                # (Codex P1 on PR #350). Defaults to 0 for assignments
                # written before this field existed.
                merge_conflict_extensions=assignment.get(
                    "merge_conflict_extensions", 0
                ),
            )

            self.active_leases[task_id] = lease

        logger.info(f"Loaded {len(self.active_leases)} active leases from persistence")

    def get_lease_statistics(self) -> Dict[str, Any]:
        """Get statistics about current leases."""
        stats: Dict[str, Any] = {
            "total_active": len(self.active_leases),
            "expired": 0,
            "expiring_soon": 0,
            "high_renewal_count": 0,
            "by_status": {},
            "average_renewal_count": 0,
        }

        total_renewals = 0

        for lease in self.active_leases.values():
            # Count by status
            status = lease.status.value
            by_status_dict: Dict[str, int] = stats["by_status"]
            by_status_dict[status] = by_status_dict.get(status, 0) + 1

            # Count specific conditions
            if lease.is_expired:
                expired_count: int = stats["expired"]
                stats["expired"] = expired_count + 1
            elif lease.is_expiring_soon:
                expiring_count: int = stats["expiring_soon"]
                stats["expiring_soon"] = expiring_count + 1

            if lease.renewal_count >= self.max_renewals:
                high_renewal_count: int = stats["high_renewal_count"]
                stats["high_renewal_count"] = high_renewal_count + 1

            total_renewals += lease.renewal_count

        if self.active_leases:
            stats["average_renewal_count"] = total_renewals / len(self.active_leases)

        return stats

    def calculate_adaptive_timeout(
        self, progress: int, update_count: int, has_recent_activity: bool
    ) -> tuple[int, int]:
        """
        Calculate adaptive timeout based on task state (progressive timeout).

        Parameters
        ----------
        progress : int
            Current progress percentage (0-100)
        update_count : int
            Number of progress updates received
        has_recent_activity : bool
            Whether task shows recent activity

        Returns
        -------
        tuple[int, int]
            (lease_seconds, grace_seconds) timeout configuration

        Notes
        -----
        Progressive timeout phases (widened 2026-04-12 after experiment
        66 evidence showed agents routinely go 2+ minutes between
        progress reports during implementation bursts — the previous
        90-120s timeouts caused leases to expire mid-implementation,
        recovering in-progress tasks and reassigning them to other
        agents):

        - Phase 1 (Unproven): No updates yet → 180s + 60s = 240s total
        - Phase 2 (Working): First update → 240s + 60s = 300s total
        - Phase 3 (Proven): 25-75% progress → 300s + 60s = 360s total
        - Phase 4 (Finishing): >75% progress → 360s + 90s = 450s total

        Phase 4 was widened 2026-04-25 (snake_game-v1 cascade).
        The original "near completion = faster recovery" intuition was
        backwards: tail-phase activities (test runs, builds, commits,
        push, conflict resolution) take LONGER between progress
        reports than the middle phase, not shorter. Empirical evidence
        showed 161-215s gaps during the final 25% routinely tripped
        the old 210s window, causing recovery on tasks that were
        actually completing successfully. Phase 4 is now the longest
        window, not the shortest. The 90s grace also covers silent
        validator LLM calls (60-120s each) that run after 100% is
        reported; touch_lease is called before each attempt.

        These tolerances accommodate the observed 116-120s gap between
        progress reports during contract-first implementation work,
        plus a comfortable buffer for agents reading contract files
        and running tests locally without touching MCP tools.
        """
        # Phase 1: No updates yet - strict but tolerant of setup time
        if update_count == 0:
            return (180, 60)

        # Phase 2: First update received - moderate timeout
        if update_count == 1:
            return (240, 60)

        # Phase 4: Near completion — LONGEST window. Tail-phase activities
        # (final tests, build, commit, push, conflict resolution) routinely
        # run 2-4 minutes between progress reports; validator LLM calls add
        # another 60-120s each. Shorter windows here cause spurious recovery
        # on tasks that are actively completing.
        if progress >= 75:
            return (360, 90)

        # Phase 3: Good progress (25-75%)
        if progress >= 25:
            return (300, 60)

        # Default: working state
        return (240, 60)

    async def should_recover_expired_lease(self, lease: AssignmentLease) -> bool:
        """
        Determine if expired lease should be recovered using cadence detection.

        Compares time since last progress update against the agent's own
        median update interval * silence_multiplier. If the agent has been
        silent for longer than expected based on its established cadence,
        it's considered dead and the task should be recovered.

        Defense-in-depth guard (Simon decision 011b3fad): if the task
        is already in a terminal state (DONE/BLOCKED) on the board,
        skip recovery regardless of cadence. The lease is stale
        bookkeeping at that point — recovering a finished task only
        causes a fresh agent to redo work that's already complete
        (snake_game-v1 cascade). The lease will be cleared on the
        next monitor pass; we just don't reassign.

        Parameters
        ----------
        lease : AssignmentLease
            The expired lease to evaluate

        Returns
        -------
        bool
            True if task should be recovered, False to give more time
            or because the task is already terminal.

        Notes
        -----
        Real data from logs: median progress interval ~47s, mean ~60s.
        Default silence_multiplier is 1.5x — configurable via constructor.

        Fallback: if fewer than 2 progress updates exist (can't compute
        median), always recover since the agent has no established cadence.
        """
        # Defense-in-depth: never recover a task that's already terminal.
        # The board status flips to DONE/BLOCKED before lease bookkeeping
        # has caught up; recovering at that point reassigns finished work
        # to a fresh agent and produces duplicate output.
        if hasattr(self.kanban_client, "get_task_by_id"):
            try:
                task_obj = await self.kanban_client.get_task_by_id(lease.task_id)
                if task_obj is not None:
                    status_value = getattr(task_obj, "status", None)
                    # Accept either TaskStatus enum or string forms
                    status_str = (
                        getattr(status_value, "value", None) or str(status_value)
                    ).lower()
                    if status_str in {"done", "completed", "blocked"}:
                        # Observability counter — every hit here is a
                        # latent coordination bug elsewhere (lease
                        # bookkeeping fell behind the board status flip).
                        # The guard prevents the bad outcome (recovering
                        # a finished task), but the metric makes the
                        # underlying bug visible instead of swallowing
                        # it silently.
                        self.recoveries_skipped_terminal_status += 1
                        logger.warning(
                            f"Task {lease.task_id} is terminal "
                            f"(status={status_str}); skipping recovery. "
                            f"This indicates a stale lease — the board "
                            f"reached a terminal state but lease cleanup "
                            f"didn't fire. Counter: "
                            f"recoveries_skipped_terminal_status="
                            f"{self.recoveries_skipped_terminal_status}"
                        )
                        return False
            except Exception as e:
                # Don't block recovery on a kanban lookup hiccup — fall
                # through to the cadence check.
                logger.debug(f"Terminal-status check failed for {lease.task_id}: {e}")

        now = datetime.now(timezone.utc)
        median_interval = lease.median_update_interval

        # Not enough data to compute cadence — recover
        if median_interval is None:
            logger.info(
                f"Task {lease.task_id}: no established update cadence "
                f"(updates={len(lease.update_timestamps)}), recovering"
            )
            return True

        # Calculate silence duration from last update
        if lease.update_timestamps:
            last_update = max(lease.update_timestamps)
            silence_seconds = (now - last_update).total_seconds()
        else:
            silence_seconds = (now - lease.assigned_at).total_seconds()

        threshold = median_interval * self.silence_multiplier

        if silence_seconds > threshold:
            logger.info(
                f"Task {lease.task_id}: silence={silence_seconds:.0f}s > "
                f"threshold={threshold:.0f}s "
                f"(median={median_interval:.0f}s * "
                f"{self.silence_multiplier}x), recovering"
            )
            return True

        logger.info(
            f"Task {lease.task_id}: silence={silence_seconds:.0f}s <= "
            f"threshold={threshold:.0f}s "
            f"(median={median_interval:.0f}s * "
            f"{self.silence_multiplier}x), extending grace"
        )
        return False

    async def _create_recovery_handoff_comment(
        self, lease: AssignmentLease, recovery_info: RecoveryInfo
    ) -> None:
        """
        Post recovery information to Kanban as a comment (audit trail).

        This is the dual-write for observability. The task model holds
        the authoritative recovery info that agents use.

        Parameters
        ----------
        lease : AssignmentLease
            The lease being recovered
        recovery_info : RecoveryInfo
            The structured recovery information
        """
        # Build handoff message from recovery info
        handoff_message = (
            f"⚠️ **TASK RECOVERED FROM AGENT "
            f"{recovery_info.recovered_from_agent}**\n\n"
            f"**Recovery Details:**\n"
            f"- Recovered at: "
            f"{recovery_info.recovered_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"- Progress: {recovery_info.previous_progress}%\n"
            f"- Time spent: {recovery_info.time_spent_minutes:.1f} minutes\n"
            f"- Reason: {recovery_info.recovery_reason}\n\n"
            f"{recovery_info.instructions}"
        )

        # Add comment to Kanban board
        await self.kanban_client.add_comment(lease.task_id, handoff_message)

        logger.info(
            f"Added recovery handoff comment to Kanban for task {lease.task_id}"
        )


class LeaseMonitor:
    """Background monitor for lease expiration and recovery."""

    def __init__(
        self, lease_manager: AssignmentLeaseManager, check_interval_seconds: int = 60
    ):
        """
        Initialize the lease monitor.

        Parameters
        ----------
            lease_manager
                The lease manager instance.
            check_interval_seconds
                How often to check for expired leases.
        """
        self.lease_manager = lease_manager
        self.check_interval = check_interval_seconds
        self._running = False
        self._monitor_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start monitoring for expired leases."""
        if self._running:
            logger.warning("Lease monitor already running")
            return

        # Load existing leases first
        await self.lease_manager.load_active_leases()

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Lease monitor started (interval: {self.check_interval}s)")

    async def stop(self) -> None:
        """Stop the lease monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Lease monitor stopped")

    async def _monitor_loop(self) -> None:
        """Monitor lease expiration and recover dead agents."""
        while self._running:
            try:
                # Check for expired leases
                expired_leases = await self.lease_manager.check_expired_leases()

                # Recover expired leases (with smart checks)
                for lease in expired_leases:
                    should_recover = (
                        await self.lease_manager.should_recover_expired_lease(lease)
                    )

                    if not should_recover:
                        logger.info(
                            f"Skipping recovery for {lease.task_id} "
                            f"(smart checks indicate agent still working)"
                        )
                        continue

                    success = await self.lease_manager.recover_expired_lease(lease)
                    if success:
                        logger.info(
                            f"Successfully recovered expired lease for "
                            f"task {lease.task_id}"
                        )
                    else:
                        logger.error(
                            f"Failed to recover expired lease for task {lease.task_id}"
                        )

                # Log statistics periodically
                stats = self.lease_manager.get_lease_statistics()
                if stats["total_active"] > 0:
                    logger.info(
                        f"Lease stats: {stats['total_active']} active, "
                        f"{stats['expiring_soon']} expiring soon, "
                        f"{stats['expired']} expired"
                    )

                # Check for expiring leases and log warnings
                expiring = await self.lease_manager.get_expiring_leases()
                for lease in expiring:
                    logger.warning(
                        f"Lease expiring soon: task {lease.task_id} "
                        f"(expires in {lease.time_remaining})"
                    )

                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                logger.warning("Lease monitor cancelled")
                raise
            except Exception as e:
                logger.error(
                    f"Error in lease monitor: {e}",
                    exc_info=True,
                )
                await asyncio.sleep(self.check_interval)

        logger.warning("Lease monitor loop exited")
