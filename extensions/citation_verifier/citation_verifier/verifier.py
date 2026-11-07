"""The verifier worker: fetch, check, report; no model calls (#737).

A long-lived pull-loop session that pulls ``verify`` tasks, reads
the claim the board wrote into the task's inputs (and nothing else:
the finder's raw response never reaches it), fetches the claimed URL
through the shared robots-respecting cache, runs the fixed checks,
and reports the verdict. It also handles ``synthesize`` tasks.

The verifier costs nothing to run: it never calls a model, and its
page fetches go through the same cache Condition C replays, so both
conditions see identical observations (protocol step 9).

Run it AFTER the finder has drained the board's find tasks; see the
phase order in ``tow_finder.py``'s docstring::

    python -m citation_verifier.verifier \
        --url http://localhost:4777/mcp --project tow-smoke \
        --agent-id verifier-fetch-1 --vendor fetchcheck \
        --cache-dir /path/to/fetch-cache
"""

import argparse
import asyncio
from typing import Any, Dict

from citation_verifier.fetch import FetchCache  # noqa: E402
from citation_verifier.worker_common import (  # noqa: E402
    WorkerConfig,
    build_verify_evidence,
    render_case_answer,
    work_loop,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the verifier's argument parser."""
    parser = argparse.ArgumentParser(prog="tow_verifier")
    parser.add_argument("--url", default="http://localhost:4777/mcp")
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--vendor",
        required=True,
        help=(
            "Declared vendor; must differ from the board's finder vendor "
            "or eligibility will refuse every verify task"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Shared fetch cache directory (Condition C replays it)",
    )
    parser.add_argument("--idle-exits", type=int, default=10)
    return parser


def main() -> None:
    """Run the verifier loop."""
    args = build_parser().parse_args()
    cache = FetchCache(cache_dir=args.cache_dir)

    cfg = WorkerConfig(
        marcus_url=args.url,
        project_id=args.project,
        agent_id=args.agent_id,
        vendor=args.vendor,
        role="verifier",
        skills=["tow", "verify"],
        idle_exits=args.idle_exits,
    )

    def handle_verify(task: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch the claimed URL and report the verdict."""
        return build_verify_evidence(task.get("inputs") or {}, cache)

    def handle_synthesize(task: Dict[str, Any]) -> Dict[str, Any]:
        """Render the case answer from the accumulated inputs."""
        return render_case_answer(task.get("inputs") or {})

    stats = asyncio.run(
        work_loop(cfg, {"verify": handle_verify, "synthesize": handle_synthesize})
    )
    print(f"[{cfg.agent_id}] done: {stats}")


if __name__ == "__main__":
    main()
