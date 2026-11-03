"""
Unit tests for src.cost_tracking.cost_aggregator.

Aggregator is read-only: takes a CostStore, returns dicts shaped like the
Cato ``/api/cost/*`` response payloads documented in #409. Tests seed the
store with deterministic events and assert the aggregations are correct.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from src.cost_tracking.cost_aggregator import CostAggregator
from src.cost_tracking.cost_store import (
    CostStore,
    ModelPrice,
    Run,
    TokenEvent,
)


@pytest.fixture
def store(tmp_path: Path) -> CostStore:
    """Tmp store seeded with one default Anthropic price."""
    s = CostStore(db_path=tmp_path / "costs.db")
    s.record_price(
        ModelPrice(
            model="claude-sonnet-4-6",
            provider="anthropic",
            effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            input_per_million=3.0,
            cache_creation_per_million=3.75,
            cache_read_per_million=0.30,
            output_per_million=15.0,
            source="default",
        )
    )
    return s


@pytest.fixture
def populated_store(store: CostStore) -> CostStore:
    """Store with one experiment + 3 events covering planner + 2 workers."""
    store.record_run(
        Run(
            run_id="exp_1",
            project_id="proj_1",
            project_name="hangman",
            started_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            num_agents=2,
            total_tasks=10,
            completed_tasks=4,
        )
    )
    base_kwargs = dict(
        run_id="exp_1",
        project_id="proj_1",
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Planner: parse_prd
    store.record_event(
        TokenEvent(
            agent_id="planner",
            agent_role="planner",
            operation="parse_prd",
            input_tokens=1000,
            output_tokens=500,
            **base_kwargs,
        )
    )
    # Worker 1: turn 1
    store.record_event(
        TokenEvent(
            agent_id="agent_unicorn_1",
            agent_role="worker",
            operation="turn",
            task_id="t_1",
            session_id="s_1",
            turn_index=1,
            input_tokens=2000,
            cache_creation_tokens=1000,
            cache_read_tokens=500,
            output_tokens=300,
            **base_kwargs,
        )
    )
    # Worker 2: turn 1
    store.record_event(
        TokenEvent(
            agent_id="agent_unicorn_2",
            agent_role="worker",
            operation="turn",
            task_id="t_2",
            session_id="s_2",
            turn_index=1,
            input_tokens=3000,
            cache_creation_tokens=0,
            cache_read_tokens=2000,
            output_tokens=400,
            **base_kwargs,
        )
    )
    return store


@pytest.fixture
def agg(populated_store: CostStore) -> CostAggregator:
    """Aggregator over the populated store."""
    return CostAggregator(store=populated_store)


class TestExperimentSummary:
    """Top-level summary used by /api/cost/experiments/{id}."""

    def test_returns_metadata(self, agg: CostAggregator) -> None:
        """Summary includes project_id, name, totals from experiments table."""
        s = agg.run_summary("exp_1")
        assert s["run_id"] == "exp_1"
        assert s["project_id"] == "proj_1"
        assert s["project_name"] == "hangman"
        assert s["total_tasks"] == 10
        assert s["completed_tasks"] == 4

    def test_summary_aggregates_token_totals(self, agg: CostAggregator) -> None:
        """summary.total_tokens sums across all event types."""
        s = agg.run_summary("exp_1")
        # 1500 (planner) + 3800 (w1) + 5400 (w2) = 10700
        assert s["summary"]["total_tokens"] == 10700
        assert s["summary"]["total_events"] == 3

    def test_summary_breaks_down_by_role(self, agg: CostAggregator) -> None:
        """by_role groups planner vs worker."""
        s = agg.run_summary("exp_1")
        roles = {r["role"]: r for r in s["by_role"]}
        assert roles["planner"]["events"] == 1
        assert roles["worker"]["events"] == 2

    def test_summary_breaks_down_by_agent(self, agg: CostAggregator) -> None:
        """by_agent has one row per distinct agent_id."""
        s = agg.run_summary("exp_1")
        agents = {a["agent_id"]: a for a in s["by_agent"]}
        assert set(agents.keys()) == {"planner", "agent_unicorn_1", "agent_unicorn_2"}
        assert agents["agent_unicorn_2"]["turns"] == 1

    def test_summary_breaks_down_by_task(self, agg: CostAggregator) -> None:
        """by_task ignores rows with NULL task_id."""
        s = agg.run_summary("exp_1")
        task_ids = {t["task_id"] for t in s["by_task"]}
        assert task_ids == {"t_1", "t_2"}  # planner row excluded

    def test_summary_includes_cache_hit_rate(self, agg: CostAggregator) -> None:
        """cache_hit_rate = cache_read / (input + cache_creation + cache_read)."""
        s = agg.run_summary("exp_1")
        # totals: input=6000, cache_creation=1000, cache_read=2500
        # hit_rate = 2500 / 9500 ≈ 0.2632
        assert s["summary"]["cache_hit_rate"] == pytest.approx(2500 / 9500, rel=1e-3)

    def test_summary_returns_none_for_unknown_experiment(
        self, agg: CostAggregator
    ) -> None:
        """Querying a non-existent experiment returns None."""
        assert agg.run_summary("nonexistent") is None


class TestProjectSummary:
    """Project-scoped summary used by Cato's project-first dashboard.

    Mirrors :class:`TestExperimentSummary` but keyed on project_id.
    Marcus runs that never call start_experiment still produce token
    events; the project axis is the only universal identity.
    """

    def test_aggregates_across_all_events_in_project(self, agg: CostAggregator) -> None:
        """All planner + worker events in proj_1 roll up to one summary."""
        s = agg.project_summary("proj_1")
        assert s is not None
        assert s["project_id"] == "proj_1"
        # populated fixture has 3 events in exp_1 (proj_1)
        assert s["summary"]["total_events"] == 3
        assert s["summary"]["total_tokens"] == 10700

    def test_breaks_down_by_role(self, agg: CostAggregator) -> None:
        """by_role groups planner vs worker at project scope."""
        s = agg.project_summary("proj_1")
        assert s is not None
        roles = {r["role"]: r for r in s["by_role"]}
        assert roles["planner"]["events"] == 1
        assert roles["worker"]["events"] == 2

    def test_includes_cache_hit_rate(self, agg: CostAggregator) -> None:
        """cache_hit_rate computed at project scope."""
        s = agg.project_summary("proj_1")
        assert s is not None
        assert s["summary"]["cache_hit_rate"] == pytest.approx(2500 / 9500, rel=1e-3)

    def test_returns_none_for_project_with_no_events(self, agg: CostAggregator) -> None:
        """Unknown project_id returns None, not an empty shape."""
        assert agg.project_summary("nonexistent_project") is None

    def test_counts_events_for_unpriced_models(
        self, populated_store: CostStore
    ) -> None:
        """Events whose model isn't in model_prices still count.

        Real-world data: agents produce '<synthetic>' planner artifacts
        and local Qwen-class models that have no seed price. The old
        ``v_event_cost`` view INNER-joined to model_prices and silently
        dropped these rows — Codex P2 on PR #513. Cost falls back to
        $0 (correct: we don't know the price), but counts and tokens
        must reflect the actual events.
        """
        from datetime import datetime, timezone

        from src.cost_tracking.cost_store import TokenEvent

        # Same project, a model that is NOT in the price table.
        populated_store.record_event(
            TokenEvent(
                run_id="exp_1",
                project_id="proj_1",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="<synthetic>",
                input_tokens=100,
                output_tokens=50,
                request_id="req_synth_1",
                timestamp=datetime(2026, 5, 11, tzinfo=timezone.utc),
            )
        )
        s = CostAggregator(populated_store).project_summary("proj_1")
        assert s is not None
        # 3 priced events from the fixture + 1 unpriced = 4 total
        assert s["summary"]["total_events"] == 4
        # Tokens still reflect the new row (150 added to 10700)
        assert s["summary"]["total_tokens"] == 10700 + 150
        # cost_usd for the synthetic row is 0 (no price), so total cost
        # equals what it was before — unchanged.
        # Find the unpriced row in by_model:
        synthetic = next(
            (m for m in s["by_model"] if m["model"] == "<synthetic>"), None
        )
        assert synthetic is not None
        assert synthetic["events"] == 1
        assert synthetic["tokens"] == 150
        assert synthetic["cost_usd"] == 0.0

    def test_does_not_require_experiments_row(self, populated_store: CostStore) -> None:
        """Project summary works for projects that never called start_experiment.

        This is the whole point of project-first: Marcus's main code
        path doesn't open an MLflow experiment, but token events still
        land in token_events with a project_id. Verify the summary
        renders without any experiments-table row.
        """
        from datetime import datetime, timezone

        from src.cost_tracking.cost_store import TokenEvent

        populated_store.record_event(
            TokenEvent(
                run_id="exp_orphan",
                project_id="proj_no_exp",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
                request_id="req_orphan_1",
                timestamp=datetime(2026, 5, 11, tzinfo=timezone.utc),
            )
        )
        s = CostAggregator(populated_store).project_summary("proj_no_exp")
        assert s is not None
        assert s["summary"]["total_events"] == 1
        assert s["summary"]["runs"] == 1  # the run_id we stamped


class TestSessionTurns:
    """Per-session turn trajectory used by drill-down."""

    def test_returns_turns_in_order(self, populated_store: CostStore) -> None:
        """session_turns sorts ascending by turn_index."""
        # Add a 2nd turn for s_1
        populated_store.record_event(
            TokenEvent(
                run_id="exp_1",
                project_id="proj_1",
                agent_id="agent_unicorn_1",
                agent_role="worker",
                operation="turn",
                session_id="s_1",
                turn_index=2,
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=500,
                output_tokens=100,
            )
        )
        agg = CostAggregator(store=populated_store)
        turns = agg.session_turns("s_1")
        assert [t["turn_index"] for t in turns] == [1, 2]
        assert turns[0]["total_tokens"] == 2000 + 1000 + 500 + 300


class TestExperimentList:
    """List view used by /api/cost/experiments."""

    def test_lists_experiments_with_summary(self, populated_store: CostStore) -> None:
        """Each row has totals attached."""
        agg = CostAggregator(store=populated_store)
        rows = agg.list_runs()
        assert len(rows) == 1
        assert rows[0]["run_id"] == "exp_1"
        assert rows[0]["total_tokens"] == 10700

    def test_filter_by_project(self, populated_store: CostStore) -> None:
        """project_id filter narrows the set."""
        populated_store.record_run(
            Run(
                run_id="exp_2",
                project_id="other",
                started_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
            )
        )
        agg = CostAggregator(store=populated_store)
        rows = agg.list_runs(project_id="proj_1")
        assert {r["run_id"] for r in rows} == {"exp_1"}


class TestListProjects:
    """list_projects derives project rollups from token_events.project_id.

    Project axis is Marcus's actual identity (per CLAUDE.md GH-388 +
    the session launcher); run_id is an MLflow tracking handle and is
    intentionally not the join key here.
    """

    def test_lists_projects_with_totals(self, agg: CostAggregator) -> None:
        """One row per distinct project_id with token + cost rollups."""
        rows = agg.list_projects()
        assert len(rows) == 1
        assert rows[0]["project_id"] == "proj_1"
        assert rows[0]["events"] == 3
        assert rows[0]["total_tokens"] == 10700

    def test_attaches_project_name_when_experiment_exists(
        self, agg: CostAggregator
    ) -> None:
        """Kaia PR #33 review: project_name should plumb through the JOIN.

        The fixture experiment ``exp_1`` registered ``project_name='hangman'``;
        the project rollup should surface that label instead of leaving it
        for the dashboard to fall back to a truncated project_id.
        """
        rows = agg.list_projects()
        assert rows[0]["project_name"] == "hangman"

    def test_project_name_null_when_no_experiment_registered(
        self, populated_store: CostStore
    ) -> None:
        """A project with events but no MLflow run leaves project_name NULL.

        Many runs never call start_experiment, so the dashboard MUST handle
        NULL gracefully. This test pins the contract: NULL means "no name
        available, fall back to project_id".
        """
        populated_store.record_event(
            TokenEvent(
                run_id="exp_no_meta",
                project_id="proj_no_meta",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=1,
                output_tokens=1,
            )
        )
        agg = CostAggregator(store=populated_store)
        rows = agg.list_projects()
        nameless = next(r for r in rows if r["project_id"] == "proj_no_meta")
        assert nameless["project_name"] is None

    def test_excludes_unassigned_bucket(self, populated_store: CostStore) -> None:
        """Events tagged 'unassigned' do not appear in the project list."""
        populated_store.record_event(
            TokenEvent(
                run_id="unassigned",
                project_id="unassigned",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=10,
                output_tokens=10,
            )
        )
        agg = CostAggregator(store=populated_store)
        rows = agg.list_projects()
        assert all(r["project_id"] != "unassigned" for r in rows)

    def test_orders_by_cost_desc(self, populated_store: CostStore) -> None:
        """A cheaper project sorts below an expensive one."""
        populated_store.record_event(
            TokenEvent(
                run_id="exp_2",
                project_id="proj_cheap",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=1,
                output_tokens=1,
            )
        )
        rows = CostAggregator(store=populated_store).list_projects()
        assert rows[0]["project_id"] == "proj_1"
        assert rows[1]["project_id"] == "proj_cheap"


class TestUnassignedTotals:
    """unassigned_totals surfaces the 'no PlannerContext' gap."""

    def test_returns_zeros_when_no_unassigned_events(self, agg: CostAggregator) -> None:
        """Clean state: nothing to report."""
        totals = agg.unassigned_totals()
        assert totals["events"] == 0
        assert totals["total_cost_usd"] == 0.0

    def test_sums_unassigned_events(self, populated_store: CostStore) -> None:
        """Once we add an unassigned event, totals reflect it."""
        populated_store.record_event(
            TokenEvent(
                run_id="unassigned",
                project_id="unassigned",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=0,
            )
        )
        totals = CostAggregator(store=populated_store).unassigned_totals()
        assert totals["events"] == 1
        # 1M input * $3/M = $3
        assert totals["total_cost_usd"] == pytest.approx(3.0, rel=1e-6)


class TestCacheHitRate:
    """Cache hit rate per agent for a given experiment."""

    def test_per_agent_hit_rate(self, agg: CostAggregator) -> None:
        """cache_hit_rate is computed per agent independently."""
        rates = agg.cache_hit_rate_by_agent("exp_1")
        agent_2 = next(r for r in rates if r["agent_id"] == "agent_unicorn_2")
        # agent_2: input=3000, cache_creation=0, cache_read=2000
        # hit_rate = 2000 / 5000 = 0.40
        assert agent_2["cache_hit_rate"] == pytest.approx(0.40, rel=1e-6)


class TestRunAudit:
    """Token-attribution audit at run scope (Marcus #527)."""

    def test_reconciles_when_all_events_have_known_role(
        self, agg: CostAggregator
    ) -> None:
        """Healthy run: every event has agent_role planner or worker."""
        audit = agg.run_audit("exp_1")
        # Fixture has 3 events: 1 planner + 2 workers. All known roles.
        assert audit["total_events"] == 3
        assert audit["reconciles"] is True
        assert audit["tokens_outside_known_roles"] == 0
        assert audit["planner_events"] == 1
        assert audit["worker_events"] == 2

    def test_zero_orphans_when_all_worker_events_have_task_id(
        self, agg: CostAggregator
    ) -> None:
        """Worker rows in fixture all have task_id set."""
        audit = agg.run_audit("exp_1")
        assert audit["worker_events_without_task_id"] == 0
        assert audit["worker_events_without_agent_id"] == 0

    def test_orphan_count_when_worker_event_missing_task_id(
        self, populated_store: CostStore
    ) -> None:
        """A worker event without task_id surfaces as orphan."""
        populated_store.record_event(
            TokenEvent(
                run_id="exp_1",
                project_id="proj_1",
                agent_id="agent_unicorn_3",
                agent_role="worker",
                operation="turn",
                # No task_id — orphan.
                session_id="s_3",
                turn_index=1,
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
            )
        )
        audit = CostAggregator(store=populated_store).run_audit("exp_1")
        assert audit["worker_events_without_task_id"] == 1

    def test_reports_zero_for_unknown_run(self, agg: CostAggregator) -> None:
        """Unknown run yields a zeroed audit, not an error."""
        audit = agg.run_audit("nope")
        assert audit["total_events"] == 0
        assert audit["total_tokens"] == 0
        assert audit["reconciles"] is True


class TestProjectAudit:
    """Token-attribution audit at project scope (Marcus #527)."""

    def test_reconciles_at_project_scope(self, agg: CostAggregator) -> None:
        """Same 3 events, scoped by project_id, still reconcile."""
        audit = agg.project_audit("proj_1")
        assert audit["total_events"] == 3
        assert audit["reconciles"] is True

    def test_project_audit_isolates_to_project(
        self, populated_store: CostStore
    ) -> None:
        """Events in another project are not counted in this project's audit."""
        populated_store.record_event(
            TokenEvent(
                run_id="exp_2",
                project_id="proj_2",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=999,
                output_tokens=0,
            )
        )
        audit = CostAggregator(store=populated_store).project_audit("proj_1")
        # proj_1 still has its original 3 events; the new event belongs to proj_2.
        assert audit["total_events"] == 3


class TestByOperationSplitByRole:
    """``by_operation`` slices carry ``role`` so dashboard can separate axes (#527)."""

    def test_run_summary_by_operation_emits_role(self, agg: CostAggregator) -> None:
        """Every by_operation row has a ``role`` field set to the agent_role."""
        result = agg.run_summary("exp_1")
        assert result is not None
        for row in result["by_operation"]:
            assert "role" in row
            assert row["role"] in {"planner", "worker"}

    def test_planner_and_worker_turn_appear_separately(
        self, agg: CostAggregator
    ) -> None:
        """``operation='turn'`` only appears on worker rows, not planner."""
        result = agg.run_summary("exp_1")
        assert result is not None
        turn_rows = [r for r in result["by_operation"] if r["operation"] == "turn"]
        assert len(turn_rows) == 1, "turn should aggregate to one worker row"
        assert turn_rows[0]["role"] == "worker"

    def test_run_summary_includes_audit_field(self, agg: CostAggregator) -> None:
        """``run_summary`` now carries an inline audit dict."""
        result = agg.run_summary("exp_1")
        assert result is not None
        assert "audit" in result
        assert result["audit"]["reconciles"] is True

    def test_project_summary_includes_audit_field(self, agg: CostAggregator) -> None:
        """``project_summary`` carries the same inline audit dict."""
        result = agg.project_summary("proj_1")
        assert result is not None
        assert "audit" in result
        assert result["audit"]["reconciles"] is True


class TestByTool:
    """``by_tool`` aggregates worker rows by tool_intent (Marcus #527 Phase 2)."""

    def test_run_summary_includes_by_tool(self, populated_store: CostStore) -> None:
        """Adding tool_intent to events surfaces in summary.by_tool."""
        # Add a worker row with tool_intent='worker_edit' to the
        # existing populated fixture.
        populated_store.record_event(
            TokenEvent(
                run_id="exp_1",
                project_id="proj_1",
                agent_id="agent_unicorn_1",
                agent_role="worker",
                operation="turn",
                tool_intent="worker_edit",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="r_edit",
                input_tokens=500,
                output_tokens=200,
            )
        )
        populated_store.record_event(
            TokenEvent(
                run_id="exp_1",
                project_id="proj_1",
                agent_id="agent_unicorn_2",
                agent_role="worker",
                operation="turn",
                tool_intent="worker_marcus_call",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="r_marcus",
                input_tokens=100,
                output_tokens=50,
            )
        )

        result = CostAggregator(store=populated_store).run_summary("exp_1")
        assert result is not None
        by_tool = {r["tool_intent"]: r for r in result["by_tool"]}
        assert "worker_edit" in by_tool
        assert "worker_marcus_call" in by_tool
        assert by_tool["worker_edit"]["events"] == 1
        assert by_tool["worker_marcus_call"]["events"] == 1

    def test_by_tool_excludes_null_intent_rows(self, agg: CostAggregator) -> None:
        """Rows with tool_intent NULL (planner / pre-#527 legacy) are excluded."""
        # The fixture has no tool_intent on its rows, so by_tool is empty.
        result = agg.run_summary("exp_1")
        assert result is not None
        assert result["by_tool"] == []

    def test_project_summary_includes_by_tool(self, populated_store: CostStore) -> None:
        """project_summary also surfaces by_tool."""
        populated_store.record_event(
            TokenEvent(
                run_id="exp_1",
                project_id="proj_1",
                agent_id="agent_unicorn_1",
                agent_role="worker",
                operation="turn",
                tool_intent="worker_bash",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="r_bash",
                input_tokens=300,
                output_tokens=100,
            )
        )
        result = CostAggregator(store=populated_store).project_summary("proj_1")
        assert result is not None
        intents = {r["tool_intent"] for r in result["by_tool"]}
        assert "worker_bash" in intents


class TestTaskNamesJoin:
    """``by_task`` LEFT JOINs to task_names so the dashboard sees names
    (Marcus #530). Rows without a name entry still appear with NULL.
    """

    def test_run_summary_by_task_includes_task_name(
        self, populated_store: CostStore
    ) -> None:
        """When a task_name row exists, by_task surfaces it."""
        populated_store.record_task_name("t_1", "Implement scoring logic")
        result = CostAggregator(store=populated_store).run_summary("exp_1")
        assert result is not None
        rows = {r["task_id"]: r for r in result["by_task"]}
        assert rows["t_1"]["task_name"] == "Implement scoring logic"
        # t_2 has no name entry — LEFT JOIN returns NULL, not a row drop.
        assert rows["t_2"]["task_name"] is None

    def test_project_summary_by_task_includes_task_name(
        self, populated_store: CostStore
    ) -> None:
        """Same LEFT JOIN on the project-scoped summary."""
        populated_store.record_task_name("t_1", "Implement scoring logic")
        result = CostAggregator(store=populated_store).project_summary("proj_1")
        assert result is not None
        rows = {r["task_id"]: r for r in result["by_task"]}
        assert rows["t_1"]["task_name"] == "Implement scoring logic"


class TestByDecomposer:
    """``by_decomposer`` separates spend by decomposition strategy (#519)."""

    def test_project_summary_includes_by_decomposer(
        self, populated_store: CostStore
    ) -> None:
        """Fixture run has no decomposer label → all events bucket 'unknown'."""
        result = CostAggregator(store=populated_store).project_summary("proj_1")
        assert result is not None
        buckets = {r["decomposer"]: r for r in result["by_decomposer"]}
        # 3 events in the fixture, all on exp_1 which has decomposer=NULL.
        assert "unknown" in buckets
        assert buckets["unknown"]["events"] == 3

    def test_project_summary_by_decomposer_separates_strategies(
        self, store: CostStore
    ) -> None:
        """Two runs same project, different decomposers — slice separates them.

        Verifies the JOIN to ``runs`` and the COALESCE(NULL, 'unknown')
        bucket all wire together. Feature-based and contract-first sit
        in their own rows; totals reconcile against the underlying
        events.
        """
        # Feature-based run: 1 planner call, 1 worker turn.
        store.record_run(
            Run(
                run_id="run_fb",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 10, tzinfo=timezone.utc),
                decomposer="feature_based",
            )
        )
        store.record_event(
            TokenEvent(
                run_id="run_fb",
                project_id="proj_x",
                agent_id="planner",
                agent_role="planner",
                operation="parse_prd",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="r_fb_p",
                input_tokens=1000,
                output_tokens=500,
            )
        )
        store.record_event(
            TokenEvent(
                run_id="run_fb",
                project_id="proj_x",
                agent_id="agent_fb_1",
                agent_role="worker",
                operation="turn",
                provider="anthropic",
                model="claude-sonnet-4-6",
                request_id="r_fb_w",
                input_tokens=2000,
                output_tokens=300,
            )
        )
        # Contract-first run: 3 events to widen the spread.
        store.record_run(
            Run(
                run_id="run_cf",
                project_id="proj_x",
                project_name="x",
                started_at=datetime(2026, 5, 13, 11, tzinfo=timezone.utc),
                decomposer="contract_first",
            )
        )
        for i in range(3):
            store.record_event(
                TokenEvent(
                    run_id="run_cf",
                    project_id="proj_x",
                    agent_id="planner",
                    agent_role="planner",
                    operation="generate_contracts",
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    request_id=f"r_cf_{i}",
                    input_tokens=5000,
                    output_tokens=2000,
                )
            )

        result = CostAggregator(store=store).project_summary("proj_x")
        assert result is not None
        buckets = {r["decomposer"]: r for r in result["by_decomposer"]}

        assert buckets["feature_based"]["events"] == 2
        assert buckets["contract_first"]["events"] == 3
        assert "unknown" not in buckets

        # Reconcile against summary — every event lands in exactly one bucket.
        total_events_in_buckets = sum(b["events"] for b in buckets.values())
        assert total_events_in_buckets == result["summary"]["total_events"]


class TestRunAuditOpenStatus:
    """``run_audit`` reports run-lifecycle status (Marcus #537)."""

    def test_run_open_true_when_ended_at_is_null(
        self, populated_store: CostStore
    ) -> None:
        """Fixture run has no ended_at → audit reports run_open=True."""
        audit = CostAggregator(store=populated_store).run_audit("exp_1")
        assert audit["run_open"] is True

    def test_run_open_false_after_close(self, populated_store: CostStore) -> None:
        """Closing the run flips run_open to False."""
        populated_store.close_run(
            "exp_1", ended_at=datetime(2026, 5, 13, tzinfo=timezone.utc)
        )
        audit = CostAggregator(store=populated_store).run_audit("exp_1")
        assert audit["run_open"] is False

    def test_run_open_false_for_unknown_run(self, populated_store: CostStore) -> None:
        """Querying a nonexistent run reports run_open=False (not noise)."""
        audit = CostAggregator(store=populated_store).run_audit("nope")
        assert audit["run_open"] is False


class TestProjectAuditRunCounts:
    """``project_audit`` reports runs_total / runs_open (Marcus #537)."""

    def test_one_open_run_counted(self, populated_store: CostStore) -> None:
        """Fixture has one open run for proj_1."""
        audit = CostAggregator(store=populated_store).project_audit("proj_1")
        assert audit["runs_total"] == 1
        assert audit["runs_open"] == 1

    def test_closed_runs_lower_open_count(self, populated_store: CostStore) -> None:
        """Closing the run reduces runs_open without changing runs_total."""
        populated_store.close_run(
            "exp_1", ended_at=datetime(2026, 5, 13, tzinfo=timezone.utc)
        )
        audit = CostAggregator(store=populated_store).project_audit("proj_1")
        assert audit["runs_total"] == 1
        assert audit["runs_open"] == 0
