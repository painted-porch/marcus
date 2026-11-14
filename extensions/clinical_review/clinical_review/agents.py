"""
The board-side agents: a pull loop and the three handlers it runs.

Every agent is a long-lived pull loop over MCP: register once with a vendor
and a principal, then ``request_next_task``, work the card, report with typed
evidence, ask again. The board does all the enforcement; an agent only ever
sees its own assignment and the ``inputs`` the board wrote for it.

- The **adviser** closes ``advise`` cards by reporting the recommendation it
  generated for that chain before seeding (so every arm checks the same text),
  and keeps asking for work until the board drains, so every time the kernel
  refuses it a ``verify`` card the refusal is on the record.
- The **verifier** closes ``verify`` cards with a sealed model check of the
  inputs the board handed it. An indeterminate check declines the card
  (ADR-0013 K5) instead of pretending to a verdict.
- The **approver** is a human principal; it closes ``approve`` cards,
  rejecting a chain whose verify verdict was a flag.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from marcus_toolkit.client import MarcusMCPClient

from .models import Metered
from .prompts import build_checker_messages, parse_verdict

CHECKER_MAX_TOKENS = 600

#: What a handler returns: ``("completed", evidence)`` or ``("declined", reason)``.
Outcome = tuple[str, Any]
Handler = Callable[[dict[str, Any]], Outcome]
Log = Callable[[dict[str, Any]], None]


class Board:
    """
    The MCP calls an agent makes, over the toolkit client.

    Parameters
    ----------
    client : MarcusMCPClient
        A connected client.
    log : Optional[Callable[[Dict[str, Any]], None]]
        Receives one record per call, with the reply.
    """

    def __init__(self, client: MarcusMCPClient, log: Log | None = None) -> None:
        self.client = client
        self.log = log

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call one tool, log it, and return its reply body (never None)."""
        reply = self.client.call_tool(tool, args)
        body: dict[str, Any] = (
            dict(reply)
            if reply is not None
            else {"success": False, "error": self.client.last_error}
        )
        if self.log:
            self.log(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "tool": tool,
                    "args": {k: v for k, v in args.items() if k != "evidence"},
                    "evidence": args.get("evidence"),
                    "reply": body,
                }
            )
        return body

    def register(
        self, agent_id: str, project_id: str, *, role: str, vendor: str, principal: str
    ) -> dict[str, Any]:
        """Register an agent with its vendor and principal."""
        return self.call(
            "register_agent",
            {
                "agent_id": agent_id,
                "name": agent_id,
                "role": role,
                "skills": [role],
                "project_id": project_id,
                "vendor": vendor,
                "principal": principal,
            },
        )

    def request(self, agent_id: str) -> dict[str, Any]:
        """Ask for the next card."""
        return self.call("request_next_task", {"agent_id": agent_id})

    def complete(
        self, agent_id: str, task_id: str, evidence: dict[str, Any], message: str
    ) -> dict[str, Any]:
        """Close a card with typed evidence."""
        return self.call(
            "report_task_progress",
            {
                "agent_id": agent_id,
                "task_id": task_id,
                "status": "completed",
                "progress": 100,
                "message": message,
                "evidence": evidence,
            },
        )

    def decline(self, agent_id: str, task_id: str, reason: str) -> dict[str, Any]:
        """Decline a held card terminally (K5)."""
        return self.call(
            "request_task_decline",
            {"agent_id": agent_id, "task_id": task_id, "reason": reason},
        )

    def export_case(self, chain: str) -> dict[str, Any]:
        """Export one chain's audit bundle (tasks, evidence, refusals)."""
        return self.call("export_case", {"case_id": chain})


@dataclass
class AgentConfig:
    """
    One agent's identity and loop settings.

    Parameters
    ----------
    agent_id : str
        The id on the record.
    project_id : str
        The registry project id to register into.
    role : str
        ``adviser``, ``verifier`` or ``reviewer``.
    vendor : str
        The declared vendor; K12 keys on it. Empty switches the vendor rule
        off for this agent, which arm A2 declares.
    principal : str
        ``agent`` or ``human``.
    idle_exits : int
        Exit after this many consecutive empty polls.
    poll_floor : float
        Minimum seconds between polls.
    poll_cap : float
        Maximum seconds between polls, whatever the server suggests.
    """

    agent_id: str
    project_id: str
    role: str
    vendor: str = ""
    principal: str = "agent"
    idle_exits: int = 6
    poll_floor: float = 0.2
    poll_cap: float = 2.0


@dataclass
class LoopStats:
    """Counters a loop returns."""

    completed: int = 0
    declined: int = 0
    blocked: int = 0
    polls: int = 0
    drained: bool = False
    task_ids: list[str] = field(default_factory=list)


def pull_loop(
    board: Board,
    cfg: AgentConfig,
    handlers: dict[str, Handler],
    sleep: Callable[[float], None] = time.sleep,
) -> LoopStats:
    """
    Register, then pull and work cards until the board is quiet.

    Parameters
    ----------
    board : Board
        The board.
    cfg : AgentConfig
        The agent.
    handlers : Dict[str, Handler]
        Outcome builders keyed by ``task_type``.
    sleep : Callable[[float], None]
        The sleep between empty polls; tests pass a stub.

    Returns
    -------
    LoopStats

    Raises
    ------
    RuntimeError
        When registration is refused.
    """
    registration = board.register(
        cfg.agent_id,
        cfg.project_id,
        role=cfg.role,
        vendor=cfg.vendor,
        principal=cfg.principal,
    )
    if not registration.get("success"):
        raise RuntimeError(f"{cfg.agent_id}: registration refused: {registration}")

    stats = LoopStats()
    idle = 0
    while idle < cfg.idle_exits:
        reply = board.request(cfg.agent_id)
        stats.polls += 1
        task = reply.get("task")
        if reply.get("status") == "BOARD_DRAINED":
            stats.drained = True
            break
        if not isinstance(task, dict):
            idle += 1
            wait = min(
                cfg.poll_cap,
                max(cfg.poll_floor, float(reply.get("retry_after_seconds") or 1)),
            )
            sleep(wait)
            continue
        idle = 0
        task_id = str(task.get("id") or "")
        task_type = str(task.get("task_type") or "")
        stats.task_ids.append(task_id)
        handler = handlers.get(task_type)
        if handler is None:
            board.call(
                "report_blocker",
                {
                    "agent_id": cfg.agent_id,
                    "task_id": task_id,
                    "blocker_description": (
                        f"{cfg.agent_id} has no handler for {task_type!r}"
                    ),
                },
            )
            stats.blocked += 1
            continue
        kind, payload = handler(task)
        if kind == "declined":
            result = board.decline(cfg.agent_id, task_id, str(payload))
            if result.get("success"):
                stats.declined += 1
            else:
                stats.blocked += 1
            continue
        result = board.complete(
            cfg.agent_id, task_id, dict(payload), f"{task_type} done by {cfg.agent_id}"
        )
        if result.get("success"):
            stats.completed += 1
        else:
            stats.blocked += 1
    return stats


def adviser_handler(recommendations: dict[str, dict[str, Any]]) -> Handler:
    """
    Build the adviser's handler: replay the chain's generated recommendation.

    Parameters
    ----------
    recommendations : Dict[str, Dict[str, Any]]
        Chain id to ``{"recommendation": str, "model": str}``.

    Returns
    -------
    Handler
    """

    def handle(task: dict[str, Any]) -> Outcome:
        inputs = task.get("inputs") or {}
        chain = str(inputs.get("chain_id") or "")
        row = recommendations.get(chain)
        if row is None:
            return ("declined", f"no generated recommendation for chain {chain!r}")
        return (
            "completed",
            {"recommendation": row["recommendation"], "model": row.get("model", "")},
        )

    return handle


def verifier_handler(model: Metered, arm: str) -> Handler:
    """
    Build the verifier's handler: the sealed check over the card's inputs.

    Parameters
    ----------
    model : Metered
        The checker model behind the ledger.
    arm : str
        ``A2`` or ``A3``, written on the usage row and the evidence.

    Returns
    -------
    Handler
    """

    def handle(task: dict[str, Any]) -> Outcome:
        inputs = task.get("inputs") or {}
        chain = str(inputs.get("chain_id") or "")
        messages = build_checker_messages(
            str(inputs.get("vignette") or ""),
            str(inputs.get("physician_draft") or ""),
            str(inputs.get("recommendation") or ""),
        )
        reply = model.complete(
            messages, max_tokens=CHECKER_MAX_TOKENS, extra={"arm": arm, "chain": chain}
        )
        verdict = parse_verdict(reply.text)
        if verdict["verdict"] == "indeterminate":
            reply = model.complete(
                messages,
                max_tokens=CHECKER_MAX_TOKENS,
                extra={"arm": arm, "chain": chain, "retry": 1},
            )
            verdict = parse_verdict(reply.text)
        if verdict["verdict"] == "indeterminate":
            return ("declined", "checker returned no parseable verdict after one retry")
        return (
            "completed",
            {
                "verdict": verdict["verdict"],
                "reason": verdict["reason"] or "(none given)",
                "source": verdict["source"] or "(none named)",
                "model": reply.model,
                "arm": arm,
                "raw": reply.text,
            },
        )

    return handle


def approver_handler(reviewer: str) -> Handler:
    """
    Build the human approver's handler: reject a flagged chain, approve the rest.

    Parameters
    ----------
    reviewer : str
        The human principal's id.

    Returns
    -------
    Handler
    """

    def handle(task: dict[str, Any]) -> Outcome:
        inputs = task.get("inputs") or {}
        decision = "rejected" if inputs.get("verdict") == "flag" else "approved"
        return (
            "completed",
            {
                "decision": decision,
                "approved_by": reviewer,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    return handle
