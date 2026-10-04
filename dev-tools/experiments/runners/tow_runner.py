"""Seed Tow Center citation cases onto a Marcus board (issue #737, step 6).

The Tow Center study gave AI search engines excerpts from news
articles and asked each engine to identify the headline, publisher,
publication date, and URL.
This runner reads that study's public dataset (200 excerpts across 20
publishers; 1,600 rows once each of the original eight engines'
answers are counted) and seeds one case per excerpt through the
kanban interface: a ``find`` task, a dependent ``verify`` task, an
``approve`` task on a deterministic sample, and a ``synthesize`` task.
No decomposer and no planner model are involved, which is the live
proof that decomposition is optional (the strip column of the issue's
keep/add/strip table).

Three rules the seeding enforces by construction:

- Ground truth never reaches the board. The publisher, date, and
  source URL live only in the manifest file this runner writes for
  the grader (step 9); a worker that can read the answer from the
  board is not being tested.
- The dataset stays out of the repository. The Tow Center encrypted
  it to keep the excerpts away from AI crawlers; decrypt it locally
  (the passphrase is in their README) and point ``--dataset`` at the
  CSV. Do not commit the CSV, and do not commit manifests, which
  contain the excerpts and answers.
- One board per finder vendor. Condition B attributes each case to
  the vendor whose finder worked it, and the board never assigns, so
  attribution comes from environment design: seed one project per
  vendor (e.g. ``--project tow-b-perplexity``) and register only that
  vendor's finder plus a different-vendor verifier against it.

Usage::

    python dev-tools/experiments/runners/tow_runner.py \
        --dataset /path/to/GenAISearch_Data.csv \
        --db /path/to/kanban.db \
        --project tow-b-perplexity \
        --limit 50 --approval-rate 0.1 --seed 737 \
        --manifest /path/to/manifest-perplexity.json
"""

import argparse
import asyncio
import csv
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.integrations.providers.sqlite_kanban import SQLiteKanban  # noqa: E402

# The study's query, verbatim from the Tow Center README, so the
# finder sends exactly what the original engines received.
QUERY_FORMAT = (
    "{excerpt}\n\n"
    "--\n"
    "Identify the article that contains this quote. Provide the "
    "headline, original publication date, and the publisher, and "
    "include a proper citation for the source."
)

# One task spec: (role, task_data without dependencies, depends-on roles).
TaskSpec = Tuple[str, Dict[str, Any], List[str]]


@dataclass
class Excerpt:
    """One prompt from the dataset with its ground truth.

    Parameters
    ----------
    number : int
        The dataset's prompt number (1 to 200).
    excerpt : str
        The hand-selected article excerpt used as the query.
    publication : str
        Ground truth publisher (manifest only, never the board).
    date : str
        Ground truth publication date as the dataset gives it.
    source_url : str
        Ground truth article URL (manifest only, never the board).
    crawler : str
        Whether the publisher allows or blocks search crawlers.
    paywalled : str
        The dataset's paywall note for the article.
    affiliation : str
        Whether the publisher has a deal or lawsuit with an AI company.
    """

    number: int
    excerpt: str
    publication: str
    date: str
    source_url: str
    crawler: str
    paywalled: str
    affiliation: str

    @property
    def case_id(self) -> str:
        """Zero-padded case id, e.g. ``tow-0007``."""
        return f"tow-{self.number:04d}"


def load_excerpts(csv_path: str) -> List[Excerpt]:
    """Load the unique excerpts from the Tow dataset CSV.

    The CSV holds one row per (prompt, engine) pair from the original
    study; this collapses to one row per prompt and keeps the ground
    truth columns.

    Parameters
    ----------
    csv_path : str
        Path to the decrypted ``GenAISearch_Data.csv``.

    Returns
    -------
    List[Excerpt]
        Unique excerpts sorted by prompt number.
    """
    by_number: Dict[int, Excerpt] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw_number = (row.get("Prompt Number") or "").strip()
            if not raw_number:
                continue
            number = int(raw_number)
            if number in by_number:
                continue
            by_number[number] = Excerpt(
                number=number,
                excerpt=(row.get("Prompt") or "").strip(),
                publication=(row.get("Publication") or "").strip(),
                date=(row.get("Date of Article") or "").strip(),
                source_url=(row.get("Source URL") or "").strip(),
                crawler=(row.get("Crawler") or "").strip(),
                paywalled=(row.get("Paywalled Article?") or "").strip(),
                affiliation=(row.get("Affiliation") or "").strip(),
            )
    return [by_number[n] for n in sorted(by_number)]


def sample_approvals(numbers: List[int], rate: float, seed: int) -> Set[int]:
    """Pick the deterministic approval sample.

    Parameters
    ----------
    numbers : List[int]
        Prompt numbers being seeded.
    rate : float
        Fraction of cases that get a human approval task (the
        protocol says 10 percent).
    seed : int
        RNG seed; the preregistration needs the sample reproducible.

    Returns
    -------
    Set[int]
        The sampled prompt numbers.
    """
    count = round(len(numbers) * rate)
    if count <= 0:
        return set()
    rng = random.Random(seed)
    return set(rng.sample(sorted(numbers), min(count, len(numbers))))


def build_case_spec(
    excerpt: Excerpt, approval_sampled: bool, project_id: str
) -> List[TaskSpec]:
    """Build the task chain for one case.

    Parameters
    ----------
    excerpt : Excerpt
        The excerpt the case is about. Only its text and case id go
        onto the board; the ground truth fields stay behind.
    approval_sampled : bool
        True when this case gets a human approve task between verify
        and synthesize.
    project_id : str
        Project id every task carries (GH-388 scoping).

    Returns
    -------
    List[TaskSpec]
        Ordered (role, task_data, depends_on_roles) triples.
    """
    case = excerpt.case_id
    labels = ["tow", f"case:{case}"]
    common = {
        "priority": "high",
        "estimated_hours": 0.1,
        "labels": labels,
        "project_id": project_id,
    }

    find: TaskSpec = (
        "find",
        {
            **common,
            "name": f"Find the source of {case}",
            "description": (
                "Send the query in inputs['query'] to your engine and "
                "report the claim as evidence: headline, publisher, "
                "date, url, and raw (the engine's full response)."
            ),
            "task_type": "find",
            "inputs": {
                "case_id": case,
                "excerpt": excerpt.excerpt,
                "query": QUERY_FORMAT.format(excerpt=excerpt.excerpt),
            },
            "output_schema": {
                "type": "object",
                "required": ["headline", "publisher", "date", "url"],
            },
        },
        [],
    )
    verify: TaskSpec = (
        "verify",
        {
            **common,
            "name": f"Verify the claim for {case}",
            "description": (
                "Fetch the claimed URL respecting robots.txt, check "
                "the excerpt appears on the page, check the publisher "
                "against the domain and the date within one day, and "
                "report verdict (verified | contradicted | "
                "unverifiable) and checks."
            ),
            "task_type": "verify",
            "inputs": {"case_id": case, "excerpt": excerpt.excerpt},
            "output_schema": {
                "type": "object",
                "required": ["verdict", "checks"],
            },
        },
        ["find"],
    )
    approve: TaskSpec = (
        "approve",
        {
            **common,
            "name": f"Approve the verified claim for {case}",
            "description": (
                "A person reviews the claim and the verdict side by "
                "side and approves or rejects (calibration sample)."
            ),
            "task_type": "approve",
            "inputs": {"case_id": case},
        },
        ["verify"],
    )
    synthesize: TaskSpec = (
        "synthesize",
        {
            **common,
            "name": f"Render the case answer for {case}",
            "description": (
                "Assemble the case answer from the claim and the "
                "verdict: verified claims become the answer; "
                "contradicted or unverifiable claims close the case "
                "as declined with the reason attached."
            ),
            "task_type": "synthesize",
            "inputs": {"case_id": case},
            "output_schema": {"type": "object", "required": ["answer"]},
        },
        ["approve"] if approval_sampled else ["verify"],
    )

    spec = [find, verify]
    if approval_sampled:
        spec.append(approve)
    spec.append(synthesize)
    return spec


async def seed_cases(
    csv_path: str,
    db_path: str,
    project_id: str,
    limit: int,
    approval_rate: float,
    seed: int,
    manifest_path: str,
) -> Dict[str, Any]:
    """Seed cases onto the board and write the grader's manifest.

    Parameters
    ----------
    csv_path : str
        The decrypted dataset CSV.
    db_path : str
        SQLite board file (created if absent).
    project_id : str
        Project id for every task; one project per finder vendor.
    limit : int
        Seed the first ``limit`` excerpts (50 for the pilot, 200 for
        the full run); 0 means all.
    approval_rate : float
        Fraction of cases given a human approve task.
    seed : int
        RNG seed for the approval sample.
    manifest_path : str
        Where to write the manifest JSON (ground truth, task ids,
        approval flags). Keep it out of the repository.

    Returns
    -------
    Dict[str, Any]
        The manifest that was written.
    """
    excerpts = load_excerpts(csv_path)
    if limit:
        excerpts = excerpts[:limit]
    sampled = sample_approvals([e.number for e in excerpts], approval_rate, seed)

    board = SQLiteKanban(
        {
            "db_path": db_path,
            "project_name": project_id,
            "attachments_dir": str(Path(db_path).parent / "attachments"),
        }
    )
    await board.connect()

    cases: Dict[str, Any] = {}
    for excerpt in excerpts:
        approval_sampled = excerpt.number in sampled
        spec = build_case_spec(excerpt, approval_sampled, project_id)
        ids_by_role: Dict[str, str] = {}
        for role, task_data, depends_on in spec:
            task_data = dict(task_data)
            task_data["dependencies"] = [ids_by_role[r] for r in depends_on]
            created = await board.create_task(task_data)
            ids_by_role[role] = created.id
        cases[excerpt.case_id] = {
            "prompt_number": excerpt.number,
            "excerpt": excerpt.excerpt,
            "ground_truth": {
                "publication": excerpt.publication,
                "date": excerpt.date,
                "source_url": excerpt.source_url,
            },
            "crawler": excerpt.crawler,
            "paywalled": excerpt.paywalled,
            "affiliation": excerpt.affiliation,
            "approval_sampled": approval_sampled,
            "tasks": ids_by_role,
        }

    await board.disconnect()

    manifest = {
        "project_id": project_id,
        "db_path": db_path,
        "seed": seed,
        "approval_rate": approval_rate,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def main() -> None:
    """Parse arguments and seed the cases."""
    parser = argparse.ArgumentParser(prog="tow_runner")
    parser.add_argument(
        "--dataset", required=True, help="Decrypted GenAISearch_Data.csv"
    )
    parser.add_argument("--db", required=True, help="SQLite board file")
    parser.add_argument(
        "--project",
        required=True,
        help="Project id; one per finder vendor (e.g. tow-b-perplexity)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Seed the first N excerpts; 0 means all (default: %(default)s)",
    )
    parser.add_argument(
        "--approval-rate",
        type=float,
        default=0.1,
        help="Fraction of cases with a human approval (default: %(default)s)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=737,
        help="RNG seed for the approval sample (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Where to write the grader manifest (keep out of the repo)",
    )
    args = parser.parse_args()

    manifest = asyncio.run(
        seed_cases(
            csv_path=args.dataset,
            db_path=args.db,
            project_id=args.project,
            limit=args.limit,
            approval_rate=args.approval_rate,
            seed=args.seed,
            manifest_path=args.manifest,
        )
    )
    total = len(manifest["cases"])
    approvals = sum(1 for c in manifest["cases"].values() if c["approval_sampled"])
    print(
        f"Seeded {total} cases ({approvals} with human approval) into "
        f"{args.db} as project {args.project}; manifest at {args.manifest}"
    )


if __name__ == "__main__":
    main()
