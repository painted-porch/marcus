"""Condition A: writer plus same-model self-check, no board (POC runner).

Reproduces the workflow this project is modelled on: one agent writes
the report and its claims, and a checking agent of the same model, in
the same conversation, decides whether to accept it. Up to three
rounds. No typed-claim schema enforcement, no eligibility rule, no
harness-derived cross-checks, no declined outcome, no audit record --
those are what Condition B adds, and this condition exists to measure
what happens without them.

The self-check must literally continue the writer's own session, not
start a fresh one that only reads report.md and claims.json. H3
(PROTOCOL.md) is specifically about whether a checker that can read the
writer's reasoning accepts the writer's word for it at a higher rate
than a checker that only sees the claim and the source; a fresh
session with no access to that reasoning would not test H3 at all, it
would just be a second instance of Condition B's checker with a model
attached. ``--session-id`` pins the writer's session so
``--resume`` can continue the exact same conversation for the
self-check pass.

The writer model is a parameter (``--model``), not a code edit, so the
same run can be pointed at Sonnet, Opus, or any other model the
``claude`` CLI accepts, without touching this file.

Usage::

    python dev-tools/experiments/runners/numeric_condition_a.py
        --staged-dir  ~/marcus-experiments/.../case-000/eval_...
        --out-dir     ~/marcus-experiments/.../condition-a/case-000
        --model       claude-sonnet-5
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - fixed argv, no shell, local CLI
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for _entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from runners.numeric_cost_watchdog import check_before_next_pass  # noqa: E402
from runners.numeric_writer import INSTRUCTIONS as WRITER_INSTRUCTIONS  # noqa: E402
from runners.numeric_writer import (  # noqa: E402
    build_workspace,
    collect_output,
)

#: Model used when --model is omitted. The protocol does not require
#: Opus; it is this project's default, not a requirement of the design.
DEFAULT_MODEL = "claude-opus-5"

MAX_ROUNDS = 3

SELF_CHECK_INSTRUCTIONS = """\
You just wrote report.md and claims.json in this workspace. Now check your
own work as a skeptical reviewer would, with the advantage of remembering
why you wrote what you wrote.

Re-open staged/ and verify every claim in claims.json actually matches what
the staged files say. Check derived figures by recomputing them. Check
whether any two figures in the report contradict each other (a total that
should match a sum, a ratio that should match two counts).

Decide: ACCEPT or REJECT.

If REJECT, write exactly why, naming the specific claim id and what is
wrong with it, so the next attempt knows what to fix.

Write your decision to self_check.json in this exact shape:

  {"verdict": "accept"} or
  {"verdict": "reject", "reasons": ["<claim id>: <what is wrong>", ...]}

Write self_check.json now. Do not modify report.md or claims.json.
"""

RETRY_PREFIX = "The self-check rejected your previous attempt for these reasons:\n\n"
RETRY_SUFFIX = (
    "\n\nFix the numbers, not the wording. Rewrite report.md and "
    "claims.json in full, in this same workspace."
)


def _run_claude(
    prompt: str,
    workspace: Path,
    model: str,
    timeout: int,
    session_id: Optional[str] = None,
    resume: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one claude -p invocation, optionally pinning or resuming a session.

    Parameters
    ----------
    prompt : str
        The instructions for this pass.
    workspace : Path
        Working directory; also where ``--add-dir`` points.
    model : str
        Model name passed to ``--model``.
    timeout : int
        Seconds before the session is abandoned.
    session_id : Optional[str]
        When given, pins this session under ``--session-id`` so a
        later pass can resume it by id.
    resume : Optional[str]
        When given, continues that session instead of starting fresh
        (``--resume``), so the model sees everything said so far.

    Returns
    -------
    Dict[str, Any]
        ``returncode``, ``seconds``, ``stdout_tail``, ``stderr_tail``.
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        str(workspace),
        "--model",
        model,
    ]
    if session_id:
        cmd += ["--session-id", session_id]
    if resume:
        cmd += ["--resume", resume]

    started = time.monotonic()
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "returncode": proc.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "stdout_tail": (proc.stdout or "")[-1500:],
        "stderr_tail": (proc.stderr or "")[-1500:],
    }


def read_self_check(workspace: Path) -> Dict[str, Any]:
    """Read back the self-check's verdict.

    Parameters
    ----------
    workspace : Path
        The shared writer/self-check workspace.

    Returns
    -------
    Dict[str, Any]
        The parsed verdict, or a reject with a named problem when the
        file is missing or malformed -- a self-check that produced
        nothing is not treated as an accept.
    """
    path = workspace / "self_check.json"
    if not path.exists():
        return {
            "verdict": "reject",
            "reasons": ["self_check.json missing: the checker produced no verdict"],
        }
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "verdict": "reject",
            "reasons": [f"self_check.json is not valid JSON: {exc}"],
        }
    if data.get("verdict") not in ("accept", "reject"):
        return {
            "verdict": "reject",
            "reasons": [f"self_check.json has no valid verdict: {data!r}"],
        }
    result: Dict[str, Any] = data
    return result


def run_condition_a_case(
    staged_dir: Path,
    out_dir: Path,
    model: str = DEFAULT_MODEL,
    max_rounds: int = MAX_ROUNDS,
    timeout: int = 900,
    stop_line: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one case through the full write-then-self-check loop.

    Parameters
    ----------
    staged_dir : Path
        The evaluation directory to describe.
    out_dir : Path
        Workspace for this case; holds report.md, claims.json,
        self_check.json, and a per-round transcript record.
    model : str
        Model for both the writer and the self-check pass. The
        protocol calls for the same model on both sides -- a checker
        on a different model would be testing something else.
    max_rounds : int
        How many write-then-check rounds before giving up.
    timeout : int
        Seconds per claude -p invocation.
    stop_line : Optional[float]
        An approved spend ceiling for this case, in dollars. When
        given, accumulated spend is checked via
        ``numeric_cost_watchdog.check_before_next_pass`` before every
        single ``claude -p`` launch in this loop -- the write pass
        and the self-check pass alike -- and the case halts rather
        than launching the next one once the line is reached.
        ``None`` (the default) performs no check at all, so a caller
        that does not opt in spends exactly as before.

    Returns
    -------
    Dict[str, Any]
        ``verdict`` (accept, exhausted, or stopped_cost_limit),
        ``rounds``, ``session_id``, ``round_log`` (one entry per
        attempt), and the final ``report``/``claims`` read back from
        the workspace.
    """
    workspace = build_workspace(staged_dir, out_dir)
    session_id = str(uuid.uuid4())
    round_log: List[Dict[str, Any]] = []

    def over_the_line() -> bool:
        """Check accumulated spend now, when a stop_line was given."""
        if stop_line is None:
            return False
        check = check_before_next_pass(
            transcript_paths_for(session_id), stop_line, model
        )
        return check.should_stop

    def stopped(rounds_completed: int) -> Dict[str, Any]:
        """Build the halted-run result at the current workspace state."""
        final = collect_output(workspace)
        return {
            "verdict": "stopped_cost_limit",
            "rounds": rounds_completed,
            "session_id": session_id,
            "round_log": round_log,
            "report": final["report"],
            "claims": final["claims"],
        }

    prompt = WRITER_INSTRUCTIONS
    for round_n in range(1, max_rounds + 1):
        if over_the_line():
            return stopped(round_n - 1)

        if round_n == 1:
            write_result = _run_claude(
                prompt, workspace, model, timeout, session_id=session_id
            )
        else:
            write_result = _run_claude(
                prompt, workspace, model, timeout, resume=session_id
            )
        got = collect_output(workspace)

        if over_the_line():
            return stopped(round_n - 1)

        check_result = _run_claude(
            SELF_CHECK_INSTRUCTIONS, workspace, model, timeout, resume=session_id
        )
        verdict = read_self_check(workspace)
        round_log.append(
            {
                "round": round_n,
                "write_exit": write_result["returncode"],
                "write_seconds": write_result["seconds"],
                "claims_declared": len(got["claims"]),
                "check_exit": check_result["returncode"],
                "check_seconds": check_result["seconds"],
                "verdict": verdict.get("verdict"),
                "reasons": verdict.get("reasons", []),
            }
        )

        if verdict.get("verdict") == "accept":
            final = collect_output(workspace)
            return {
                "verdict": "accept",
                "rounds": round_n,
                "session_id": session_id,
                "round_log": round_log,
                "report": final["report"],
                "claims": final["claims"],
            }

        reasons = "\n".join(f"- {r}" for r in verdict.get("reasons", []))
        prompt = RETRY_PREFIX + reasons + RETRY_SUFFIX

    final = collect_output(workspace)
    return {
        "verdict": "exhausted",
        "rounds": max_rounds,
        "session_id": session_id,
        "round_log": round_log,
        "report": final["report"],
        "claims": final["claims"],
    }


def transcript_paths_for(session_id: str) -> List[Path]:
    """Find a session's Claude Code transcript files, for cost accounting.

    Parameters
    ----------
    session_id : str
        The pinned session id this run used.

    Returns
    -------
    List[Path]
        Matching transcript files under ``~/.claude/projects/``.
    """
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return []
    pattern = re.compile(re.escape(session_id))
    return [p for p in base.glob("*/*.jsonl") if pattern.search(p.name)]


def build_parser() -> argparse.ArgumentParser:
    """Build condition A's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_condition_a")
    p.add_argument("--staged-dir", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-rounds", type=int, default=MAX_ROUNDS)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument(
        "--stop-line",
        type=float,
        default=None,
        help="Approved spend ceiling in dollars; omit to run unchecked.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Run one case and print the outcome.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Zero when the case was accepted.
    """
    args = build_parser().parse_args(argv)
    outcome = run_condition_a_case(
        staged_dir=Path(args.staged_dir),
        out_dir=Path(args.out_dir),
        model=args.model,
        max_rounds=args.max_rounds,
        timeout=args.timeout,
        stop_line=args.stop_line,
    )
    print(f"model: {args.model}")
    print(f"verdict: {outcome['verdict']}")
    print(f"rounds: {outcome['rounds']}")
    for entry in outcome["round_log"]:
        print(
            f"  round {entry['round']}: write={entry['write_exit']} "
            f"({entry['write_seconds']}s) claims={entry['claims_declared']} "
            f"check={entry['check_exit']} ({entry['check_seconds']}s) "
            f"-> {entry['verdict']}"
        )
    return 0 if outcome["verdict"] == "accept" else 1


if __name__ == "__main__":
    sys.exit(main())
