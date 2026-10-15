"""
Unit tests for the audit-bundle export (#737, step 8).

``export_case`` turns one case into its complete, third-party-
readable record: every task with its type, timestamps, and the
principal who worked it; the evidence payloads (``raw`` included,
this is the auditors' view); the approval decision; the eligibility
refusals; identity changes for the agents involved; and the cost
rows keyed by agent. The bundle is a query over what the board
already holds, which is the system-of-record thesis in one function.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from src.core.models import Priority, Task, TaskStatus, WorkerStatus
from src.marcus_mcp.tools.audit_tools import export_case

pytestmark = pytest.mark.unit

CASE = "tow-0001"
RAW = "FULL ENGINE RESPONSE, auditors only"


def _task(
    task_id: str,
    task_type: str,
    assigned_to: Optional[str],
    case_id: str = CASE,
    evidence: Optional[Dict[str, Any]] = None,
    dependencies: Optional[List[str]] = None,
) -> Task:
    """Build one closed case task with evidence on the record."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=f"{task_type} for {case_id}",
        description="",
        status=TaskStatus.DONE,
        priority=Priority.HIGH,
        assigned_to=assigned_to,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=0.1,
        dependencies=dependencies or [],
        labels=["tow", f"case:{case_id}"],
        source_context={"evidence": evidence} if evidence else None,
        task_type=task_type,
    )


def _worker(worker_id: str, vendor: str = "", principal: str = "agent") -> WorkerStatus:
    """Build a registered worker."""
    return WorkerStatus(
        worker_id=worker_id,
        name=worker_id,
        role="worker",
        email=None,
        current_tasks=[],
        completed_tasks_count=0,
        capacity=40,
        skills=[],
        availability={},
        vendor=vendor,
        principal=principal,
    )


def _closed_case_state() -> Mock:
    """The worked case from the design, closed, on a Mock state."""
    find = _task(
        "t-find",
        "find",
        "finder-pplx-1",
        evidence={
            "headline": "H",
            "publisher": "P",
            "date": "2025-03-06",
            "url": "https://example.org/a",
            "raw": RAW,
        },
    )
    verify = _task(
        "t-verify",
        "verify",
        "verifier-gem-1",
        evidence={"verdict": "verified", "checks": {"excerpt_found": True}},
        dependencies=["t-find"],
    )
    approve = _task(
        "t-approve",
        "approve",
        "larry",
        evidence={"decision": "approved", "approved_by": "larry"},
        dependencies=["t-verify"],
    )
    synthesize = _task(
        "t-synth",
        "synthesize",
        "verifier-gem-1",
        evidence={
            "answer": {"status": "verified/approved", "url": "https://example.org/a"}
        },
        dependencies=["t-approve"],
    )
    other_case = _task("t-other", "find", "finder-pplx-1", case_id="tow-0099")

    state = Mock()
    state.project_tasks = [find, verify, approve, synthesize, other_case]
    state.agent_status = {
        "finder-pplx-1": _worker("finder-pplx-1", vendor="perplexity"),
        "verifier-gem-1": _worker("verifier-gem-1", vendor="google"),
        "larry": _worker("larry", principal="human"),
    }
    state.eligibility_refusals = [
        {
            "agent_id": "finder-pplx-1",
            "task_id": "t-verify",
            "task_type": "verify",
            "reason": "author of the claim this verify task checks",
            "count": 3,
            "first_refused_at": "2026-10-04T14:02:30+00:00",
            "last_refused_at": "2026-10-04T14:03:30+00:00",
        },
        {
            "agent_id": "someone-else",
            "task_id": "unrelated-task",
            "task_type": "verify",
            "reason": "same vendor as the claim's author (perplexity)",
            "count": 1,
            "first_refused_at": "2026-10-04T14:02:31+00:00",
            "last_refused_at": "2026-10-04T14:02:31+00:00",
        },
    ]
    state.identity_changes = [
        {
            "agent_id": "finder-pplx-1",
            "old_vendor": "perplexity",
            "new_vendor": "google",
            "old_principal": "agent",
            "new_principal": "agent",
            "timestamp": "2026-10-04T15:00:00+00:00",
        },
        {
            "agent_id": "stranger",
            "old_vendor": "a",
            "new_vendor": "b",
            "old_principal": "agent",
            "new_principal": "agent",
            "timestamp": "2026-10-04T15:00:00+00:00",
        },
    ]
    state.cost_store = None
    return state


class TestExportCase:
    """Test suite for export_case."""

    @pytest.mark.asyncio
    async def test_every_task_exports_with_timestamp_and_principal(self) -> None:
        """The issue's acceptance line, verbatim."""
        bundle = await export_case(CASE, _closed_case_state())

        assert bundle["case_id"] == CASE
        assert len(bundle["tasks"]) == 4
        for task in bundle["tasks"]:
            assert task["created_at"]
            assert task["updated_at"]
            assert task["worked_by"]["agent_id"]
            assert task["worked_by"]["principal"] in ("agent", "human")

    @pytest.mark.asyncio
    async def test_tasks_from_other_cases_are_excluded(self) -> None:
        """The bundle is one case, not the whole board."""
        bundle = await export_case(CASE, _closed_case_state())

        assert "t-other" not in [t["task_id"] for t in bundle["tasks"]]

    @pytest.mark.asyncio
    async def test_evidence_is_kept_raw_included(self) -> None:
        """The audit bundle is the one view that keeps raw."""
        bundle = await export_case(CASE, _closed_case_state())

        find = next(t for t in bundle["tasks"] if t["task_type"] == "find")
        assert find["evidence"]["raw"] == RAW

    @pytest.mark.asyncio
    async def test_approval_section_names_the_human(self) -> None:
        """The approval is first-class: decision, name, principal."""
        bundle = await export_case(CASE, _closed_case_state())

        assert len(bundle["approvals"]) == 1
        approval = bundle["approvals"][0]
        assert approval["decision"] == "approved"
        assert approval["approved_by"] == "larry"
        assert approval["principal"] == "human"

    @pytest.mark.asyncio
    async def test_refusals_are_filtered_to_the_case(self) -> None:
        """Only this case's refusals appear in its bundle."""
        bundle = await export_case(CASE, _closed_case_state())

        assert len(bundle["refusals"]) == 1
        assert "author" in bundle["refusals"][0]["reason"]

    @pytest.mark.asyncio
    async def test_identity_changes_cover_only_involved_agents(self) -> None:
        """Identity churn is shown for the case's agents, not strangers."""
        bundle = await export_case(CASE, _closed_case_state())

        agents = {c["agent_id"] for c in bundle["identity_changes"]}
        assert agents == {"finder-pplx-1"}

    @pytest.mark.asyncio
    async def test_case_summary_carries_outcome_and_window(self) -> None:
        """The summary answers: what happened, when, final answer."""
        bundle = await export_case(CASE, _closed_case_state())

        summary = bundle["summary"]
        assert summary["status"] == "closed"
        assert summary["opened_at"] and summary["closed_at"]
        assert summary["answer"]["status"] == "verified/approved"

    @pytest.mark.asyncio
    async def test_unknown_case_returns_an_empty_bundle(self) -> None:
        """A case id with no tasks is reported, not an exception."""
        bundle = await export_case("tow-9999", _closed_case_state())

        assert bundle["tasks"] == []
        assert bundle["summary"]["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_costs_come_from_the_store_keyed_by_agent(
        self, tmp_path: Path
    ) -> None:
        """Ingested worker usage rows surface in the bundle."""
        from src.cost_tracking.cost_store import CostStore
        from src.cost_tracking.worker_usage_ingester import ingest_usage_log

        state = _closed_case_state()
        store = CostStore(db_path=tmp_path / "costs.db")
        log = tmp_path / "usage.jsonl"
        log.write_text(
            json.dumps(
                {
                    "ts": "2026-10-04T12:00:00+00:00",
                    "agent_id": "finder-pplx-1",
                    "engine": "perplexity",
                    "model": "sonar",
                    "case_id": CASE,
                    "task_id": "t-find",
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "cost_usd": 0.0002,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ingest_usage_log(store, log, run_id="r", project_id="p")
        state.cost_store = store

        bundle = await export_case(CASE, state)

        assert len(bundle["costs"]) == 1
        cost = bundle["costs"][0]
        assert cost["agent_id"] == "finder-pplx-1"
        assert cost["task_id"] == "t-find"
        assert cost["input_tokens"] == 120
