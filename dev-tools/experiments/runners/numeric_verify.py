"""The verifier: check a written report against the evaluation it describes.

This is the checking half of the trial, and it calls no model. It
loads the staged evaluation, loads the claims the writer declared,
adds the cross-checks the harness requires whatever the writer said,
and returns one verdict per claim plus one for every figure in the
report text that no claim covers.

It never sees the writer's reasoning. It is handed the report, the
claims and the staged files, and nothing else, so it checks the data
rather than the writer's account of the data.

Standalone, for scoring a rehearsal::

    python dev-tools/experiments/runners/numeric_verify.py
        --workspace ~/marcus-experiments/.../rehearsal/case-003
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.numeric_claims import (  # noqa: E402
    VERDICT_FAILED,
    VERDICT_UNCOVERED,
    VERDICT_UNRESOLVABLE,
    VERDICT_VERIFIED,
    CheckResult,
    Claim,
    check_report,
    extract_numbers,
    load_sources,
)

STAGED_SUBDIR = "staged"


def verify_workspace(workspace: Path) -> Dict[str, Any]:
    """Check one written report against its staged evaluation.

    Parameters
    ----------
    workspace : Path
        Directory holding ``staged/``, ``report.md`` and
        ``claims.json``.

    Returns
    -------
    Dict[str, Any]
        ``verdict`` (verified or failed), the per-claim ``results``,
        the ``failed_claims`` a retry would be handed, and the
        ``summary`` counts the grader reads.
    """
    workspace = Path(workspace).expanduser()
    staged = workspace / STAGED_SUBDIR
    report_path = workspace / "report.md"
    claims_path = workspace / "claims.json"

    sources = load_sources(staged)
    report_text = report_path.read_text() if report_path.exists() else ""

    declared: List[Claim] = []
    parse_error: Optional[str] = None
    if not report_path.exists():
        parse_error = "report.md is missing"
    elif not report_text.strip():
        parse_error = "report.md is empty"

    if claims_path.exists():
        try:
            raw = json.loads(claims_path.read_text())
            items = raw.get("claims", []) if isinstance(raw, dict) else raw
            declared = [Claim.from_dict(item) for item in items]
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            parse_error = parse_error or f"claims.json could not be read: {exc}"
    else:
        parse_error = parse_error or "claims.json is missing"

    results: List[CheckResult] = check_report(
        declared, sources, report_text=report_text if report_path.exists() else None
    )

    counts = Counter(r.verdict for r in results)
    figures = extract_numbers(report_text)
    covered = len(figures) - counts.get(VERDICT_UNCOVERED, 0)
    coverage = (covered / len(figures)) if figures else 1.0

    kinds = Counter(c.kind for c in declared)

    bad = [
        r
        for r in results
        if r.verdict in (VERDICT_FAILED, VERDICT_UNRESOLVABLE, VERDICT_UNCOVERED)
    ]

    return {
        "verdict": VERDICT_VERIFIED if not bad and not parse_error else VERDICT_FAILED,
        "parse_error": parse_error,
        "results": results,
        "failed_claims": [{"claim_id": r.claim_id, "detail": r.detail} for r in bad],
        "summary": {
            "claims_declared": len(declared),
            "claims_required": len(results)
            - len(declared)
            - counts.get(VERDICT_UNCOVERED, 0),
            "kinds": dict(kinds),
            "figures_in_text": len(figures),
            "coverage": round(coverage, 3),
            "verified": counts.get(VERDICT_VERIFIED, 0),
            "failed": counts.get(VERDICT_FAILED, 0),
            "unresolvable": counts.get(VERDICT_UNRESOLVABLE, 0),
            "uncovered": counts.get(VERDICT_UNCOVERED, 0),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the verifier's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_verify")
    p.add_argument("--workspace", required=True)
    p.add_argument("--show", type=int, default=25)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Check one workspace and print the verdict.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Zero when the report verified.
    """
    args = build_parser().parse_args(argv)
    outcome = verify_workspace(Path(args.workspace))
    s = outcome["summary"]

    if outcome["parse_error"]:
        print(f"PARSE ERROR: {outcome['parse_error']}")

    print(f"verdict           {outcome['verdict'].upper()}")
    print(f"claims declared   {s['claims_declared']}  kinds={s['kinds']}")
    print(f"figures in text   {s['figures_in_text']}")
    print(f"coverage          {s['coverage']:.1%}")
    print(
        f"verdicts          verified={s['verified']} failed={s['failed']} "
        f"unresolvable={s['unresolvable']} uncovered={s['uncovered']}"
    )

    bad = outcome["failed_claims"]
    if bad:
        print(f"\nproblems ({len(bad)}):")
        for item in bad[: args.show]:
            print(f"  {item['claim_id']:28s} {item['detail'][:90]}")
        if len(bad) > args.show:
            print(f"  ... {len(bad) - args.show} more")
    return 0 if outcome["verdict"] == VERDICT_VERIFIED else 1


if __name__ == "__main__":
    sys.exit(main())
