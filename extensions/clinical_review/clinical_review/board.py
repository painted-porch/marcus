"""
Driving one board arm end to end: seed, run the agents, export the bundles.

Both board arms (A2 and A3) use this. The difference is the vendor strings the
two agents register with, which is what decides whether the kernel's K12
vendor rule is in force, and which model the verifier calls.

The adviser agent keeps asking for work after its advise cards are closed, so
every refusal of a verify card is written to the kernel's refusal log, which
is the evidence that the silo held. The exported bundle carries those refusals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from marcus_toolkit.client import MarcusMCPClient

from .agents import (
    AgentConfig,
    Board,
    LoopStats,
    adviser_handler,
    approver_handler,
    pull_loop,
    verifier_handler,
)
from .cases import Case
from .models import Metered
from .prompts import chain_id
from .seed import build_seed_specs, sample_approvals, seed_board


@dataclass
class ArmResult:
    """
    What one board arm produced.

    Parameters
    ----------
    arm : str
        ``A2`` or ``A3``.
    project_id : str
        The registry project id.
    adviser : LoopStats
        The adviser loop's counters.
    verifier : LoopStats
        The verifier loop's counters.
    approver : Optional[LoopStats]
        The human stand-in's counters, when any chain was sampled.
    bundles : List[Dict[str, Any]]
        One exported audit bundle per chain.
    refusals : List[Dict[str, Any]]
        Every eligibility refusal the bundles carry, deduplicated by task.
    """

    arm: str
    project_id: str
    adviser: LoopStats
    verifier: LoopStats
    approver: LoopStats | None
    bundles: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)


def chains_for(cases: list[Case], salt: str) -> list[tuple[Case, str, str]]:
    """
    Build every ``(case, chain_id, mode)`` triple, aligned then adversarial.

    Parameters
    ----------
    cases : List[Case]
        The cases.
    salt : str
        The run's chain-id salt.

    Returns
    -------
    List[Tuple[Case, str, str]]
    """
    triples: list[tuple[Case, str, str]] = []
    for case in cases:
        for mode in ("aligned", "adversarial"):
            triples.append((case, chain_id(case.case_id, mode, salt), mode))
    return triples


def run_board_arm(
    client: MarcusMCPClient,
    *,
    arm: str,
    cases: list[Case],
    salt: str,
    recommendations: dict[str, dict[str, Any]],
    checker: Metered,
    project_name: str,
    project_root: Path,
    state_dir: Path,
    adviser_vendor: str,
    verifier_vendor: str,
    approval_rate: float = 0.2,
    seed: int = 918,
    sleep: Callable[[float], None] | None = None,
) -> ArmResult:
    """
    Seed one arm's board, run its three agents, and export every chain.

    Parameters
    ----------
    client : MarcusMCPClient
        A connected client for the server under test.
    arm : str
        ``A2`` (vendor rule off: both agents register with an empty vendor) or
        ``A3`` (vendor rule in force).
    cases : List[Case]
        The cases.
    salt : str
        The chain-id salt.
    recommendations : Dict[str, Dict[str, Any]]
        Chain id to the generated recommendation the adviser replays.
    checker : Metered
        The verifier's model behind the ledger.
    project_name : str
        The arm's project name; never contains "test".
    project_root : Path
        Where Marcus files artifacts.
    state_dir : Path
        Where the agents' board-call logs go.
    adviser_vendor : str
        The adviser's declared vendor.
    verifier_vendor : str
        The verifier's declared vendor.
    approval_rate : float
        Fraction of chains that get a human approve card.
    seed : int
        The approval sample's seed.
    sleep : Optional[Callable[[float], None]]
        Passed to the loops; tests pass a stub.

    Returns
    -------
    ArmResult
    """
    triples = chains_for(cases, salt)
    pairs = [(case, chain) for case, chain, _mode in triples]
    chain_ids = [chain for _case, chain, _mode in triples]
    sampled = sample_approvals(chain_ids, approval_rate, seed)
    specs = build_seed_specs(pairs, sampled, project_name)
    project_root.mkdir(parents=True, exist_ok=True)
    reply = seed_board(
        client,
        project_name=project_name,
        description=(
            f"Hospital arm {arm}: an advise -> verify -> approve chain per clinical "
            "decision point, the verifier sealed from the dialogue (issue #918)."
        ),
        specs=specs,
        project_root=str(project_root.resolve()),
    )
    project_id = str(reply["project_id"])

    def logger(name: str) -> Callable[[dict[str, Any]], None]:
        path = state_dir / name / "board-calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)

        def log(record: dict[str, Any]) -> None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        return log

    kwargs: dict[str, Any] = {} if sleep is None else {"sleep": sleep}

    adviser_id = f"adviser-{arm.lower()}"
    verifier_id = f"verifier-{arm.lower()}"
    reviewer_id = f"reviewer-{arm.lower()}"

    adviser_stats = pull_loop(
        Board(client, logger(adviser_id)),
        AgentConfig(adviser_id, project_id, "adviser", vendor=adviser_vendor),
        {"advise": adviser_handler(recommendations)},
        **kwargs,
    )
    verifier_stats = pull_loop(
        Board(client, logger(verifier_id)),
        AgentConfig(verifier_id, project_id, "verifier", vendor=verifier_vendor),
        {"verify": verifier_handler(checker, arm)},
        **kwargs,
    )
    approver_stats: LoopStats | None = None
    if sampled:
        approver_stats = pull_loop(
            Board(client, logger(reviewer_id)),
            AgentConfig(reviewer_id, project_id, "reviewer", principal="human"),
            {"approve": approver_handler(reviewer_id)},
            **kwargs,
        )
        # An agent principal is never offered an approve card, and the board is
        # not drained while those cards are open, so the verifier's own loop
        # exits on idle rather than on the drain. Ask once more now that the
        # approvals are closed, which is also how the citation chain finishes.
        after_approvals = pull_loop(
            Board(client, logger(verifier_id)),
            AgentConfig(
                verifier_id,
                project_id,
                "verifier",
                vendor=verifier_vendor,
                idle_exits=2,
            ),
            {"verify": verifier_handler(checker, arm)},
            **kwargs,
        )
        verifier_stats.completed += after_approvals.completed
        verifier_stats.declined += after_approvals.declined
        verifier_stats.polls += after_approvals.polls
        verifier_stats.drained = verifier_stats.drained or after_approvals.drained
    # The adviser asks again after the verify cards exist, so the kernel's
    # refusal of each one is on the record rather than merely implied.
    second_pass = pull_loop(
        Board(client, logger(adviser_id)),
        AgentConfig(
            adviser_id, project_id, "adviser", vendor=adviser_vendor, idle_exits=2
        ),
        {"advise": adviser_handler(recommendations)},
        **kwargs,
    )
    adviser_stats.polls += second_pass.polls
    adviser_stats.drained = adviser_stats.drained or second_pass.drained

    board = Board(client, logger("export"))
    bundles: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    for chain in chain_ids:
        bundle = board.export_case(chain)
        if bundle.get("case_id") or bundle.get("tasks"):
            bundles.append(bundle)
            refusals.extend(bundle.get("refusals") or [])
    return ArmResult(
        arm=arm,
        project_id=project_id,
        adviser=adviser_stats,
        verifier=verifier_stats,
        approver=approver_stats,
        bundles=bundles,
        refusals=refusals,
    )


def refusals_naming_the_adviser(
    refusals: list[dict[str, Any]], adviser_id: str
) -> list[dict[str, Any]]:
    """
    Keep the refusals that show the adviser being denied a verify card.

    Parameters
    ----------
    refusals : List[Dict[str, Any]]
        Refusal rows from the bundles.
    adviser_id : str
        The adviser agent's id.

    Returns
    -------
    List[Dict[str, Any]]
    """
    return [
        r
        for r in refusals
        if str(r.get("agent_id") or "") == adviser_id
        and "author" in str(r.get("reason") or "").lower()
    ]
