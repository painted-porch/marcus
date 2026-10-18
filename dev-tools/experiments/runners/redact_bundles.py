"""Redact Tow audit bundles for publication (#737).

The raw bundles that ``export_bundles.py`` writes contain the Tow
dataset excerpts (``tasks[].inputs.excerpt`` and ``tasks[].inputs.query``,
which embeds the excerpt) and the raw engine output
(``tasks[].evidence.raw``). The Tow Center encrypted its dataset to keep
the excerpts away from crawlers, so none of that may be published.

This script writes a redacted copy of each bundle. It strips the
sensitive fields and keeps everything that makes a bundle a useful audit
record: the claimed citation (headline, publisher, date, url), the
deterministic checks and verdicts, approvals, refusals, and costs. It
then verifies that not one stripped string survives anywhere in the
redacted output, and exits non-zero if any does.

Usage::

    python dev-tools/experiments/runners/redact_bundles.py \
        --in-dir  /path/to/bundles-openai \
        --out-dir /path/to/redacted/bundles-openai
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List

# Keys whose string values hold the dataset excerpt or the raw engine
# output. These are the only fields removed; the claim, checks, verdicts,
# approvals, refusals, and costs are kept.
REDACT_KEYS = {
    "excerpt",
    "query",
    "raw",
    "raw_response",
    "raw_output",
    "raw_text",
    "prose",
    "prompt",
    "response",
}
MARKER = "[redacted: excerpt or raw engine output withheld per Tow dataset terms]"


def redact(obj: Any, stripped: List[str]) -> Any:
    """Return a copy of ``obj`` with sensitive string fields replaced.

    Parameters
    ----------
    obj : Any
        A JSON-decoded value from a bundle.
    stripped : list of str
        Accumulator: every non-empty string value that was removed is
        appended here, so the caller can verify none of them survive.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in REDACT_KEYS and isinstance(value, str):
                if value:
                    stripped.append(value)
                out[key] = MARKER
            else:
                out[key] = redact(value, stripped)
        return out
    if isinstance(obj, list):
        return [redact(item, stripped) for item in obj]
    return obj


def main() -> None:
    """Redact every bundle in a directory and verify no leak remains."""
    parser = argparse.ArgumentParser(prog="redact_bundles")
    parser.add_argument(
        "--in-dir", required=True, help="Directory of raw bundle JSON files"
    )
    parser.add_argument(
        "--out-dir", required=True, help="Directory to write redacted bundles into"
    )
    args = parser.parse_args()

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bundles = 0
    stripped_total = 0
    leaks = 0
    for path in sorted(in_dir.glob("*.json")):
        data = json.load(open(path))
        stripped: List[str] = []
        redacted = redact(data, stripped)
        blob = json.dumps(redacted, ensure_ascii=False)
        # Verification: no stripped string may appear anywhere in the
        # redacted output, including inside fields we did not target.
        for sensitive in stripped:
            if sensitive and sensitive in blob:
                print(
                    f"LEAK in {path.name}: a stripped string survived",
                    file=sys.stderr,
                )
                leaks += 1
        json.dump(
            redacted, open(out_dir / path.name, "w"), ensure_ascii=False, indent=2
        )
        bundles += 1
        stripped_total += len(stripped)

    print(
        f"redacted {bundles} bundles, "
        f"stripped {stripped_total} sensitive strings, leaks: {leaks}"
    )
    if leaks:
        sys.exit(2)


if __name__ == "__main__":
    main()
