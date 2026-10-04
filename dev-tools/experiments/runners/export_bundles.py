"""Export every case's audit bundle from a live Marcus server (#737).

Walks the runner's manifest, calls the ``export_case`` MCP tool for
each case over HTTP, and writes one JSON bundle per case. Run it at
the end of a pilot or full run, WHILE THE SERVER IS STILL UP: the
refusal log and the registrations live in server memory, so bundles
exported after a restart would be missing them.

Bundles contain the excerpts and the workers' raw engine output; do
not commit them to the repository (the Tow Center encrypted the
dataset to keep it away from crawlers).

Usage::

    python dev-tools/experiments/runners/export_bundles.py \
        --url http://localhost:4777/mcp \
        --manifest /path/to/manifest.json \
        --out-dir /path/to/bundles
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

from src.worker.inspector import Inspector, _extract_text_from_result  # noqa: E402


async def export_all(url: str, manifest_path: str, out_dir: str) -> int:
    """Export one bundle file per manifest case.

    Parameters
    ----------
    url : str
        Marcus HTTP MCP endpoint.
    manifest_path : str
        The runner's manifest (case ids and task ids).
    out_dir : str
        Directory for ``<case_id>.json`` bundle files.

    Returns
    -------
    int
        0 when every case exported with tasks, 1 otherwise.
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases = sorted(manifest.get("cases", {}))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    empty: list[str] = []
    client = Inspector(connection_type="http")
    async with client.connect(url=url):
        for case_id in cases:
            if client.session is None:
                raise RuntimeError("Not connected to Marcus")
            result = await client.session.call_tool(
                "export_case", arguments={"case_id": case_id}
            )
            text = _extract_text_from_result(result)
            bundle: Dict[str, Any] = json.loads(text) if text else {}
            (out / f"{case_id}.json").write_text(
                json.dumps(bundle, indent=1, ensure_ascii=False),
                encoding="utf-8",
            )
            if not bundle.get("tasks"):
                empty.append(case_id)

    print(f"Exported {len(cases)} bundles to {out}")
    if empty:
        print(f"WARNING: {len(empty)} bundles had no tasks: {empty[:5]}...")
        return 1
    return 0


def main() -> None:
    """Parse arguments and export the bundles."""
    parser = argparse.ArgumentParser(prog="export_bundles")
    parser.add_argument("--url", default="http://localhost:4777/mcp")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(export_all(args.url, args.manifest, args.out_dir)))


if __name__ == "__main__":
    main()
