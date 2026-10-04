"""Approvals CLI: the smallest human interface to the board (issue #737).

An approve task is a task only a person can complete. This command is
that person's hands: it registers the reviewer as a ``human``
principal, pulls open approve tasks through the SAME MCP tool path
agents use (``register_agent``, ``request_next_task``,
``report_task_progress``), shows each one, and completes it with the
decision recorded as typed evidence. Eligibility guarantees a human
principal is offered approve tasks and nothing else, and the approval
gate guarantees nobody else can complete one.

Usage::

    python -m src.cli.approvals --project <project_id> [--reviewer larry]
    python -m src.cli.approvals --project <project_id> --decide approved

Interactive by default: each offered approval prints and the reviewer
types a (approve), r (reject), or q (quit). With ``--decide`` every
offered approval receives that decision without prompting, which is
what the hand-seeded end-to-end case and the pilot's sampled
approvals use when the human has pre-reviewed out of band.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.worker.inspector import Inspector

VALID_DECISIONS = ("approved", "rejected")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the approvals CLI.

    Returns
    -------
    argparse.ArgumentParser
        Parser with url, reviewer, name, project, and decide options.
    """
    parser = argparse.ArgumentParser(
        prog="approvals",
        description=(
            "List open approve tasks on the Marcus board and complete "
            "them as a human principal (issue #737)."
        ),
    )
    parser.add_argument(
        "--url",
        default="http://localhost:4298/mcp",
        help="Marcus HTTP MCP endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--reviewer",
        default="reviewer",
        help="Reviewer id to register as a human principal " "(default: %(default)s)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Display name for the reviewer (default: the reviewer id)",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project id to register into (required; GH-388 scoping)",
    )
    parser.add_argument(
        "--decide",
        choices=list(VALID_DECISIONS),
        default=None,
        help=(
            "Apply this decision to every offered approval without "
            "prompting (for scripted runs where the review happened "
            "out of band)"
        ),
    )
    return parser


def _decision_evidence(reviewer: str, decision: str) -> Dict[str, Any]:
    """Build the typed evidence payload for an approval decision.

    Parameters
    ----------
    reviewer : str
        The human principal completing the approve task.
    decision : str
        "approved" or "rejected".

    Returns
    -------
    Dict[str, Any]
        Evidence with the decision, the reviewer, and a UTC timestamp.

    Raises
    ------
    ValueError
        If the decision is not one of the two valid outcomes.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"Unknown decision {decision!r}; valid: {', '.join(VALID_DECISIONS)}"
        )
    return {
        "decision": decision,
        "approved_by": reviewer,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


def _print_task(task: Dict[str, Any]) -> None:
    """Print one offered approve task for the reviewer to read.

    Parameters
    ----------
    task : Dict[str, Any]
        The task dict returned by request_next_task.
    """
    print("\n" + "=" * 60)
    print(f"APPROVAL: {task.get('name', '(unnamed)')}")
    print(f"task id:  {task.get('id', '?')}")
    description = task.get("description") or ""
    if description:
        print(f"\n{description}")
    inputs = task.get("inputs")
    if inputs:
        print("\nWhat the board holds for this approval:")
        print(json.dumps(inputs, indent=2, default=str))
    print("=" * 60)


def _prompt_decision() -> Optional[str]:
    """Ask the reviewer for a decision on the printed approval.

    Returns
    -------
    Optional[str]
        "approved", "rejected", or None to quit.
    """
    while True:
        answer = input("[a]pprove / [r]eject / [q]uit: ").strip().lower()
        if answer in ("a", "approve", "approved"):
            return "approved"
        if answer in ("r", "reject", "rejected"):
            return "rejected"
        if answer in ("q", "quit"):
            return None
        print("Please answer a, r, or q.")


async def run(args: argparse.Namespace) -> int:
    """Register as a human principal and work the open approvals.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    int
        Process exit code: 0 on a clean drain or quit, 1 on a failed
        registration or completion.
    """
    client = Inspector(connection_type="http")
    async with client.connect(url=args.url):
        registration = await client.register_agent(
            agent_id=args.reviewer,
            name=args.name or args.reviewer,
            role="reviewer",
            skills=[],
            project_id=args.project,
            principal="human",
        )
        if not registration.get("success"):
            print(
                f"Registration failed: {registration.get('error', registration)}",
                file=sys.stderr,
            )
            return 1

        completed = 0
        while True:
            response = await client.request_next_task(args.reviewer)
            task = response.get("task")
            if not task:
                print(f"No open approvals. Completed {completed} this session.")
                return 0

            _print_task(task)
            decision = args.decide or _prompt_decision()
            if decision is None:
                print(
                    "Quitting; the shown approval stays leased to you "
                    "until the lease expires."
                )
                return 0

            result = await client.report_task_progress(
                agent_id=args.reviewer,
                task_id=task["id"],
                status="completed",
                progress=100,
                message=f"{decision} by {args.reviewer}",
                evidence=_decision_evidence(args.reviewer, decision),
            )
            if not result.get("success"):
                print(
                    f"Completion failed: {result.get('error', result)}",
                    file=sys.stderr,
                )
                return 1
            completed += 1
            print(f"Recorded: {decision} ({task['id']}).")


def main() -> None:
    """Parse arguments and run the approvals loop."""
    args = build_parser().parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
