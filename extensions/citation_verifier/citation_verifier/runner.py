"""Seed Tow Center citation cases onto a Marcus board (issue #737, step 6).

The Tow Center study gave AI search engines excerpts from news
articles and asked each engine to identify the headline, publisher,
publication date, and URL.
This runner reads that study's public dataset (200 excerpts across 20
publishers; 1,600 rows once each of the original eight engines'
answers are counted) and seeds one case per excerpt: a ``find`` task, a
dependent ``verify`` task, an ``approve`` task on a deterministic
sample, and a ``synthesize`` task.
No decomposer and no planner model are involved, which is the live
proof that decomposition is optional (the strip column of the issue's
keep/add/strip table).

The cards reach the board over MCP only (ADR-0013 K2, decided
2026-11-13, issue #911): one ``create_project`` call carries every
seed spec with its ``task_type``, ``inputs`` and final ``id``, plus the
project's task-type declaration (``task_type_registrations``, issue
#909), and Marcus mints the project record, the board and Cato's
metadata rows itself. This module imports no kernel code; it talks to a
running server the way every worker does.

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
- One project per finder vendor. Condition B attributes each case to
  the vendor whose finder worked it, and the board never assigns, so
  attribution comes from environment design: seed one project per
  vendor (e.g. ``--project tow-b-perplexity``) and register only that
  vendor's finder plus a different-vendor verifier against it.

Usage (against a running Marcus)::

    python -m citation_verifier.runner \
        --dataset /path/to/GenAISearch_Data.csv \
        --url http://127.0.0.1:4777/mcp \
        --project tow-b-perplexity \
        --limit 50 --approval-rate 0.1 --seed 737 \
        --manifest /path/to/manifest-perplexity.json

The manifest records the registry project id Marcus returned; the
workers, the approvals CLI and the exporter take that id as
``--project``.
"""

import argparse
import asyncio
import csv
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from citation_verifier.kernel import Inspector, extract_text_from_result

# The study's query, verbatim from the Tow Center README, so the
# finder sends exactly what the original engines received.
QUERY_FORMAT = (
    "{excerpt}\n\n"
    "--\n"
    "Identify the article that contains this quote. Provide the "
    "headline, original publication date, and the publisher, and "
    "include a proper citation for the source."
)

# One task spec: (role, seed spec without depends_on, depends-on roles).
TaskSpec = Tuple[str, Dict[str, Any], List[str]]

#: An MCP tool call: ``(tool_name, arguments) -> parsed reply``.
ToolCall = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

#: The chain's ADR-0013 K2 declaration (issue #909): what each task type's
#: completion must report as evidence, and that ``verify`` checks ``find``
#: (so the board never offers a verify card to the finder whose claim it
#: checks, nor to that finder's vendor). ``checks_injectable`` is false
#: throughout: the kernel adds no checks of its own to this chain (K4).
#: The evidence shapes are the ones the workers report
#: (``worker_common.py``) and the approvals CLI sends
#: (``src/cli/approvals.py``).
TASK_TYPE_REGISTRATIONS: Dict[str, Dict[str, Any]] = {
    "find": {
        "evidence_schema": {
            "type": "object",
            "required": ["headline", "publisher", "date", "url"],
        },
        "checks_injectable": False,
    },
    "verify": {
        "evidence_schema": {"type": "object", "required": ["verdict", "checks"]},
        "verifies_task_type": "find",
        "checks_injectable": False,
    },
    "approve": {
        "evidence_schema": {"type": "object", "required": ["decision", "approved_by"]},
        "checks_injectable": False,
    },
    "synthesize": {
        "evidence_schema": {"type": "object", "required": ["answer"]},
        "checks_injectable": False,
    },
}

_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


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


def task_id_for(project_name: str, case_id: str, role: str) -> str:
    """Return the final board id of one card.

    Parameters
    ----------
    project_name : str
        The project the card belongs to; several projects may share one
        board file, so the id carries it.
    case_id : str
        The case, e.g. ``tow-0007``.
    role : str
        ``find``, ``verify``, ``approve`` or ``synthesize``.

    Returns
    -------
    str
        An id over ``[A-Za-z0-9_-]``, the charset the board accepts.
    """
    return _ID_SAFE.sub("-", f"{project_name}-{case_id}-{role}").strip("-")


def build_case_spec(
    excerpt: Excerpt, approval_sampled: bool, project_name: str
) -> List[TaskSpec]:
    """Build the task chain for one case as seed specs.

    Parameters
    ----------
    excerpt : Excerpt
        The excerpt the case is about. Only its text and case id go
        onto the board; the ground truth fields stay behind.
    approval_sampled : bool
        True when this case gets a human approve task between verify
        and synthesize.
    project_name : str
        The project the cards belong to; part of every card's id.

    Returns
    -------
    List[TaskSpec]
        Ordered (role, seed spec, depends_on_roles) triples. Each seed
        spec is what ``create_project(options.tasks)`` accepts
        (``src/integrations/seeded_tasks.py``): name, description,
        labels, estimated_hours, priority, ``task_type``, ``inputs``
        and ``id``. The evidence each type must report is declared once
        per project (:data:`TASK_TYPE_REGISTRATIONS`), not per card.
    """
    case = excerpt.case_id
    common = {
        "priority": "high",
        "estimated_hours": 0.1,
        "labels": ["tow", f"case:{case}"],
    }

    def _spec(
        role: str, name: str, description: str, inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            **common,
            "id": task_id_for(project_name, case, role),
            "name": name,
            "description": description,
            "task_type": role,
            "inputs": inputs,
        }

    find: TaskSpec = (
        "find",
        _spec(
            "find",
            f"Find the source of {case}",
            "Send the query in inputs['query'] to your engine and "
            "report the claim as evidence: headline, publisher, "
            "date, url, and raw (the engine's full response).",
            {
                "case_id": case,
                "excerpt": excerpt.excerpt,
                "query": QUERY_FORMAT.format(excerpt=excerpt.excerpt),
            },
        ),
        [],
    )
    verify: TaskSpec = (
        "verify",
        _spec(
            "verify",
            f"Verify the claim for {case}",
            "Fetch the claimed URL respecting robots.txt, check "
            "the excerpt appears on the page, check the publisher "
            "against the domain and the date within one day, and "
            "report verdict (verified | contradicted | "
            "unverifiable) and checks.",
            {"case_id": case, "excerpt": excerpt.excerpt},
        ),
        ["find"],
    )
    approve: TaskSpec = (
        "approve",
        _spec(
            "approve",
            f"Approve the verified claim for {case}",
            "A person reviews the claim and the verdict side by "
            "side and approves or rejects (calibration sample).",
            {"case_id": case},
        ),
        ["verify"],
    )
    synthesize: TaskSpec = (
        "synthesize",
        _spec(
            "synthesize",
            f"Render the case answer for {case}",
            "Assemble the case answer from the claim and the "
            "verdict: verified claims become the answer; "
            "contradicted or unverifiable claims close the case "
            "as declined with the reason attached.",
            {"case_id": case},
        ),
        ["approve"] if approval_sampled else ["verify"],
    )

    spec = [find, verify]
    if approval_sampled:
        spec.append(approve)
    spec.append(synthesize)
    return spec


def build_seed_specs(
    excerpts: List[Excerpt], sampled: Set[int], project_name: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """Turn every case's chain into one flat seed-spec list.

    Parameters
    ----------
    excerpts : List[Excerpt]
        The selected excerpts, one case each.
    sampled : Set[int]
        Prompt numbers whose case gets a human approve task.
    project_name : str
        The project the cards belong to.

    Returns
    -------
    Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]
        The specs, with ``depends_on`` resolved to the dependency's
        name, and a map of case id to ``{role: task id}``.
    """
    specs: List[Dict[str, Any]] = []
    ids_by_case: Dict[str, Dict[str, str]] = {}
    for excerpt in excerpts:
        chain = build_case_spec(excerpt, excerpt.number in sampled, project_name)
        names_by_role = {role: spec["name"] for role, spec, _ in chain}
        ids_by_case[excerpt.case_id] = {role: spec["id"] for role, spec, _ in chain}
        for role, spec, depends_on_roles in chain:
            specs.append(
                {**spec, "depends_on": [names_by_role[r] for r in depends_on_roles]}
            )
    return specs, ids_by_case


async def call_marcus(url: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Call one Marcus tool over HTTP MCP and parse its JSON reply.

    Parameters
    ----------
    url : str
        Marcus HTTP MCP endpoint.
    name : str
        The tool name.
    arguments : Dict[str, Any]
        The tool arguments.

    Returns
    -------
    Dict[str, Any]
        The reply body, or ``{}`` when the reply carried no text.
    """
    client = Inspector(connection_type="http")
    async with client.connect(url=url):
        if client.session is None:
            raise RuntimeError("Not connected to Marcus")
        result = await client.session.call_tool(name, arguments=arguments)
    text = extract_text_from_result(result)
    parsed = json.loads(text) if text else {}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


async def seed_cases(
    csv_path: str,
    url: str,
    project_name: str,
    limit: int,
    approval_rate: float,
    seed: int,
    manifest_path: str,
    project_root: str,
    stratify: bool = False,
    call_tool: Optional[ToolCall] = None,
) -> Dict[str, Any]:
    """Create the project and its cards over MCP, and write the manifest.

    Parameters
    ----------
    csv_path : str
        The decrypted dataset CSV.
    url : str
        Marcus HTTP MCP endpoint of the running server.
    project_name : str
        The project's name; one project per finder vendor. Marcus
        returns the registry id, which the manifest records.
    limit : int
        Seed the first ``limit`` excerpts (50 for the pilot, 200 for
        the full run); 0 means all.
    approval_rate : float
        Fraction of cases given a human approve task.
    seed : int
        RNG seed for the approval sample.
    manifest_path : str
        Where to write the manifest JSON (ground truth, task ids,
        approval flags, the registry project id). Keep it out of the
        repository.
    project_root : str
        The directory Marcus files the project's artifacts under; the
        kernel refuses a sqlite project without one (#588). Created
        if missing. This chain writes nothing there.
    stratify : bool
        Select excerpts round-robin across publishers instead of
        first-N, so every publisher's crawler posture is represented.
    call_tool : Optional[ToolCall]
        The MCP call to use; defaults to :func:`call_marcus` against
        ``url``. Injected in tests.

    Returns
    -------
    Dict[str, Any]
        The manifest that was written.

    Raises
    ------
    RuntimeError
        Marcus refused the project (the reply body is in the message)
        or returned no project id.
    """
    excerpts = select_excerpts(load_excerpts(csv_path), limit, stratify)
    sampled = sample_approvals([e.number for e in excerpts], approval_rate, seed)
    specs, ids_by_case = build_seed_specs(excerpts, sampled, project_name)

    root = Path(project_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if call_tool is None:

        async def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            return await call_marcus(url, name, arguments)

    reply = await call_tool(
        "create_project",
        {
            "description": (
                f"On the Record: verify the AI-cited sources of {len(excerpts)} "
                "news excerpts through a find -> verify -> approve -> "
                "synthesize chain."
            ),
            "project_name": project_name,
            "options": {
                "provider": "sqlite",
                "complexity": "prototype",
                "project_root": str(root),
                "tasks": specs,
                "task_type_registrations": TASK_TYPE_REGISTRATIONS,
            },
        },
    )
    if not reply.get("success") or not reply.get("project_id"):
        raise RuntimeError(f"create_project refused the seeded board: {reply}")
    registry_id = str(reply["project_id"])

    cases: Dict[str, Any] = {}
    for excerpt in excerpts:
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
            "approval_sampled": excerpt.number in sampled,
            "tasks": ids_by_case[excerpt.case_id],
        }

    manifest = {
        "project_id": registry_id,
        "project_name": project_name,
        "url": url,
        "seed": seed,
        "approval_rate": approval_rate,
        "stratified": stratify,
        "tasks_seeded": len(specs),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def main() -> None:
    """Parse arguments and seed the cases over MCP."""
    parser = argparse.ArgumentParser(prog="tow_runner")
    parser.add_argument(
        "--dataset", required=True, help="Decrypted GenAISearch_Data.csv"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:4777/mcp",
        help="Marcus HTTP MCP endpoint of the running server (default: %(default)s)",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project name; one per finder vendor (e.g. tow-b-perplexity)",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help=(
            "Directory Marcus files the project under; defaults to "
            "<manifest dir>/project-root. The chain writes nothing there."
        ),
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
    args = parser.parse_args()
    project_root = args.project_root or str(
        Path(args.manifest).resolve().parent / "project-root"
    )

    manifest = asyncio.run(
        seed_cases(
            csv_path=args.dataset,
            url=args.url,
            project_name=args.project,
            limit=args.limit,
            approval_rate=args.approval_rate,
            seed=args.seed,
            manifest_path=args.manifest,
            project_root=project_root,
            stratify=args.stratify,
        )
    )
    total = len(manifest["cases"])
    approvals = sum(1 for c in manifest["cases"].values() if c["approval_sampled"])
    print(
        f"Seeded {total} cases ({approvals} with human approval, "
        f"{manifest['tasks_seeded']} tasks) over MCP at {args.url} as project "
        f"{args.project}; registry id {manifest['project_id']}; manifest at "
        f"{args.manifest}"
    )


if __name__ == "__main__":
    main()
