"""Snapshot GitHub repo traffic into a durable local store.

GitHub's traffic API (views, clones, referrers, paths) retains only a
rolling 14 days, so launch-week numbers vanish unless captured.  Run
weekly (cron) and this tool appends to ``~/marcus-experiments/traffic``:

- ``views.jsonl`` / ``clones.jsonl`` — one row per day, merged across
  overlapping snapshots (larger count wins, so a mid-day undercount
  never clobbers a completed day).
- ``snapshots.jsonl`` — one raw row per run: referrers, popular paths,
  stars, forks, watchers.

Usage::

    python dev-tools/metrics/snapshot_traffic.py \
        --repo painted-porch/marcus --out ~/marcus-experiments/traffic

Requires an authenticated ``gh`` CLI; makes no writes to GitHub.
"""

import argparse
import datetime
import json
import subprocess
from pathlib import Path
from typing import Any


def merge_daily(
    existing: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge overlapping per-day traffic rows from two snapshots.

    Parameters
    ----------
    existing : list of dict
        Rows already on disk, each ``{"timestamp", "count", "uniques"}``.
    new : list of dict
        Rows from the current API call, same shape.

    Returns
    -------
    list of dict
        One row per timestamp, sorted ascending.  Where both inputs
        cover a day, each field keeps the larger value, because a day's
        true total only grows and an early snapshot undercounts it.
    """
    by_day: dict[str, dict[str, Any]] = {
        row["timestamp"]: dict(row) for row in existing
    }
    for row in new:
        day = by_day.setdefault(row["timestamp"], dict(row))
        day["count"] = max(day["count"], row["count"])
        day["uniques"] = max(day["uniques"], row["uniques"])
    return [by_day[ts] for ts in sorted(by_day)]


def _gh_api(path: str) -> Any:
    """Return the parsed JSON body of one ``gh api`` call."""
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list, treating a missing file as empty."""
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Rewrite a JSONL file from a list of rows."""
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    """Fetch traffic, merge the daily series, and append the raw snapshot."""
    parser = argparse.ArgumentParser(prog="snapshot_traffic")
    parser.add_argument("--repo", default="painted-porch/marcus")
    parser.add_argument("--out", default="~/marcus-experiments/traffic")
    args = parser.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    for kind in ("views", "clones"):
        series = _gh_api(f"repos/{args.repo}/traffic/{kind}?per=day")[kind]
        path = out / f"{kind}.jsonl"
        merged = merge_daily(_load_jsonl(path), series)
        _write_jsonl(path, merged)
        print(f"{kind}: {len(merged)} days on record")

    meta = _gh_api(f"repos/{args.repo}")
    snapshot = {
        "taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stars": meta["stargazers_count"],
        "forks": meta["forks_count"],
        "watchers": meta["subscribers_count"],
        "referrers": _gh_api(f"repos/{args.repo}/traffic/popular/referrers"),
        "paths": _gh_api(f"repos/{args.repo}/traffic/popular/paths"),
    }
    with (out / "snapshots.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, sort_keys=True) + "\n")
    print(f"snapshot: {snapshot['stars']} stars, {snapshot['forks']} forks")


if __name__ == "__main__":
    main()
