"""The report writer: turn a staged evaluation into a report and its claims.

The writer is the one agent in the trial. It reads a staged
evaluation directory and produces two files: a performance report in
prose, and a claims file declaring every number that report states,
with the address in the staged data where each one can be checked.

Declaring the claims is the writer's whole obligation beyond writing
well. It does not decide what counts as verified, it does not run
the check, and it is never told which cross-checks the harness will
apply; those are derived from the staged data and cannot be omitted.

The writer can be run standalone, which is how its instruction sheet
is rehearsed before any board is involved::

    python dev-tools/experiments/runners/numeric_writer.py
        --staged-dir  ~/marcus-experiments/.../case-000/eval_2026...
        --out-dir     ~/marcus-experiments/.../rehearsal/case-000
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

INSTRUCTIONS = """\
You are writing a model performance report from a completed evaluation.

The evaluation you must describe is in `staged/`. Read every file there
before you write anything.

  metrics.json    overall figures for the whole evaluation
  per_crop.csv    one row per crop
  per_record.csv  one row per evaluated record
  tiers.csv       quality buckets by error size
  quartiles.csv   breakdown by harvested area

Write exactly two files into the directory you are running in.

== 1. report.md ==

A short, honest performance report with these sections:

  Executive summary   the headline figures and what they mean
  Per-crop results    how the model did on each crop, and why they differ
  Quality tiers       how the records are distributed by error
  Field size          whether larger units are predicted better
  Conclusions         what to do next

Write for a reader who will act on it. Use the units the data gives:
different crops are reported in different units and the per_crop table
states which. Do not invent context the data does not support.

== 2. claims.json ==

EVERY number appearing in report.md must have a claim here. A number in
the report with no claim behind it is a failure of the report, so keep
the two in step as you write. Do not state a number you cannot address.

The file is a JSON object: {"claims": [ ... ]}

Each claim is an object:

  id        a short unique tag, e.g. "c1"
  value     the number exactly as the report states it
  label     what the number is, in words; it must not name a different
            row or a different unit from the one it reads
  kind      "lookup" or "derived"
  source    (lookup only) the file holding the truth
  key       (lookup only) where in that file to look
  expr      (derived only) the arithmetic, e.g. "a / b"
  inputs    (derived only) {"a": "<address>", "b": "<address>"}
  tolerance optional {"type": "abs"|"rel", "value": <number>}
            omit for an exact match

ADDRESSES

  A JSON key          source "metrics.json", key "r2_overall"
  A CSV cell          source "per_crop.csv", key "crop=maize:r2"
                      (the row where column crop is maize, column r2)
  A CSV column total  source "tiers.csv", key "sum:n_records"
  A CSV row count     source "per_record.csv", key "count:"

  Inside `inputs`, write the whole address as one string:
      "per_crop.csv:crop=maize:n_records"

WORKED EXAMPLES

  A figure read straight from a file:

    {"id": "c1", "value": 0.481, "label": "maize R2",
     "kind": "lookup", "source": "per_crop.csv", "key": "crop=maize:r2"}

  A figure you worked out yourself. State the tolerance you intend,
  because a rounded figure is checked against the exact recomputation:

    {"id": "c2", "value": 19.9, "label": "maize to cotton record ratio",
     "kind": "derived", "expr": "a / b",
     "inputs": {"a": "per_crop.csv:crop=maize:n_records",
                "b": "per_crop.csv:crop=cotton:n_records"},
     "tolerance": {"type": "abs", "value": 0.05}}

RULES

  - Every number in report.md has exactly one claim.
  - Any figure you compute, round, or combine is "derived", never
    "lookup". Rounding a value makes it derived, and it needs a
    tolerance wide enough to cover the rounding you did.
  - A label must match the row it reads. Do not put one crop's figure
    under another crop's name, or a figure's value under another
    figure's name.
  - Report what the data says, including where the model did badly.

Write report.md and claims.json now. Do not write any other file.
"""

STAGED_SUBDIR = "staged"


def build_workspace(staged_dir: Path, out_dir: Path) -> Path:
    """Lay out a clean workspace holding only the staged evaluation.

    The writer is given a copy rather than the original, so a sibling
    evaluation directory is not reachable by walking upward and the
    run cannot modify the staged data.

    Parameters
    ----------
    staged_dir : Path
        The evaluation directory to describe.
    out_dir : Path
        Where the writer works and leaves its output.

    Returns
    -------
    Path
        The workspace directory.
    """
    out_dir = out_dir.expanduser()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staged_copy = out_dir / STAGED_SUBDIR
    shutil.copytree(staged_dir.expanduser(), staged_copy)
    return out_dir


def run_writer(
    workspace: Path,
    model: Optional[str] = None,
    timeout: int = 900,
    failed_claims: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run one writing session in the workspace.

    Parameters
    ----------
    workspace : Path
        Directory holding ``staged/``; output lands here.
    model : Optional[str]
        Model override passed to the CLI.
    timeout : int
        Seconds before the session is abandoned.
    failed_claims : Optional[List[Dict[str, Any]]]
        On a retry, the claims the checker rejected. They are appended
        to the instructions so the writer knows what to fix; it is
        never told how the check is implemented.

    Returns
    -------
    Dict[str, Any]
        ``returncode``, ``seconds``, ``stdout_tail`` and ``stderr_tail``.
    """
    prompt = INSTRUCTIONS
    if failed_claims:
        lines = "\n".join(
            f"  - {c.get('claim_id', '?')}: {c.get('detail', '')}"
            for c in failed_claims
        )
        prompt += (
            "\n\nA previous attempt was rejected. These claims did not "
            "hold up:\n\n" + lines + "\n\nFix the numbers, not the "
            "wording. Rewrite report.md and claims.json in full.\n"
        )

    cmd = [
        "claude",
        "-p",
        prompt,
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


def collect_output(workspace: Path) -> Dict[str, Any]:
    """Read back what the writer produced and say what is wrong with it.

    Parameters
    ----------
    workspace : Path
        The workspace the writer ran in.

    Returns
    -------
    Dict[str, Any]
        ``report``, ``claims``, ``problems`` and ``extra_files``.
    """
    report_path = workspace / "report.md"
    claims_path = workspace / "claims.json"
    problems: List[str] = []

    report = report_path.read_text() if report_path.exists() else ""
    if not report:
        problems.append("report.md missing or empty")

    claims: List[Dict[str, Any]] = []
    if not claims_path.exists():
        problems.append("claims.json missing")
    else:
        try:
            raw = json.loads(claims_path.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"claims.json is not valid JSON: {exc}")
            raw = None
        if isinstance(raw, dict):
            claims = list(raw.get("claims") or [])
        elif isinstance(raw, list):
            claims = raw
        elif raw is not None:
            problems.append("claims.json is neither an object nor a list")

    seen = set()
    for claim in claims:
        cid = claim.get("id")
        if not cid:
            problems.append("a claim has no id")
        elif cid in seen:
            problems.append(f"duplicate claim id: {cid}")
        seen.add(cid)
        if claim.get("kind") not in ("lookup", "derived", "consistency"):
            problems.append(f"claim {cid} has kind {claim.get('kind')!r}")

    expected = {"report.md", "claims.json", STAGED_SUBDIR}
    extra = sorted(p.name for p in workspace.iterdir() if p.name not in expected)

    return {
        "report": report,
        "claims": claims,
        "problems": problems,
        "extra_files": extra,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the writer's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_writer")
    p.add_argument("--staged-dir", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=900)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Run one writing session and report what came back.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Zero when the writer produced usable output.
    """
    args = build_parser().parse_args(argv)
    workspace = build_workspace(Path(args.staged_dir), Path(args.out_dir))
    run = run_writer(workspace, model=args.model, timeout=args.timeout)
    print(f"session exit={run['returncode']} in {run['seconds']}s")
    if run["returncode"] != 0:
        print(run["stderr_tail"], file=sys.stderr)

    got = collect_output(workspace)
    print(f"report.md   {len(got['report'])} chars")
    print(f"claims.json {len(got['claims'])} claims")
    if got["extra_files"]:
        print(f"unexpected files: {got['extra_files']}")
    for problem in got["problems"]:
        print(f"  problem: {problem}")
    return 1 if got["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
