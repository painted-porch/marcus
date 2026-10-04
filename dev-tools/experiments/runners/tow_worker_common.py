"""Shared pull loop and handlers for the Tow workers (#737, step 7).

Both workers (the finder and the verifier) are long-lived pull-loop
sessions in the #706 sense: register once with vendor and principal,
then loop on ``request_next_task``, work the task, report with typed
evidence, and ask again, sleeping the server's ``retry_after_seconds``
between empty polls and exiting after a run of them. The board does
all the enforcement; a worker only ever sees its own assignment and
the ``inputs`` the board wrote for it.

The synthesize renderer lives here because eligibility may offer a
synthesize task to either worker, and a worker must be able to
complete whatever it pulls or the case stalls until lease expiry.
A task type a worker has no handler for is reported as a blocker,
never silently held.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runners.tow_checks import decide_verdict  # noqa: E402
from runners.tow_fetch import FetchResult  # noqa: E402

CLAIM_FIELDS = ("headline", "publisher", "date", "url")

# A handler takes the assigned task dict and returns the evidence
# payload for its completion.
Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


@dataclass
class WorkerConfig:
    """Configuration for one pull-loop worker.

    Parameters
    ----------
    marcus_url : str
        Marcus HTTP MCP endpoint.
    project_id : str
        Project to register into; one board per finder vendor.
    agent_id : str
        This worker's id on the record.
    vendor : str
        Declared vendor; eligibility keys on it.
    role : str
        Registered role label (finder, verifier).
    skills : List[str]
        Registered skills.
    idle_exits : int
        Exit after this many consecutive empty polls.
    poll_floor : float
        Minimum seconds between polls regardless of the server's
        retry interval.
    usage_log : Optional[str]
        JSONL file where per-call usage rows are appended.
    """

    marcus_url: str
    project_id: str
    agent_id: str
    vendor: str
    role: str
    skills: List[str] = field(default_factory=list)
    idle_exits: int = 10
    poll_floor: float = 1.0
    usage_log: Optional[str] = None


def render_case_answer(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Render the case answer from the accumulated chain state.

    The board's accumulative handoff means a synthesize task's
    ``inputs`` carry the claim (from the find close), the verdict
    (from the verify close), and the decision when the case was in
    the approval sample.

    Parameters
    ----------
    inputs : Dict[str, Any]
        The synthesize task's inputs.

    Returns
    -------
    Dict[str, Any]
        Evidence with the case ``answer``: the verified claim, or an
        honest decline carrying the reason.
    """
    verdict = str(inputs.get("verdict") or "")
    decision = inputs.get("decision")
    case_id = str(inputs.get("case_id") or "")

    if verdict == "verified" and decision in (None, "approved"):
        status = "verified/approved" if decision == "approved" else "verified"
        answer: Dict[str, Any] = {
            **{name: inputs.get(name, "") for name in CLAIM_FIELDS},
            "status": status,
        }
    else:
        if decision == "rejected":
            reason = "rejected by the human reviewer"
        elif verdict:
            reason = f"claim was {verdict}"
        else:
            reason = "no verdict reached"
        answer = {"status": "declined", "reason": reason}

    return {"answer": answer, "case_id": case_id}


def build_verify_evidence(inputs: Dict[str, Any], cache: Any) -> Dict[str, Any]:
    """Check the claim in a verify task's inputs through the fetcher.

    Parameters
    ----------
    inputs : Dict[str, Any]
        The verify task's inputs: the excerpt plus the claim the
        board handed off when the find task closed.
    cache : Any
        A shared fetch cache exposing ``fetch(url) -> FetchResult``.

    Returns
    -------
    Dict[str, Any]
        Evidence with ``verdict`` and the full ``checks`` record.
    """
    claim = {name: str(inputs.get(name) or "") for name in CLAIM_FIELDS}
    excerpt = str(inputs.get("excerpt") or "")

    if not claim["url"]:
        now = datetime.now(timezone.utc).isoformat()
        fetch = FetchResult(
            url="",
            final_url="",
            status=None,
            html="",
            text="",
            robots_blocked=False,
            error="no URL claimed",
            fetched_at=now,
        )
    else:
        fetch = cache.fetch(claim["url"])

    verdict, checks = decide_verdict(fetch, excerpt, claim)
    return {"verdict": verdict, "checks": checks}


def append_usage_row(usage_log: Optional[str], row: Dict[str, Any]) -> None:
    """Append one usage row to the worker's JSONL log.

    Parameters
    ----------
    usage_log : Optional[str]
        Path to the log; None disables logging.
    row : Dict[str, Any]
        The row to record (engine, tokens, cost, case).
    """
    if not usage_log:
        return
    path = Path(usage_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = {"ts": datetime.now(timezone.utc).isoformat(), **row}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(stamped, ensure_ascii=False) + "\n")


async def work_loop(
    cfg: WorkerConfig,
    handlers: Dict[str, Handler],
    client: Any = None,
) -> Dict[str, int]:
    """Run the pull loop until the board goes quiet.

    Parameters
    ----------
    cfg : WorkerConfig
        The worker's configuration.
    handlers : Dict[str, Handler]
        Evidence builders keyed by ``task_type``.
    client : Any
        Injected MCP client for tests; None constructs an Inspector
        and connects to ``cfg.marcus_url``.

    Returns
    -------
    Dict[str, int]
        Counters: completed, blocked, polls.
    """
    if client is None:
        from src.worker.inspector import Inspector

        inspector = Inspector(connection_type="http")
        async with inspector.connect(url=cfg.marcus_url):
            return await _loop_with_client(cfg, handlers, inspector)
    return await _loop_with_client(cfg, handlers, client)


async def _loop_with_client(
    cfg: WorkerConfig, handlers: Dict[str, Handler], client: Any
) -> Dict[str, int]:
    """Run the loop body with an already-connected client."""
    registration = await client.register_agent(
        agent_id=cfg.agent_id,
        name=cfg.agent_id,
        role=cfg.role,
        skills=cfg.skills,
        project_id=cfg.project_id,
        vendor=cfg.vendor,
        principal="agent",
    )
    if not registration.get("success"):
        raise RuntimeError(f"registration failed: {registration}")

    stats = {"completed": 0, "blocked": 0, "polls": 0}
    idle = 0
    while idle < cfg.idle_exits:
        response = await client.request_next_task(cfg.agent_id)
        stats["polls"] += 1
        task = response.get("task")
        if not task:
            idle += 1
            wait = max(cfg.poll_floor, float(response.get("retry_after_seconds") or 5))
            await asyncio.sleep(wait)
            continue

        idle = 0
        task_id = task.get("id", "")
        task_type = str(task.get("task_type") or "")
        handler = handlers.get(task_type)
        if handler is None:
            print(
                f"[{cfg.agent_id}] no handler for task_type={task_type!r} "
                f"({task_id}); reporting blocker"
            )
            await client.report_blocker(
                agent_id=cfg.agent_id,
                task_id=task_id,
                blocker_description=(
                    f"Worker {cfg.agent_id} has no handler for task type "
                    f"{task_type!r}."
                ),
            )
            stats["blocked"] += 1
            continue

        try:
            evidence = handler(task)
        except Exception as work_err:  # noqa: BLE001 - block, don't die
            print(f"[{cfg.agent_id}] handler failed on {task_id}: {work_err}")
            await client.report_blocker(
                agent_id=cfg.agent_id,
                task_id=task_id,
                blocker_description=(f"{task_type} handler failed: {work_err}"),
            )
            stats["blocked"] += 1
            continue

        result = await client.report_task_progress(
            agent_id=cfg.agent_id,
            task_id=task_id,
            status="completed",
            progress=100,
            message=f"{task_type} done by {cfg.agent_id}",
            evidence=evidence,
        )
        if result.get("success"):
            stats["completed"] += 1
            print(f"[{cfg.agent_id}] completed {task_type} {task_id}")
        else:
            print(
                f"[{cfg.agent_id}] completion refused for {task_id}: "
                f"{str(result)[:200]}"
            )
            stats["blocked"] += 1
    return stats
