"""Redact dataset text from audit bundles for public release (#737).

The Tow Center distributes its 200 excerpts encrypted so the article
text stays out of AI training crawlers, and this repository honors
that: raw bundles carry the excerpt (in task ``inputs``), the study
query built from it, and the engine's full prose answer (``raw``
evidence), so they stay out of the repository.
This tool produces the publishable copy: every ``excerpt``, ``query``,
and ``raw`` value is replaced with a redaction marker, while claims,
check results, verdicts, actors, approvals, refusals, and costs stay
word-for-word, because those are the receipts the release exists to
publish.

Usage::

    python dev-tools/experiments/runners/redact_bundles.py \
        --bundles /path/to/bundles-openai /path/to/bundles-pplx \
        --out /path/to/release/bundles-redacted
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REDACTED = "[redacted: Tow Center dataset text; see the study's repository]"
REDACT_KEYS = frozenset({"excerpt", "query", "raw"})


def redact(value: Any) -> Any:
    """Return a deep copy with dataset-text fields redacted.

    Parameters
    ----------
    value : Any
        Any JSON-shaped structure from a bundle.

    Returns
    -------
    Any
        The same structure with every non-empty string under an
        ``excerpt``, ``query``, or ``raw`` key replaced by the
        redaction marker, at any depth.
    """
    if isinstance(value, dict):
        return {
            k: (
                REDACTED if k in REDACT_KEYS and isinstance(v, str) and v else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main() -> None:
    """Redact every bundle in the given directories."""
    parser = argparse.ArgumentParser(prog="redact_bundles")
    parser.add_argument("--bundles", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out_root = Path(args.out)
    total = 0
    for bundle_dir in args.bundles:
        src = Path(bundle_dir)
        dst = out_root / src.name
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.json")):
            bundle = json.loads(f.read_text(encoding="utf-8"))
            (dst / f.name).write_text(
                json.dumps(redact(bundle), indent=1, ensure_ascii=False),
                encoding="utf-8",
            )
            total += 1
    print(f"Redacted {total} bundles into {out_root}")


if __name__ == "__main__":
    main()
    sys.exit(0)
