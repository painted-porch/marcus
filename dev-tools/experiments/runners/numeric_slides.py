"""The slide worker: assemble a deck from accepted claims only.

This is the step that replaces Chris's third loop, which re-audits
numbers once they reach the slides. Re-auditing is a second
independent chance to miss something, and it can pass a figure the
report verifier already rejected. Here the slide agent receives only
the claims that already verified, nothing else, so it has no figure
available to it that was not already checked. Verifying the deck is
then a mapping problem, not a second numeric audit:
:func:`runners.numeric_claims.check_slide_spec` resolves it with no
model call.

Usage::

    python dev-tools/experiments/runners/numeric_slides.py \
        --verify-workspace ~/marcus-experiments/.../rehearsal/case-003 \
        --out-dir ~/marcus-experiments/.../rehearsal/case-003-slides
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 - fixed argv, no shell, local CLI
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.numeric_claims import (  # noqa: E402
    VERDICT_VERIFIED,
    CheckResult,
    check_slide_spec,
)
from runners.numeric_verify import verify_workspace  # noqa: E402

ACCEPTED_SUBDIR = "accepted_claims.json"

INSTRUCTIONS = """\
You are building a slide deck's data from a set of already-approved figures.

The file `accepted_claims.json` in this directory lists every number you
may use. Each entry has an id, a value, and a label saying what it is.

You have no other source of numbers. Do not compute anything new, do not
round further, do not look anywhere else. If a figure you would want for
a good slide is not in the list, leave it out of the deck rather than
inventing it.

Write exactly one file: slides.json

Shape:

  {"slides": [
    {"title": "<slide heading>",
     "cells": [
       {"value": "<the number exactly as the claim states it>",
        "claim": "<the claim's id>",
        "label": "<the claim's own label, unchanged>"},
       ...
     ]}
  ]}

Group related claims onto the same slide (e.g. all per-crop figures
together). Every cell's "claim" must be an id from accepted_claims.json,
its "value" must equal that claim's value exactly, and its "label" must
be that claim's own label, not a paraphrase. A cell with no claim id, or
a claim id not in the list, is a defect in your output.

Write slides.json now. Do not write any other file.
"""


def accepted_claims(outcome: Dict[str, Any]) -> Dict[str, CheckResult]:
    """Filter a verify outcome down to the claims that verified.

    This is the only route by which a claim reaches the slide worker.
    A claim that failed, was unresolvable, or was never declared does
    not appear here, so it cannot reach the deck either.

    Parameters
    ----------
    outcome : Dict[str, Any]
        Output of :func:`runners.numeric_verify.verify_workspace`.

    Returns
    -------
    Dict[str, CheckResult]
        Accepted claims keyed by id.
    """
    return {
        r.claim_id: r
        for r in outcome["results"]
        if r.verdict == VERDICT_VERIFIED and not r.claim_id.startswith("required:")
    }


def build_slide_workspace(outcome: Dict[str, Any], out_dir: Path) -> Path:
    """Lay out a workspace holding only the accepted claims.

    The slide agent is given a copy containing nothing but the
    accepted set: not the staged data, not the report, not the
    failed or required-check claims. It cannot look anywhere else
    for a number, because there is nowhere else to look.

    Parameters
    ----------
    outcome : Dict[str, Any]
        Output of :func:`runners.numeric_verify.verify_workspace`.
    out_dir : Path
        Where the slide agent works and leaves its output.

    Returns
    -------
    Path
        The workspace directory.
    """
    out_dir = out_dir.expanduser()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    accepted = accepted_claims(outcome)
    payload = [
        {"id": cid, "value": r.value, "label": r.label}
        for cid, r in sorted(accepted.items())
    ]
    (out_dir / ACCEPTED_SUBDIR).write_text(json.dumps(payload, indent=2))
    return out_dir


def run_slide_worker(
    workspace: Path, model: Optional[str] = None, timeout: int = 600
) -> Dict[str, Any]:
    """Run one slide-building session in the workspace.

    Parameters
    ----------
    workspace : Path
        Directory holding ``accepted_claims.json``; output lands here.
    model : Optional[str]
        Model override passed to the CLI.
    timeout : int
        Seconds before the session is abandoned.

    Returns
    -------
    Dict[str, Any]
        ``returncode``, ``seconds``, ``stdout_tail`` and ``stderr_tail``.
    """
    cmd = [
        "claude",
        "-p",
        INSTRUCTIONS,
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        str(workspace),
    ]
    if model:
        cmd += ["--model", model]

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


def load_slide_spec(workspace: Path) -> Dict[str, Any]:
    """Read back the slide spec the worker produced.

    Parameters
    ----------
    workspace : Path
        The slide worker's workspace.

    Returns
    -------
    Dict[str, Any]
        The parsed spec, or ``{"slides": []}`` with a problem noted
        when the file is missing or malformed.
    """
    path = workspace / "slides.json"
    if not path.exists():
        return {"slides": [], "_problem": "slides.json missing"}
    try:
        spec = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return {"slides": [], "_problem": f"slides.json is not valid JSON: {exc}"}
    if not isinstance(spec, dict) or "slides" not in spec:
        return {"slides": [], "_problem": "slides.json has no 'slides' key"}
    slides = spec["slides"]
    if not isinstance(slides, list) or not all(isinstance(s, dict) for s in slides):
        return {
            "slides": [],
            "_problem": "slides.json's 'slides' is not a list of objects",
        }
    for slide in slides:
        cells = slide.get("cells", [])
        if not isinstance(cells, list) or not all(isinstance(c, dict) for c in cells):
            return {
                "slides": [],
                "_problem": "a slide's 'cells' is not a list of objects",
            }
    return spec


def verify_slide_workspace(
    slide_workspace: Path,
    accepted: Dict[str, CheckResult],
    sources: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check a slide spec against the claims it was allowed to use.

    This calls no model: every check is a look-up against the
    accepted set, which is why the slide check does not re-introduce
    the numeric-audit problem it replaces.

    Parameters
    ----------
    slide_workspace : Path
        Directory holding ``slides.json``.
    accepted : Dict[str, CheckResult]
        The claims the slide worker was given, from
        :func:`accepted_claims`.
    sources : Optional[Dict[str, Any]]
        Output of :func:`runners.numeric_claims.load_sources`. Passed
        straight through to :func:`check_slide_spec`, so a slide
        title bound to one accepted claim is also checked against
        that claim's own row provenance, not just whether the figure
        happens to match some accepted value on the slide.

    Returns
    -------
    Dict[str, Any]
        ``verdict``, the list of ``problems`` found (empty means
        clean), and the parsed ``spec``.
    """
    spec = load_slide_spec(slide_workspace)
    problems: List[str] = []
    if "_problem" in spec:
        problems.append(spec["_problem"])

    labels = {cid: r.label for cid, r in accepted.items()}
    problems.extend(check_slide_spec(spec, accepted, labels, sources=sources))

    cells_total = sum(len(s.get("cells", [])) for s in spec.get("slides", []))
    return {
        "verdict": VERDICT_VERIFIED if not problems else "failed",
        "problems": problems,
        "spec": spec,
        "cells_total": cells_total,
        "accepted_available": len(accepted),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the slide worker's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_slides")
    p.add_argument("--verify-workspace", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=600)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Build and check one slide deck end to end.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Zero when the deck verified clean.
    """
    args = build_parser().parse_args(argv)
    outcome = verify_workspace(Path(args.verify_workspace))
    if outcome["verdict"] != VERDICT_VERIFIED:
        # H4's guarantee is that a deck built only from accepted
        # claims cannot carry a figure the report verifier rejected.
        # A report that failed -- even on a harness-required check no
        # individual slide claim is about -- was never accepted as a
        # whole, so there is nothing to build a deck from.
        print(f"report verdict: {outcome['verdict'].upper()}, refusing to build slides")
        for problem in outcome["failed_claims"]:
            print(f"  problem: {problem['claim_id']}: {problem['detail']}")
        return 1

    accepted = accepted_claims(outcome)
    print(f"accepted claims available: {len(accepted)}")

    workspace = build_slide_workspace(outcome, Path(args.out_dir))
    run = run_slide_worker(workspace, model=args.model, timeout=args.timeout)
    print(f"session exit={run['returncode']} in {run['seconds']}s")
    if run["returncode"] != 0:
        print(run["stderr_tail"], file=sys.stderr)

    checked = verify_slide_workspace(workspace, accepted, sources=outcome["sources"])
    print(f"slide verdict: {checked['verdict'].upper()}")
    print(f"cells on deck: {checked['cells_total']}")
    for problem in checked["problems"]:
        print(f"  problem: {problem}")
    return 0 if checked["verdict"] == VERDICT_VERIFIED else 1


if __name__ == "__main__":
    sys.exit(main())
