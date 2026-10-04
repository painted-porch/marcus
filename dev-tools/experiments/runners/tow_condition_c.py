"""Condition C: the verifier bolted on with no record (#737, step 9).

The same checks the Condition B verifier runs, applied to Condition
A's answers as a post-hoc filter: no board, no eligibility, no held
close, no lease, no audit bundle. Verified answers are kept,
contradicted and unverifiable answers are dropped (declined). C and
B share one fetch cache, so a page that moves between runs scores
the same in both; that shared cache is a preregistered requirement.

This condition exists to keep the claim honest: the accuracy gain
comes from the verifier, and C scoring like B is the expected
result. What B adds is that the check cannot be skipped, cannot be
done by the author or the author's vendor, cannot be lost, and can
be shown to a third party.

Free to run: page fetches only, no model calls::

    python dev-tools/experiments/runners/tow_condition_c.py \
        --a-responses /path/to/condition-a.jsonl \
        --dataset /path/to/GenAISearch_Data.csv \
        --cache-dir /path/to/fetch-cache \
        --out /path/to/condition-c.jsonl
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.tow_checks import decide_verdict  # noqa: E402
from runners.tow_fetch import FetchCache, FetchResult  # noqa: E402
from runners.tow_runner import load_excerpts  # noqa: E402

CLAIM_FIELDS = ("headline", "publisher", "date", "url")


def filter_answer(a_row: Dict[str, Any], excerpt: str, cache: Any) -> Dict[str, Any]:
    """Run the verifier's checks on one Condition A answer.

    Parameters
    ----------
    a_row : Dict[str, Any]
        One Condition A JSONL row (the claim plus usage).
    excerpt : str
        The case's excerpt.
    cache : Any
        The shared fetch cache.

    Returns
    -------
    Dict[str, Any]
        The Condition C row: kept or dropped, with the verdict and
        checks, the answer minus ``raw`` when kept, and A's cost.
    """
    claim = {name: str(a_row.get(name) or "") for name in CLAIM_FIELDS}
    if claim["url"]:
        fetch = cache.fetch(claim["url"])
    else:
        fetch = FetchResult(
            url="",
            final_url="",
            status=None,
            html="",
            text="",
            robots_blocked=False,
            error="no URL claimed",
            fetched_at="",
        )
    verdict, checks = decide_verdict(fetch, excerpt, claim)
    kept = verdict == "verified"
    return {
        "case_id": a_row.get("case_id", ""),
        "engine": a_row.get("engine", ""),
        "verdict": verdict,
        "checks": checks,
        "kept": kept,
        "answer": claim if kept else None,
        "cost_usd": float(a_row.get("cost_usd") or 0.0),
    }


def main() -> None:
    """Filter every Condition A answer through the shared verifier."""
    parser = argparse.ArgumentParser(prog="tow_condition_c")
    parser.add_argument("--a-responses", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    excerpts = {e.case_id: e.excerpt for e in load_excerpts(args.dataset)}
    cache = FetchCache(cache_dir=args.cache_dir)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    total = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for line in Path(args.a_responses).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            a_row = json.loads(line)
            excerpt = excerpts.get(str(a_row.get("case_id") or ""), "")
            result = filter_answer(a_row, excerpt, cache)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            total += 1
            kept += int(result["kept"])
    print(f"Condition C complete: kept {kept}/{total} answers -> {out_path}")


if __name__ == "__main__":
    main()
