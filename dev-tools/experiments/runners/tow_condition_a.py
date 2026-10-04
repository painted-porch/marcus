"""Condition A runner: each engine alone, no board (#737, step 9).

For each excerpt and each engine, one request with the study's exact
query; the raw response, the parsed claim, the model name, token
counts, and the cost estimate land in one JSONL row per answer. No
verification, no record: this is the baseline the original study
measured.

PAID CALLS. Every row costs money. The runner refuses to start
without ``--confirm-paid``, which exists so issue #737's rule 5 (no
paid API call without an approved estimate) cannot be tripped by
accident. Get the estimate approved first, then::

    python dev-tools/experiments/runners/tow_condition_a.py \
        --dataset /path/to/GenAISearch_Data.csv \
        --engines perplexity openai \
        --limit 50 \
        --out /path/to/condition-a.jsonl \
        --confirm-paid
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.tow_engines import ENGINES, EngineAnswer  # noqa: E402
from runners.tow_runner import QUERY_FORMAT, load_excerpts  # noqa: E402


def build_a_row(case_id: str, engine_name: str, answer: EngineAnswer) -> Dict[str, Any]:
    """Build one Condition A JSONL row from an engine answer.

    Parameters
    ----------
    case_id : str
        The case the query belongs to.
    engine_name : str
        The engine that answered.
    answer : EngineAnswer
        The parsed answer with usage and cost.

    Returns
    -------
    Dict[str, Any]
        The row the grader's A loader reads.
    """
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "case_id": case_id,
        "engine": engine_name,
        "model": answer.model,
        "headline": answer.headline,
        "publisher": answer.publisher,
        "date": answer.date,
        "url": answer.url,
        "raw": answer.raw,
        "prompt_tokens": answer.prompt_tokens,
        "completion_tokens": answer.completion_tokens,
        "cost_usd": answer.cost_usd,
    }


def main() -> None:
    """Run Condition A for the chosen engines and excerpts."""
    parser = argparse.ArgumentParser(prog="tow_condition_a")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--engines", nargs="+", required=True, choices=sorted(ENGINES))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--sleep", type=float, default=1.0, help="Seconds between requests"
    )
    parser.add_argument(
        "--confirm-paid",
        action="store_true",
        help="Required: confirms the cost estimate was approved (#737 rule 5)",
    )
    args = parser.parse_args()

    paid_engines = [e for e in args.engines if e != "fake"]
    if paid_engines and not args.confirm_paid:
        print(
            "REFUSING: engines "
            f"{paid_engines} make paid calls. Re-run with --confirm-paid "
            "after the cost estimate is approved (issue #737, rule 5).",
            file=sys.stderr,
        )
        sys.exit(2)

    excerpts = load_excerpts(args.dataset)
    if args.limit:
        excerpts = excerpts[: args.limit]

    engines = {name: ENGINES[name]() for name in args.engines}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_cost = 0.0
    with open(out_path, "a", encoding="utf-8") as out:
        for excerpt in excerpts:
            query = QUERY_FORMAT.format(excerpt=excerpt.excerpt)
            for name, engine in engines.items():
                try:
                    answer = engine.query(query)
                except Exception as engine_err:  # noqa: BLE001 - record and go on
                    print(f"[{name}] {excerpt.case_id} FAILED: {engine_err}")
                    answer = EngineAnswer(raw=f"ENGINE ERROR: {engine_err}")
                row = build_a_row(excerpt.case_id, name, answer)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                total_cost += answer.cost_usd
                print(
                    f"[{name}] {excerpt.case_id} url={answer.url[:60]!r} "
                    f"(${answer.cost_usd:.4f}, running ${total_cost:.4f})"
                )
                time.sleep(args.sleep)
    print(f"Condition A complete: {out_path} (estimated ${total_cost:.4f})")


if __name__ == "__main__":
    main()
