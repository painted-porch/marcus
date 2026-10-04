"""
Unit tests for the shared Tow worker loop and renderer (#737, step 7).

Both workers are long-lived pull loops in the #706 sense: register
once with vendor and principal, then request, work, report, repeat,
sleeping the server's retry interval between empty polls and exiting
after a run of empty polls. The synthesize renderer is shared, since
eligibility can offer a synthesize task to either worker. No test
touches the network or a real server.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from runners.tow_worker_common import (
    WorkerConfig,
    build_verify_evidence,
    render_case_answer,
    work_loop,
)

pytestmark = pytest.mark.unit

CLAIM_INPUTS = {
    "case_id": "tow-0001",
    "excerpt": "Some good news is that this storm",
    "headline": "Storm brings relief",
    "publisher": "Boston Globe",
    "date": "12/11/2024",
    "url": "https://www.bostonglobe.com/2024/12/11/metro/storm/",
}


class FakeClient:
    """Scripted stand-in for the Inspector MCP client."""

    def __init__(self, offers: List[Optional[Dict[str, Any]]]) -> None:
        self.offers = list(offers)
        self.registrations: List[Dict[str, Any]] = []
        self.reports: List[Dict[str, Any]] = []
        self.blockers: List[Dict[str, Any]] = []

    async def register_agent(self, **kwargs: Any) -> Dict[str, Any]:
        self.registrations.append(kwargs)
        return {"success": True}

    async def request_next_task(self, agent_id: str) -> Dict[str, Any]:
        if self.offers:
            task = self.offers.pop(0)
        else:
            task = None
        if task is None:
            return {"task": None, "retry_after_seconds": 7}
        return {"task": task}

    async def report_task_progress(self, **kwargs: Any) -> Dict[str, Any]:
        self.reports.append(kwargs)
        return {"success": True}

    async def report_blocker(self, **kwargs: Any) -> Dict[str, Any]:
        self.blockers.append(kwargs)
        return {"success": True}


def _config(**overrides: Any) -> WorkerConfig:
    """Build a WorkerConfig with test defaults."""
    data: Dict[str, Any] = {
        "marcus_url": "http://localhost:4777/mcp",
        "project_id": "tow-b",
        "agent_id": "finder-fake-1",
        "vendor": "fakevendor",
        "role": "finder",
        "skills": ["tow"],
        "idle_exits": 2,
        "poll_floor": 0.0,
    }
    data.update(overrides)
    return WorkerConfig(**data)


class TestWorkLoop:
    """Test suite for the shared pull loop."""

    @pytest.mark.asyncio
    async def test_registers_works_and_exits_on_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One task gets worked; two empty polls end the session."""
        sleeps: List[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        client = FakeClient(
            offers=[
                {
                    "id": "find-1",
                    "task_type": "find",
                    "inputs": {"query": "Q", "case_id": "tow-0001"},
                },
                None,
                None,
            ]
        )

        def handle_find(task: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "headline": "H",
                "publisher": "P",
                "date": "D",
                "url": "U",
                "raw": "R",
            }

        stats = await work_loop(_config(), {"find": handle_find}, client=client)

        assert client.registrations[0]["vendor"] == "fakevendor"
        assert client.registrations[0]["principal"] == "agent"
        assert client.registrations[0]["project_id"] == "tow-b"
        assert len(client.reports) == 1
        report = client.reports[0]
        assert report["task_id"] == "find-1"
        assert report["status"] == "completed"
        assert report["evidence"]["headline"] == "H"
        assert stats["completed"] == 1
        assert sleeps == [7, 7]

    @pytest.mark.asyncio
    async def test_unhandled_task_type_reports_a_blocker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task the worker cannot do is blocked, not silently held."""

        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        client = FakeClient(
            offers=[{"id": "impl-1", "task_type": "implement"}, None, None]
        )

        await work_loop(_config(), {}, client=client)

        assert len(client.blockers) == 1
        assert client.blockers[0]["task_id"] == "impl-1"
        assert client.reports == []

    @pytest.mark.asyncio
    async def test_handler_failure_reports_a_blocker_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashing handler blocks that task and the loop goes on."""

        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        client = FakeClient(
            offers=[
                {"id": "v-1", "task_type": "verify", "inputs": {}},
                {
                    "id": "find-1",
                    "task_type": "find",
                    "inputs": {"query": "Q"},
                },
                None,
                None,
            ]
        )

        def boom(task: Dict[str, Any]) -> Dict[str, Any]:
            raise RuntimeError("fetch exploded")

        def ok(task: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "headline": "H",
                "publisher": "P",
                "date": "D",
                "url": "U",
                "raw": "R",
            }

        stats = await work_loop(_config(), {"verify": boom, "find": ok}, client=client)

        assert len(client.blockers) == 1
        assert "fetch exploded" in client.blockers[0]["blocker_description"]
        assert stats["completed"] == 1
        assert stats["blocked"] == 1


class TestRenderCaseAnswer:
    """Test suite for the shared synthesize renderer."""

    def test_verified_and_approved_renders_the_claim(self) -> None:
        """A verified, approved claim becomes the case answer."""
        inputs = {**CLAIM_INPUTS, "verdict": "verified", "decision": "approved"}

        evidence = render_case_answer(inputs)

        answer = evidence["answer"]
        assert answer["status"] == "verified/approved"
        assert answer["url"] == CLAIM_INPUTS["url"]
        assert answer["headline"] == CLAIM_INPUTS["headline"]

    def test_verified_without_sampling_renders_verified(self) -> None:
        """Unsampled cases have no decision and still verify."""
        inputs = {**CLAIM_INPUTS, "verdict": "verified"}

        evidence = render_case_answer(inputs)

        assert evidence["answer"]["status"] == "verified"

    def test_unverifiable_renders_an_honest_decline(self) -> None:
        """The decline carries the reason, not a fabricated answer."""
        inputs = {**CLAIM_INPUTS, "verdict": "unverifiable"}

        evidence = render_case_answer(inputs)

        answer = evidence["answer"]
        assert answer["status"] == "declined"
        assert "unverifiable" in answer["reason"]
        assert "url" not in answer

    def test_rejected_decision_declines_even_when_verified(self) -> None:
        """The human's rejection overrides the machine verdict."""
        inputs = {**CLAIM_INPUTS, "verdict": "verified", "decision": "rejected"}

        evidence = render_case_answer(inputs)

        assert evidence["answer"]["status"] == "declined"
        assert "rejected" in evidence["answer"]["reason"]


class TestBuildVerifyEvidence:
    """Test suite for the verify handler core."""

    def test_verdict_and_checks_come_from_the_shared_fetcher(self) -> None:
        """The verify evidence is the verdict plus the full checks."""

        class OneShotCache:
            def fetch(self, url: str) -> Any:
                from runners.tow_fetch import FetchResult

                return FetchResult(
                    url=url,
                    final_url=url,
                    status=None,
                    html="",
                    text="",
                    robots_blocked=True,
                    error="",
                    fetched_at="2026-10-04T00:00:00+00:00",
                )

        evidence = build_verify_evidence(CLAIM_INPUTS, OneShotCache())

        assert evidence["verdict"] == "unverifiable"
        assert evidence["checks"]["robots_blocked"] is True

    def test_missing_url_is_unverifiable_without_fetching(self) -> None:
        """An empty claimed URL cannot be fetched and says so."""

        class ExplodingCache:
            def fetch(self, url: str) -> Any:
                raise AssertionError("must not fetch an empty URL")

        evidence = build_verify_evidence({**CLAIM_INPUTS, "url": ""}, ExplodingCache())

        assert evidence["verdict"] == "unverifiable"
        assert evidence["checks"]["fetch_error"] == "no URL claimed"
