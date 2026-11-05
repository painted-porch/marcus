"""Seed numeric-verifier cases onto a Marcus board (POC runner, step 4).

Writes one case's five-task chain directly into a SQLite kanban board
for each staged case directory, then registers the board in Marcus's
project registry. This mirrors ``tow_runner.py``'s precedent exactly,
for the same reason: seeding is setup, not coordination, and writing
the board directly means seeding never needs admin MCP credentials,
which is a worse thing to hand someone reproducing the trial than a
plain board-writer script.

The five tasks per case, each depending on the one before it:

    find        the writer produces report.md + claims.json
    verify      the checker resolves every claim, no model involved
    approve     a person accepts or rejects (sampled cases only)
    synthesize  the slide worker assembles slides.json from accepted
                claims
    verify      the checker re-runs in slide-mapping mode

Ground truth (the manifest's ``truth`` and ``traps`` fields written by
``numeric_stage.py``) never reaches the board: only ``staged_dir``
goes into a task's ``inputs``, so a worker that could read the answer
off the board is not being tested.

Usage::

    python dev-tools/experiments/runners/numeric_runner.py \
        --staged-root ~/marcus-experiments/numeric-verifier/staged \
        --db ~/marcus-experiments/numeric-verifier/board/kanban.db \
        --project-id numeric-verifier-b \
        --manifest ~/marcus-experiments/numeric-verifier/manifest.json \
        --approval-rate 0.2 --seed 737
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.project_registry import ProjectConfig, ProjectRegistry  # noqa: E402
from src.integrations.providers.sqlite_kanban import SQLiteKanban  # noqa: E402

#: One task spec: (role, task_data without dependencies, depends-on roles).
TaskSpec = Tuple[str, Dict[str, Any], List[str]]


def sample_approvals(case_ids: List[str], rate: float, seed: int) -> Set[str]:
    """Pick a deterministic subset of cases for human approval.

    Parameters
    ----------
    case_ids : List[str]
        Every case id, in a stable order.
    rate : float
        Fraction to sample, 0.0 to 1.0.
    seed : int
        RNG seed, so a rerun samples the same cases.

    Returns
    -------
    Set[str]
        The sampled case ids.
    """
    import random

    if rate <= 0:
        return set()
    rng = random.Random(seed)
    ordered = sorted(case_ids)
    n = max(1, round(len(ordered) * rate)) if ordered else 0
    return set(rng.sample(ordered, min(n, len(ordered))))


def discover_cases(staged_root: Path) -> List[Tuple[str, Path]]:
    """Find every staged case under a root, newest evaluation per case.

    ``numeric_stage.py`` lays out ``<root>/<case_id>/eval_<timestamp>``,
    and a trap can add a stale sibling evaluation beside the current
    one (the ``stale_sibling`` trap). The newest directory by name is
    always the current evaluation.

    Parameters
    ----------
    staged_root : Path
        Directory holding one subdirectory per case.

    Returns
    -------
    List[Tuple[str, Path]]
        ``(case_id, staged_dir)`` pairs, sorted by case id.
    """
    found = []
    for case_dir in sorted(staged_root.iterdir()):
        if not case_dir.is_dir():
            continue
        evals = sorted(case_dir.glob("eval_*"))
        if not evals:
            continue
        found.append((case_dir.name, evals[-1]))
    return found


def build_case_spec(
    case_id: str,
    staged_dir: Path,
    approval_sampled: bool,
    project_id: str,
) -> List[TaskSpec]:
    """Build the five-task chain for one case.

    Parameters
    ----------
    case_id : str
        The case's identifier, e.g. ``case-003``.
    staged_dir : Path
        The evaluation directory the report is written from. Only
        its path goes onto the board; nothing inside is ground truth.
    approval_sampled : bool
        True when this case gets a human approve task between the
        first verify and synthesize.
    project_id : str
        Project id every task carries.

    Returns
    -------
    List[TaskSpec]
        Ordered ``(role, task_data, depends_on_roles)`` triples. Two
        tasks share the role ``verify``, so roles are disambiguated
        by position (``verify_report``, ``verify_slides``) when
        wiring dependencies.
    """
    staged = str(staged_dir)
    labels = ["numeric-verifier", f"case:{case_id}"]
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
            "name": f"Write the report for {case_id}",
            "description": (
                "Read the staged evaluation named in inputs['staged_dir'] "
                "and write report.md plus claims.json: a claim for every "
                "number the report states, with the address where it can "
                "be checked."
            ),
            "task_type": "find",
            "inputs": {"case_id": case_id, "staged_dir": staged},
            "output_schema": {
                "type": "object",
                "required": ["report_path", "claims_path"],
            },
        },
        [],
    )
    verify_report: TaskSpec = (
        "verify_report",
        {
            **common,
            "name": f"Check the report's numbers for {case_id}",
            "description": (
                "Resolve every claim against the staged data, add the "
                "cross-checks the harness requires regardless of what "
                "was declared, and require a claim for every figure in "
                "the report text. Report verdict and failed_claims."
            ),
            "task_type": "verify",
            "inputs": {"case_id": case_id, "staged_dir": staged},
            "output_schema": {
                "type": "object",
                "required": ["verdict", "failed_claims"],
            },
        },
        ["find"],
    )
    approve: TaskSpec = (
        "approve",
        {
            **common,
            "name": f"Approve the verified report for {case_id}",
            "description": (
                "A person reviews the report and its verdict side by "
                "side and approves or rejects (calibration sample)."
            ),
            "task_type": "approve",
            "inputs": {"case_id": case_id},
        },
        ["verify_report"],
    )
    synthesize: TaskSpec = (
        "synthesize",
        {
            **common,
            "name": f"Build the slide spec for {case_id}",
            "description": (
                "Assemble slides.json from the accepted claims only. "
                "Every numeric cell carries the id of a claim that "
                "already verified; no new figure may be computed here."
            ),
            "task_type": "synthesize",
            "inputs": {"case_id": case_id, "staged_dir": staged},
            "output_schema": {
                "type": "object",
                "required": ["slides_path"],
            },
        },
        ["approve"] if approval_sampled else ["verify_report"],
    )
    verify_slides: TaskSpec = (
        "verify_slides",
        {
            **common,
            "name": f"Check the slide spec for {case_id}",
            "description": (
                "Confirm every slide figure maps to an accepted claim, "
                "matches its value, and sits under its own label. No "
                "re-auditing against the staged data: a figure not "
                "traceable to an accepted claim fails regardless of "
                "whether it happens to be correct."
            ),
            "task_type": "verify",
            "inputs": {"case_id": case_id},
        },
        ["synthesize"],
    )

    spec = [find, verify_report]
    if approval_sampled:
        spec.append(approve)
    spec.append(synthesize)
    spec.append(verify_slides)
    return spec


async def seed_cases(
    staged_root: str,
    db_path: str,
    project_id: str,
    approval_rate: float,
    seed: int,
    manifest_path: str,
    truth_manifest: Optional[str] = None,
) -> Dict[str, Any]:
    """Seed every staged case onto the board and write the run manifest.

    Parameters
    ----------
    staged_root : str
        Root directory of staged cases, as laid out by
        ``numeric_stage.py``.
    db_path : str
        SQLite board file (created if absent).
    project_id : str
        Project id for every task.
    approval_rate : float
        Fraction of cases given a human approve task.
    seed : int
        RNG seed for the approval sample.
    manifest_path : str
        Where to write the run manifest (task ids per case, approval
        flags). This manifest holds no ground truth and is safe to
        keep beside the board.
    truth_manifest : Optional[str]
        Path to ``numeric_stage.py``'s manifest, which does hold
        ground truth. When given, each case's truth is cross-
        referenced by id for the grader to join later; it is never
        written to the board-adjacent manifest or to any task.

    Returns
    -------
    Dict[str, Any]
        The run manifest that was written.
    """
    cases_on_disk = discover_cases(Path(staged_root).expanduser())
    case_ids = [c for c, _ in cases_on_disk]
    sampled = sample_approvals(case_ids, approval_rate, seed)

    board = SQLiteKanban(
        {
            "db_path": db_path,
            "project_name": project_id,
            "attachments_dir": str(Path(db_path).parent / "attachments"),
        }
    )
    await board.connect()

    cases: Dict[str, Any] = {}
    for case_id, staged_dir in cases_on_disk:
        approval_sampled = case_id in sampled
        spec = build_case_spec(case_id, staged_dir, approval_sampled, project_id)
        ids_by_role: Dict[str, str] = {}
        for role, task_data, depends_on in spec:
            task_data = dict(task_data)
            task_data["dependencies"] = [ids_by_role[r] for r in depends_on]
            created = await board.create_task(task_data)
            ids_by_role[role] = created.id
        cases[case_id] = {
            "staged_dir": str(staged_dir),
            "approval_sampled": approval_sampled,
            "tasks": ids_by_role,
        }

    await board.disconnect()

    truth_by_case: Dict[str, Any] = {}
    if truth_manifest:
        raw = json.loads(Path(truth_manifest).expanduser().read_text())
        for entry in raw.get("cases", []):
            truth_by_case[entry["case_id"]] = {
                "traps": entry.get("traps", []),
            }
    for case_id, info in cases.items():
        info["traps"] = truth_by_case.get(case_id, {}).get("traps", [])

    manifest = {
        "project_id": project_id,
        "db_path": str(Path(db_path).resolve()),
        "seed": seed,
        "approval_rate": approval_rate,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }
    out_path = Path(manifest_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=1))
    return manifest


async def register_board(
    project_id: str, db_path: str, registry: "ProjectRegistry | None" = None
) -> str:
    """Register a seeded board in Marcus's project registry.

    Seeding writes tasks straight into the SQLite board, which
    bypasses ``create_project``. Without this step the registry never
    learns the board exists. Idempotent on the pair of project name
    and absolute db path, so re-seeding never duplicates an entry.

    Parameters
    ----------
    project_id : str
        The board's project id, also the kanban project name.
    db_path : str
        SQLite board file; stored absolute so the entry stays valid
        from any working directory.
    registry : ProjectRegistry | None
        Injected in tests; defaults to Marcus's file-backed registry.

    Returns
    -------
    str
        The registry project id.
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
        tags=["auto-created", "sqlite", "numeric-verifier"],
    )
    return await registry.add_project(config)


def build_parser() -> argparse.ArgumentParser:
    """Build the seeding runner's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_runner")
    p.add_argument("--staged-root", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--truth-manifest", default=None)
    p.add_argument("--approval-rate", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=737)
    p.add_argument("--skip-register", action="store_true")
    return p


async def _main(args: argparse.Namespace) -> int:
    """Seed the board and register it.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed arguments.

    Returns
    -------
    int
        Process exit code.
    """
    manifest = await seed_cases(
        staged_root=args.staged_root,
        db_path=args.db,
        project_id=args.project_id,
        approval_rate=args.approval_rate,
        seed=args.seed,
        manifest_path=args.manifest,
        truth_manifest=args.truth_manifest,
    )
    print(f"seeded {len(manifest['cases'])} cases -> {args.db}")
    sampled = sum(1 for c in manifest["cases"].values() if c["approval_sampled"])
    print(f"approval-sampled: {sampled}/{len(manifest['cases'])}")
    print(f"manifest -> {args.manifest}")

    if not args.skip_register:
        registry_id = await register_board(args.project_id, args.db)
        print(f"registered as project {registry_id}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and run the seeding coroutine.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
