"""
Unit tests for the runner spawn-controller core (issue #595 Fix 3, chunk 3a).

These are the pure decision functions the runner daemon is built on:
- compute_spawn_count: how many ephemeral agents to spawn this cycle.
- StallWatchdog: detect a run whose task counts stop changing.
- experiment_lifecycle_state: map raw status fields to a lifecycle state.

They contain no I/O so they are unit-testable in isolation; the tmux /
HTTP glue that drives them lives in spawn_agents.py.
"""

import pytest

# conftest.py puts dev-tools/experiments/ on sys.path so `runners` imports.
from runners.spawn_controller import (
    SpawnThrashDetector,
    StallWatchdog,
    compute_spawn_count,
    experiment_lifecycle_state,
)

pytestmark = pytest.mark.unit


class TestComputeSpawnCount:
    """compute_spawn_count = max(0, min(desired - in_flight, unclaimed)).

    Issue #632: the formula's coverage variable is now ``in_flight_tasks``
    (Marcus's IN_PROGRESS count for the active layer), not the runner's
    tmux-pane count. The shape of the formula is unchanged.
    """

    def test_spawns_to_fill_the_gap(self) -> None:
        """desired 5, in_flight 2, plenty unclaimed -> spawn 3."""
        assert (
            compute_spawn_count(
                desired_agent_count=5, in_flight_tasks=2, unclaimed_tasks=5
            )
            == 3
        )

    def test_gated_by_unclaimed_tasks(self) -> None:
        """Never spawn more agents than there are claimable tasks."""
        assert (
            compute_spawn_count(
                desired_agent_count=5, in_flight_tasks=0, unclaimed_tasks=1
            )
            == 1
        )

    def test_clamped_at_zero_when_in_flight_exceeds_desired(self) -> None:
        """At a layer boundary in_flight can transiently exceed desired -> 0."""
        assert (
            compute_spawn_count(
                desired_agent_count=2, in_flight_tasks=3, unclaimed_tasks=5
            )
            == 0
        )

    def test_zero_when_no_unclaimed_work(self) -> None:
        """A vacancy with no claimable task spawns nothing (no idle agent)."""
        assert (
            compute_spawn_count(
                desired_agent_count=5, in_flight_tasks=2, unclaimed_tasks=0
            )
            == 0
        )

    def test_zero_when_fully_staffed(self) -> None:
        """in_flight == desired -> spawn nothing."""
        assert (
            compute_spawn_count(
                desired_agent_count=4, in_flight_tasks=4, unclaimed_tasks=4
            )
            == 0
        )

    def test_spawns_for_unclaimed_task_when_no_in_flight_work(self) -> None:
        """Hung-pane regression (#632).

        Pre-#632 a hung tmux pane gave ``live_agents=1`` and the formula
        decided ``spawn=0`` against an unclaimed task — the test58 stall.
        Post-#632 the runner reads ``in_flight_tasks`` from Marcus, which
        correctly reports 0 (no task is IN_PROGRESS because the hung
        agent's task already reached DONE). The formula should spawn 1
        agent for the unclaimed task.
        """
        assert (
            compute_spawn_count(
                desired_agent_count=1, in_flight_tasks=0, unclaimed_tasks=1
            )
            == 1
        )

    def test_residual_integration_hang_does_not_spawn(self) -> None:
        """Documented trade-off (#632 / #634).

        If the LAST in-flight task is the one that's hung (e.g., the
        integration verifier hanging in post-completion cleanup), the
        active layer has ``in_flight=1, unclaimed=0`` — formula spawns
        0 (correct, no other work). With ``unclaimed=1`` (hangover task
        like a README that depends on the hung task) we still spawn 0
        — because ``desired=1`` and ``in_flight=1``. This residual case
        is bug #634's responsibility (kill the hang); this fix
        deliberately does not paper over it by over-spawning.
        """
        # No other work — correct to spawn 0.
        assert (
            compute_spawn_count(
                desired_agent_count=1, in_flight_tasks=1, unclaimed_tasks=0
            )
            == 0
        )
        # Hangover task with the last in-flight slot occupied by a hang
        # — still spawn 0; #634 must resolve the underlying hang.
        assert (
            compute_spawn_count(
                desired_agent_count=1, in_flight_tasks=1, unclaimed_tasks=1
            )
            == 0
        )


class TestStallWatchdog:
    """StallWatchdog flags a run whose task counts stop changing."""

    def test_flags_stall_after_n_unchanged_polls(self) -> None:
        """The same tuple for `stall_polls` polls in a row is a stall."""
        wd = StallWatchdog(stall_polls=3)
        assert wd.update(2, 1, 0) is False  # first poll — baseline
        assert wd.update(2, 1, 0) is False  # unchanged x1
        assert wd.update(2, 1, 0) is False  # unchanged x2
        assert wd.update(2, 1, 0) is True  # unchanged x3 -> stalled

    def test_progress_resets_the_counter(self) -> None:
        """Any change to the task-count tuple resets the stall counter."""
        wd = StallWatchdog(stall_polls=2)
        wd.update(1, 2, 0)
        wd.update(1, 2, 0)  # unchanged x1
        assert wd.update(2, 1, 0) is False  # progress — reset
        assert wd.update(2, 1, 0) is False  # unchanged x1 again
        assert wd.update(2, 1, 0) is True  # unchanged x2 -> stalled

    def test_zero_stall_polls_disables_the_watchdog(self) -> None:
        """stall_polls=0 means the watchdog never fires."""
        wd = StallWatchdog(stall_polls=0)
        for _ in range(20):
            assert wd.update(0, 0, 0) is False


class TestSpawnThrashDetector:
    """
    SpawnThrashDetector flags runs that spawn agents but make no progress.

    Failure mode it catches (real incidents on test66, test71, test73):
    a BLOCKED task gates every claimable downstream task, so the runner
    keeps trying to fill the desired-agent count with ephemeral agents
    that immediately exit. The ``activity`` tally (completions plus
    logged agent work — context requests, artifacts, decisions,
    blockers) stays flat while the runner burns one agent's worth of
    cost per poll. StallWatchdog cannot catch this — the tuple it
    watches keeps flickering — and only fires after 20 minutes, by which
    point 30–50 agents have been wasted.

    The detector takes a single monotonic ``activity`` int; deciding
    *which* signals compose it lives at the call site (spawn_agents.py).
    ``in_progress`` is deliberately excluded there — it flickers 0→1→0
    on claim-and-exit churn and would mask the thrash.
    """

    def test_fires_after_n_idle_spawn_polls(self) -> None:
        """to_spawn>0 and activity flat for thrash_polls in a row -> fire."""
        det = SpawnThrashDetector(thrash_polls=3)
        # First observation is baseline only — never fires.
        assert det.observe(activity=5, to_spawn=2) is False
        # Three more polls: still spawning, still no completions.
        assert det.observe(activity=5, to_spawn=2) is False  # idle x1
        assert det.observe(activity=5, to_spawn=2) is False  # idle x2
        assert det.observe(activity=5, to_spawn=2) is True  # idle x3 -> fire

    def test_completion_resets_the_counter(self) -> None:
        """Any task completing during the streak resets thrash detection."""
        det = SpawnThrashDetector(thrash_polls=2)
        det.observe(activity=3, to_spawn=2)  # baseline
        assert det.observe(activity=3, to_spawn=2) is False  # idle x1
        # An agent finishes — progress, reset.
        assert det.observe(activity=4, to_spawn=2) is False
        assert det.observe(activity=4, to_spawn=2) is False  # idle x1 again
        assert det.observe(activity=4, to_spawn=2) is True  # idle x2 -> fire

    def test_logged_work_resets_without_a_completion(self) -> None:
        """Mid-task progress (an agent logging work) resets the streak.

        The whole point of broadening the signal from ``completed`` to a
        cumulative ``activity`` tally: a single contract task can take
        minutes to finish, and while it runs the agent claims the task
        and logs context requests / artifacts / decisions. Those bumps
        must reset the thrash counter so a healthy-but-slow run is not
        torn down before its first completion.
        """
        det = SpawnThrashDetector(thrash_polls=3)
        det.observe(activity=10, to_spawn=2)  # baseline, 0 completions yet
        assert det.observe(activity=10, to_spawn=2) is False  # idle x1
        assert det.observe(activity=10, to_spawn=2) is False  # idle x2
        # No task completed, but an agent logged an artifact: the tally
        # ticks up -> real progress -> reset, even though completed==0.
        assert det.observe(activity=11, to_spawn=2) is False
        assert det.observe(activity=11, to_spawn=2) is False  # idle x1
        assert det.observe(activity=11, to_spawn=2) is False  # idle x2
        assert det.observe(activity=11, to_spawn=2) is True  # idle x3 -> fire

    def test_zero_spawn_poll_does_not_advance_counter(self) -> None:
        """A poll with to_spawn=0 is not burning money — don't count it.

        Holding the counter (rather than resetting) preserves the
        signal: the thrash may resume next poll once unclaimed grows
        again, and we should not have to start counting over.
        """
        det = SpawnThrashDetector(thrash_polls=3)
        det.observe(activity=5, to_spawn=1)  # baseline
        assert det.observe(activity=5, to_spawn=1) is False  # idle x1
        assert det.observe(activity=5, to_spawn=1) is False  # idle x2
        # A poll where we did NOT spawn — counter held, not incremented.
        assert det.observe(activity=5, to_spawn=0) is False  # held at 2
        # Spawning resumes — one more idle-spawn poll trips the trigger.
        assert det.observe(activity=5, to_spawn=1) is True  # idle x3 -> fire

    def test_zero_thrash_polls_disables_the_detector(self) -> None:
        """thrash_polls=0 means the detector never fires."""
        det = SpawnThrashDetector(thrash_polls=0)
        for _ in range(50):
            assert det.observe(activity=0, to_spawn=10) is False

    def test_first_observation_never_fires(self) -> None:
        """No prior poll to compare against — first observation is baseline."""
        det = SpawnThrashDetector(thrash_polls=1)
        # Even with the most aggressive threshold (1), the first poll
        # cannot fire because there is no last_completed yet.
        assert det.observe(activity=0, to_spawn=99) is False

    def test_progress_after_fire_threshold_does_not_unfire(self) -> None:
        """Once thrash is reported, the runner is expected to tear down.

        We do not need the detector to keep reporting True after the
        first fire; one True is enough to trigger teardown. But if it
        is called again before teardown completes, completion should
        still reset the counter so a subsequent re-arm is possible
        (defensive, for future re-use).
        """
        det = SpawnThrashDetector(thrash_polls=2)
        det.observe(activity=0, to_spawn=1)  # baseline
        det.observe(activity=0, to_spawn=1)  # idle x1
        assert det.observe(activity=0, to_spawn=1) is True  # fire
        # Completion lands — counter resets, detector is quiet again.
        assert det.observe(activity=1, to_spawn=1) is False
        assert det.observe(activity=1, to_spawn=1) is False  # idle x1

    def test_realistic_thrash_scenario_test73(self) -> None:
        """Reproduce verify-snake-10 (test73) signature: BLOCKED dep, spawn-thrash.

        Baseline: ``activity=4``, then for the next ~10 polls the
        runner spawns 1–3 agents each cycle because Marcus reports
        ``desired>in_flight`` and ``unclaimed>0``, but every spawned
        agent receives "no task" (the only unclaimed work is gated by
        the BLOCKED merge-conflict task) and exits — logging no work, so
        the activity tally never moves. With thrash_polls=5 the detector
        should fire on the 6th observation — capping wasted spawns at ~5
        instead of letting the 20-minute stall watchdog count to 38+.
        """
        det = SpawnThrashDetector(thrash_polls=5)
        # The activity tally plateaus at 4 — exactly the test73 trace.
        observations = [
            (4, 2),  # baseline
            (4, 2),  # idle x1
            (4, 3),  # idle x2
            (4, 1),  # idle x3
            (4, 2),  # idle x4
            (4, 2),  # idle x5 -> fire here
        ]
        results = [det.observe(c, s) for c, s in observations]
        assert results == [False, False, False, False, False, True]


class TestExperimentLifecycleState:
    """experiment_lifecycle_state maps status fields to a lifecycle state."""

    def test_waiting_before_start(self) -> None:
        """experiment_started False -> waiting, regardless of is_running."""
        assert (
            experiment_lifecycle_state(
                experiment_started=False, is_running=False, seen_running=False
            )
            == "waiting"
        )

    def test_running_when_started_and_running(self) -> None:
        """started + running -> running."""
        assert (
            experiment_lifecycle_state(
                experiment_started=True, is_running=True, seen_running=False
            )
            == "running"
        )

    def test_startup_gap_is_waiting_not_finished(self) -> None:
        """started + not-running + never-seen-running -> waiting.

        Regression (#595): Marcus sets experiment_started=True before
        the monitor flips is_running=True. A poll in that gap must not
        read as 'finished' — that tore the session down with 0 agents
        spawned.
        """
        assert (
            experiment_lifecycle_state(
                experiment_started=True, is_running=False, seen_running=False
            )
            == "waiting"
        )

    def test_finished_when_was_running_then_stopped(self) -> None:
        """started + not-running, but is_running seen earlier -> finished."""
        assert (
            experiment_lifecycle_state(
                experiment_started=True, is_running=False, seen_running=True
            )
            == "finished"
        )
