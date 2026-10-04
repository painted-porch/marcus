"""The finder worker: one answer engine on the board (#737, step 7).

A long-lived pull-loop session that wraps exactly one engine. It
pulls ``find`` tasks, sends the study query from the task's inputs
to its engine, and reports the claim as typed evidence (headline,
publisher, date, url, raw). It also handles ``synthesize`` tasks,
because eligibility may offer them to any agent principal and a
worker must complete what it pulls.

Run order for a condition B board (one project per finder vendor):
run the finder FIRST until it idle-exits (all find tasks closed),
then the verifier, then the approvals CLI, then the verifier again
for sampled-case synthesize tasks. The verifier cannot work a find
task, and the board has no decline, so phases keep wrong pairings
from ever being offered.

PAID CALLS: every non-fake engine call costs money. Do not run with
a real engine outside the approved pilot (issue #737 rule 5). The
``fake`` engine is free and exists for end-to-end shakeouts::

    python dev-tools/experiments/runners/tow_finder.py \
        --url http://localhost:4777/mcp --project tow-smoke \
        --agent-id finder-fake-1 --vendor fakevendor --engine fake
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.tow_engines import ENGINES, BaseEngine, FakeEngine  # noqa: E402
from runners.tow_worker_common import (  # noqa: E402
    WorkerConfig,
    append_usage_row,
    render_case_answer,
    work_loop,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the finder's argument parser."""
    parser = argparse.ArgumentParser(prog="tow_finder")
    parser.add_argument("--url", default="http://localhost:4777/mcp")
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--vendor",
        required=True,
        help="Declared vendor (perplexity, openai, google, xai, fakevendor)",
    )
    parser.add_argument(
        "--engine",
        required=True,
        choices=sorted(ENGINES),
        help="Which engine this finder wraps; 'fake' is free",
    )
    parser.add_argument("--model", default=None, help="Engine model override")
    parser.add_argument(
        "--fake-claim",
        default=None,
        help="JSON file with the claim the fake engine returns",
    )
    parser.add_argument("--idle-exits", type=int, default=10)
    parser.add_argument(
        "--usage-log",
        default=None,
        help="JSONL file for per-call token and cost rows",
    )
    return parser


def main() -> None:
    """Run the finder loop."""
    args = build_parser().parse_args()

    if args.engine == "fake":
        claim = None
        if args.fake_claim:
            claim = json.loads(Path(args.fake_claim).read_text(encoding="utf-8"))
        engine: BaseEngine = FakeEngine(model=args.model, claim=claim)
    else:
        engine = ENGINES[args.engine](model=args.model)

    cfg = WorkerConfig(
        marcus_url=args.url,
        project_id=args.project,
        agent_id=args.agent_id,
        vendor=args.vendor,
        role="finder",
        skills=["tow", "find"],
        idle_exits=args.idle_exits,
        usage_log=args.usage_log,
    )

    def handle_find(task: Dict[str, Any]) -> Dict[str, Any]:
        """Send the study query to the engine; report the claim."""
        inputs = task.get("inputs") or {}
        query = str(inputs.get("query") or inputs.get("excerpt") or "")
        answer = engine.query(query)
        append_usage_row(
            cfg.usage_log,
            {
                "agent_id": cfg.agent_id,
                "engine": args.engine,
                "model": answer.model,
                "case_id": inputs.get("case_id", ""),
                "task_id": task.get("id", ""),
                "prompt_tokens": answer.prompt_tokens,
                "completion_tokens": answer.completion_tokens,
                "cost_usd": answer.cost_usd,
            },
        )
        return answer.claim_evidence()

    def handle_synthesize(task: Dict[str, Any]) -> Dict[str, Any]:
        """Render the case answer from the accumulated inputs."""
        return render_case_answer(task.get("inputs") or {})

    stats = asyncio.run(
        work_loop(cfg, {"find": handle_find, "synthesize": handle_synthesize})
    )
    print(f"[{cfg.agent_id}] done: {stats}")


if __name__ == "__main__":
    main()
