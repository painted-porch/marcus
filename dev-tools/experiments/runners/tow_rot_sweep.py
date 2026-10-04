"""Link-rot and crawler-block sweep (#737, step 10, free half).

Before any model call, fetch the ground-truth URLs of the pilot
excerpts with the SAME fetcher and robots policy the verifier uses,
and record what even a perfect finder could verify today. Four
outcomes per URL:

- ``crawler_blocked``: robots.txt refused the fetch.
- ``broken``: network error or HTTP error status.
- ``excerpt_missing``: the page fetched but the excerpt is not in
  its text (moved, paywalled, or rendered by JavaScript).
- ``verifiable``: the page fetched and carries the excerpt.

The pilot's decline rate is reported raw AND net of this baseline,
and the preregistered 50 percent no-go threshold is read net of it:
declines caused by rot indict neither the verifier nor the record.

Free to run (page fetches only), and the fetches land in the shared
cache, so the pilot's verifier replays these exact observations::

    python dev-tools/experiments/runners/tow_rot_sweep.py \
        --dataset /path/to/GenAISearch_Data.csv \
        --limit 50 \
        --cache-dir /path/to/pilot/fetch-cache \
        --out /path/to/pilot/rot-sweep.json \
        --details /path/to/pilot/rot-sweep-details.jsonl

Outputs contain ground-truth URLs and excerpt presence; keep them
out of the repository like the dataset itself.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.tow_checks import excerpt_on_page  # noqa: E402
from runners.tow_fetch import FetchCache, FetchResult  # noqa: E402

OUTCOMES = (
    "verifiable",
    "excerpt_missing",
    "broken",
    "blocked_by_server",
    "crawler_blocked",
)

# Statuses that mean "the server refused a bot", not "the page died".
_BOT_WALL_STATUSES = {401, 403, 429}


def classify_fetch(fetch: FetchResult, excerpt: str) -> str:
    """Classify one ground-truth URL's fetch outcome.

    Parameters
    ----------
    fetch : FetchResult
        The shared-cache fetch of the ground-truth URL.
    excerpt : str
        The excerpt that should be on the page.

    Returns
    -------
    str
        One of :data:`OUTCOMES`. ``crawler_blocked`` is a robots.txt
        refusal; ``blocked_by_server`` is a 401/403/429 bot wall
        (crawler blocking in effect even where robots.txt permits);
        ``broken`` is a dead or erroring page.
    """
    if fetch.robots_blocked:
        return "crawler_blocked"
    if fetch.status in _BOT_WALL_STATUSES:
        return "blocked_by_server"
    if fetch.status is None or fetch.status >= 400 or not fetch.html:
        return "broken"
    if excerpt_on_page(excerpt, fetch.text):
        return "verifiable"
    return "excerpt_missing"


def summarize(classes: List[str]) -> Dict[str, Any]:
    """Compute the rot baseline from the per-URL classes.

    Parameters
    ----------
    classes : List[str]
        One outcome per swept URL.

    Returns
    -------
    Dict[str, Any]
        Counts, the ceiling on what any finder could verify, and the
        baseline unverifiable rate the no-go threshold is read net of.
    """
    total = len(classes)
    counts = Counter(classes)
    verifiable = counts.get("verifiable", 0)
    return {
        "total": total,
        "counts": {outcome: counts.get(outcome, 0) for outcome in OUTCOMES},
        "max_verifiable_rate": (verifiable / total) if total else None,
        "baseline_unverifiable_rate": ((total - verifiable) / total if total else None),
    }


def main() -> None:
    """Sweep the pilot's ground-truth URLs and write the baseline."""
    from runners.tow_runner import load_excerpts

    parser = argparse.ArgumentParser(prog="tow_rot_sweep")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--out", required=True, help="Summary JSON path")
    parser.add_argument("--details", default=None, help="Per-URL JSONL path (optional)")
    args = parser.parse_args()

    excerpts = load_excerpts(args.dataset)
    if args.limit:
        excerpts = excerpts[: args.limit]

    cache = FetchCache(cache_dir=args.cache_dir)
    classes: List[str] = []
    details: List[Dict[str, Any]] = []
    for excerpt in excerpts:
        fetch = cache.fetch(excerpt.source_url)
        outcome = classify_fetch(fetch, excerpt.excerpt)
        classes.append(outcome)
        details.append(
            {
                "case_id": excerpt.case_id,
                "publication": excerpt.publication,
                "dataset_crawler_note": excerpt.crawler,
                "url": excerpt.source_url,
                "outcome": outcome,
                "http_status": fetch.status,
                "fetched_at": fetch.fetched_at,
            }
        )
        print(f"{excerpt.case_id} {outcome:16s} {excerpt.publication}")

    summary = {
        "swept_at": datetime.now(timezone.utc).isoformat(),
        "dataset_limit": args.limit,
        **summarize(classes),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    if args.details:
        details_path = Path(args.details)
        details_path.parent.mkdir(parents=True, exist_ok=True)
        with open(details_path, "w", encoding="utf-8") as f:
            for row in details:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
