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

from src.core.project_registry import (  # noqa: E402
    ProjectConfig,
    ProjectRegistry,
)
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


def select_excerpts(
    excerpts: List[Excerpt], limit: int, stratify: bool
) -> List[Excerpt]:
    """Select which excerpts a run covers.

    The dataset is ordered by publisher (10 articles each), so the
    plain first-N selection covers only N/10 publishers, and a
    whole-publisher bot wall then predetermines the run's decline
    rate. Stratified selection takes excerpts round-robin across
    publishers in dataset order, deterministically, so every
    publisher's crawler posture is represented.

    Parameters
    ----------
    excerpts : List[Excerpt]
        All excerpts, sorted by prompt number.
    limit : int
        How many to select; 0 means all.
    stratify : bool
        True for round-robin across publishers; False for first-N.

    Returns
    -------
    List[Excerpt]
        The selected excerpts.
    """
    if not limit or limit >= len(excerpts):
        return list(excerpts)
    if not stratify:
        return excerpts[:limit]

    by_publisher: Dict[str, List[Excerpt]] = {}
    publisher_order: List[str] = []
    for excerpt in excerpts:
        if excerpt.publication not in by_publisher:
            by_publisher[excerpt.publication] = []
            publisher_order.append(excerpt.publication)
        by_publisher[excerpt.publication].append(excerpt)

    selected: List[Excerpt] = []
    round_index = 0
    while len(selected) < limit:
        added = False
        for publisher in publisher_order:
            pool = by_publisher[publisher]
            if round_index < len(pool):
                selected.append(pool[round_index])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        round_index += 1
    return selected


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


def _apply_id_prefix(db_path: str, prefix: str, cases: Dict[str, Any]) -> None:
    """Rewrite seeded task ids to ``<prefix>_<original>`` in place.

    Cato associates a task with a registered project when the task id
    starts with the project's registry id plus an underscore (the
    exact-match fallback in its matcher), and the kanban provider
    always generates opaque hex ids. The seeder rewrites its own rows
    after creation so every downstream event carries an attributable
    id; experiment tooling only, no provider or Cato change.

    Parameters
    ----------
    db_path : str
        The seeded SQLite board file.
    prefix : str
        The registry project id to prefix.
    cases : Dict[str, Any]
        The manifest cases; their task ids are rewritten in place.
    """
    import sqlite3

    mapping = {
        old: f"{prefix}_{old}"
        for case in cases.values()
        for old in case["tasks"].values()
    }
    con = sqlite3.connect(db_path)
    with con:
        for old, new in mapping.items():
            con.execute("UPDATE tasks SET id=? WHERE id=?", (new, old))
            con.execute(
                "UPDATE task_dependencies SET task_id=? WHERE task_id=?",
                (new, old),
            )
            con.execute(
                "UPDATE task_dependencies SET depends_on_id=? " "WHERE depends_on_id=?",
                (new, old),
            )
            # Labels live in their own table keyed by task id; the
            # case:<id> label is how export_case finds a case's tasks,
            # so an orphaned label row means an empty audit bundle.
            con.execute(
                "UPDATE task_labels SET task_id=? WHERE task_id=?",
                (new, old),
            )
    con.close()
    for case in cases.values():
        case["tasks"] = {role: mapping[tid] for role, tid in case["tasks"].items()}


async def seed_cases(
    csv_path: str,
    db_path: str,
    project_id: str,
    limit: int,
    approval_rate: float,
    seed: int,
    manifest_path: str,
    stratify: bool = False,
    id_prefix: "str | None" = None,
    metadata_db: "str | None" = None,
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
    stratify : bool
        Select excerpts round-robin across publishers instead of
        first-N, so every publisher's crawler posture is represented.
    id_prefix : str | None
        When set (normally the registry project id), every seeded
        task id is rewritten to ``<prefix>_<original>`` so registry
        consumers such as Cato can attribute the tasks to the board.
    metadata_db : str | None
        Marcus persistence database to write ``task_metadata`` rows
        into (requires ``id_prefix``). Cato's task universe is this
        collection; tasks created through the NLP pipeline land there
        and directly seeded boards do not, so the seeder persists one
        honest row per task it creates.

    Returns
    -------
    Dict[str, Any]
        The manifest that was written.
    """
    excerpts = select_excerpts(load_excerpts(csv_path), limit, stratify)
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
    seeded_rows: List[Dict[str, Any]] = []
    for excerpt in excerpts:
        approval_sampled = excerpt.number in sampled
        spec = build_case_spec(excerpt, approval_sampled, project_id)
        ids_by_role: Dict[str, str] = {}
        for role, task_data, depends_on in spec:
            task_data = dict(task_data)
            task_data["dependencies"] = [ids_by_role[r] for r in depends_on]
            created = await board.create_task(task_data)
            ids_by_role[role] = created.id
            seeded_rows.append(
                {
                    "task_id": created.id,
                    "name": task_data.get("name", ""),
                    "description": str(task_data.get("description", ""))[:400],
                    "priority": str(task_data.get("priority", "medium")),
                    "estimated_hours": 0.5,
                    "labels": [task_data.get("task_type", "")],
                    "dependencies": list(task_data["dependencies"]),
                    "source_type": "tow_experiment_seed",
                }
            )
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

    if id_prefix:
        _apply_id_prefix(db_path, id_prefix, cases)
        for row in seeded_rows:
            row["task_id"] = f"{id_prefix}_{row['task_id']}"
            row["dependencies"] = [f"{id_prefix}_{d}" for d in row["dependencies"]]
            row["project_id"] = id_prefix
        if metadata_db:
            await _persist_task_metadata(metadata_db, seeded_rows)

    manifest = {
        "project_id": project_id,
        "db_path": db_path,
        "seed": seed,
        "approval_rate": approval_rate,
        "stratified": stratify,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


async def _persist_task_metadata(metadata_db: str, rows: List[Dict[str, Any]]) -> None:
    """Write one ``task_metadata`` row per seeded task.

    Cato renders tasks from Marcus's ``task_metadata`` persistence
    collection; the NLP pipeline writes it and direct seeding does
    not, so a seeded board is invisible without these rows. The rows
    are honest copies of what seeding just created, written through
    Marcus's own Persistence API.

    Parameters
    ----------
    metadata_db : str
        Path to the persistence SQLite database (normally
        ``<marcus root>/data/marcus.db``).
    rows : List[Dict[str, Any]]
        One prepared metadata row per seeded task, ids already
        prefixed.
    """
    from src.core.persistence import Persistence, SQLitePersistence

    store = Persistence(backend=SQLitePersistence(db_path=Path(metadata_db)))
    for row in rows:
        await store.store("task_metadata", row["task_id"], row)


async def register_board(
    project_id: str, db_path: str, registry: "ProjectRegistry | None" = None
) -> str:
    """Register a seeded board in Marcus's project registry.

    Seeding writes tasks straight into the SQLite board, which
    bypasses ``create_project``, the normal path that introduces a
    project to Marcus (``ProjectRegistry.add_project``). Without this
    step the registry, and every consumer of it such as the Cato
    dashboard's project list, never learns the board exists, and a
    server in ``single_project_mode`` stays bound to whatever project
    was already active. Registration is idempotent on the pair of
    project name and absolute db path, so re-seeding never duplicates
    an entry, and ``add_project`` only claims the active slot when no
    project was active before, so registering never disturbs a
    running server.

    Parameters
    ----------
    project_id : str
        The board's project id, which seeding also uses as the kanban
        project name (e.g. ``tow-b-claude``).
    db_path : str
        SQLite board file; stored absolute so the entry stays valid
        from any working directory.
    registry : ProjectRegistry | None
        Injected in tests; defaults to Marcus's file-backed registry
        under the package root.

    Returns
    -------
    str
        The registry project id (the existing one when the board was
        already registered).
    """
    absolute = str(Path(db_path).resolve())
    registry = registry or ProjectRegistry()
    await registry.initialize()

    for existing in await registry.list_projects(provider="sqlite"):
        cfg = existing.provider_config
        if cfg.get("project_name") == project_id and cfg.get("db_path") == absolute:
            return existing.id

    config = ProjectConfig(
        id="",
        name=f"{project_id} - Main Board",
        provider="sqlite",
        provider_config={
            "db_path": absolute,
            "project_name": project_id,
            "attachments_dir": str(Path(absolute).parent / "attachments"),
            "project_id": project_id,
        },
        tags=["auto-created", "sqlite", "tow-experiment"],
    )
    return await registry.add_project(config)


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
    parser.add_argument(
        "--stratify",
        action="store_true",
        help=(
            "Select excerpts round-robin across publishers instead of "
            "first-N, so every publisher's crawler posture is represented"
        ),
    )
    parser.add_argument(
        "--metadata-db",
        default=str(REPO_ROOT / "data" / "marcus.db"),
        help=(
            "Marcus persistence db for task_metadata rows, which Cato "
            "renders tasks from (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help=(
            "Skip registering the board in Marcus's project registry "
            "(registration is the default so the board shows up in "
            "registry consumers such as Cato)"
        ),
    )
    args = parser.parse_args()

    registry_id = None
    if not args.no_register:
        registry_id = asyncio.run(register_board(args.project, args.db))
        print(
            f"Registered {args.project} in Marcus's project registry "
            f"as {registry_id}"
        )

    manifest = asyncio.run(
        seed_cases(
            csv_path=args.dataset,
            db_path=args.db,
            project_id=args.project,
            limit=args.limit,
            approval_rate=args.approval_rate,
            seed=args.seed,
            manifest_path=args.manifest,
            stratify=args.stratify,
            id_prefix=registry_id,
            metadata_db=args.metadata_db if registry_id else None,
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
