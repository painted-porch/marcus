"""
The seeder: one ``create_project`` call puts every chain on the board over MCP.

Per chain (one case in one mode): an ``advise`` card, a dependent ``verify``
card and, on a seeded sample, a dependent ``approve`` card. The cards carry
their ``task_type``, ``inputs`` and final ``id`` (issue #911), and the project
carries the chain's K2 declaration (``task_type_registrations``, issue #909),
with ``verifies_task_type: "advise"`` on ``verify`` so the kernel never offers
a verify card to the agent that produced its advise card, nor to that agent's
vendor (ADR-0013 K12).

The mode is never on the board. The advise card's inputs name the chain by an
opaque id; the adviser process holds the id-to-instruction map locally, and the
grader joins the id to the manifest afterwards.
"""

from __future__ import annotations

import random
import re
from typing import Any

from marcus_toolkit.client import MarcusMCPClient
from marcus_toolkit.seed import FORBIDDEN_LABELS, PRIORITIES

from .cases import Case

#: The chain's ADR-0013 K2 declaration.
TASK_TYPE_REGISTRATIONS: dict[str, dict[str, Any]] = {
    "advise": {
        "evidence_schema": {"type": "object", "required": ["recommendation"]},
        "checks_injectable": False,
    },
    "verify": {
        "evidence_schema": {
            "type": "object",
            "required": ["verdict", "reason", "source"],
        },
        "verifies_task_type": "advise",
        "checks_injectable": False,
    },
    "approve": {
        "evidence_schema": {"type": "object", "required": ["decision", "approved_by"]},
        "checks_injectable": False,
    },
}

#: The keys the kernel's seed-spec validator accepts
#: (``src/integrations/seeded_tasks.py``).
ALLOWED_KEYS = frozenset(
    {
        "name",
        "description",
        "labels",
        "depends_on",
        "completion_criteria",
        "estimated_hours",
        "priority",
        "task_type",
        "inputs",
        "id",
    }
)

_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def task_id_for(project_name: str, chain: str, role: str) -> str:
    """
    Return a card's final board id.

    Parameters
    ----------
    project_name : str
        The project; several projects may share one board file.
    chain : str
        The opaque chain id.
    role : str
        ``advise``, ``verify`` or ``approve``.

    Returns
    -------
    str
        An id over ``[A-Za-z0-9_-]``.
    """
    return _ID_SAFE.sub("-", f"{project_name}-{chain}-{role}").strip("-")


def sample_approvals(chains: list[str], rate: float, seed: int) -> set[str]:
    """
    Choose which chains carry a human approve card.

    Parameters
    ----------
    chains : List[str]
        Chain ids in seeding order.
    rate : float
        Fraction to sample, 0 to 1.
    seed : int
        The RNG seed, so the sample is reproducible.

    Returns
    -------
    Set[str]
        The sampled chain ids.
    """
    if rate <= 0 or not chains:
        return set()
    count = max(1, round(len(chains) * rate)) if rate < 1 else len(chains)
    rng = random.Random(seed)
    return set(rng.sample(sorted(chains), min(count, len(chains))))


def build_chain_specs(
    case: Case, chain: str, approval_sampled: bool, project_name: str
) -> list[dict[str, Any]]:
    """
    Build one chain's cards as seed specs, ``depends_on`` resolved by name.

    Parameters
    ----------
    case : Case
        The case; only its vignette and draft go onto the board.
    chain : str
        The opaque chain id.
    approval_sampled : bool
        Whether this chain gets an approve card.
    project_name : str
        Part of every card id.

    Returns
    -------
    List[Dict[str, Any]]
        Two or three specs in dependency order.
    """
    common = {
        "priority": "high",
        "estimated_hours": 0.1,
        "labels": ["hospital", f"case:{chain}"],
    }
    advise_name = f"Advise {chain}"
    verify_name = f"Verify {chain}"
    specs: list[dict[str, Any]] = [
        {
            **common,
            "id": task_id_for(project_name, chain, "advise"),
            "name": advise_name,
            "description": (
                "Read the physician's draft decision in inputs and report your "
                "recommendation as evidence: recommendation."
            ),
            "task_type": "advise",
            "inputs": {
                "chain_id": chain,
                "vignette": case.vignette,
                "physician_draft": case.physician_draft,
            },
        },
        {
            **common,
            "id": task_id_for(project_name, chain, "verify"),
            "name": verify_name,
            "description": (
                "Check the recommendation in inputs against the record and the "
                "standard of care; report verdict, reason and source as evidence. "
                "You never see the conversation."
            ),
            "task_type": "verify",
            "inputs": {"chain_id": chain},
            "depends_on": [advise_name],
        },
    ]
    if approval_sampled:
        specs.append(
            {
                **common,
                "id": task_id_for(project_name, chain, "approve"),
                "name": f"Approve {chain}",
                "description": (
                    "Human sign-off standing in for the physician's final "
                    "decision; report decision and approved_by as evidence."
                ),
                "task_type": "approve",
                "inputs": {"chain_id": chain},
                "depends_on": [verify_name],
            }
        )
    return specs


def build_seed_specs(
    chains: list[tuple[Case, str]], sampled: set[str], project_name: str
) -> list[dict[str, Any]]:
    """
    Flatten every chain into one seed-spec list.

    Parameters
    ----------
    chains : List[Tuple[Case, str]]
        ``(case, chain_id)`` pairs in seeding order.
    sampled : Set[str]
        Chain ids that carry an approve card.
    project_name : str
        Part of every card id.

    Returns
    -------
    List[Dict[str, Any]]
    """
    specs: list[dict[str, Any]] = []
    for case, chain in chains:
        specs.extend(build_chain_specs(case, chain, chain in sampled, project_name))
    return specs


def validate_specs(specs: list[dict[str, Any]]) -> None:
    """
    Refuse a spec list the board would mis-handle, before the call.

    Mirrors ``marcus_toolkit.seed.validate_specs`` with the typed-chain keys
    the toolkit does not accept yet.

    Parameters
    ----------
    specs : List[Dict[str, Any]]
        The specs.

    Raises
    ------
    ValueError
        On an unknown key, a forbidden label, a bad priority, a duplicate
        name or id, or a dependency naming no task.
    """
    names = [str(s.get("name") or "") for s in specs]
    ids = [str(s.get("id") or "") for s in specs]
    if len(set(names)) != len(names) or any(not n for n in names):
        raise ValueError("every card needs a distinct, non-empty name")
    if len(set(ids)) != len(ids) or any(not i for i in ids):
        raise ValueError("every card needs a distinct, non-empty id")
    for spec in specs:
        unknown = set(spec) - ALLOWED_KEYS
        if unknown:
            raise ValueError(f"card {spec['name']!r}: unknown keys {sorted(unknown)}")
        bad = {
            str(label).lower() for label in spec.get("labels") or []
        } & FORBIDDEN_LABELS
        if bad:
            raise ValueError(
                f"card {spec['name']!r}: label(s) {sorted(bad)} switch on the "
                "software validation gate (K3)"
            )
        if spec.get("priority", "medium") not in PRIORITIES:
            raise ValueError(f"card {spec['name']!r}: bad priority")
        if spec.get("task_type") not in TASK_TYPE_REGISTRATIONS:
            raise ValueError(f"card {spec['name']!r}: undeclared task_type")
        for dep in spec.get("depends_on") or []:
            if dep not in names:
                raise ValueError(
                    f"card {spec['name']!r}: depends_on {dep!r} names no card"
                )


def seed_board(
    client: MarcusMCPClient,
    *,
    project_name: str,
    description: str,
    specs: list[dict[str, Any]],
    project_root: str,
) -> dict[str, Any]:
    """
    Create the project and its cards with one ``create_project`` call.

    Parameters
    ----------
    client : MarcusMCPClient
        A connected client.
    project_name : str
        The project name; never contains "test".
    description : str
        The project description.
    specs : List[Dict[str, Any]]
        The validated seed specs.
    project_root : str
        The directory Marcus files artifacts under; the kernel refuses a sqlite
        project without one.

    Returns
    -------
    Dict[str, Any]
        The reply, carrying ``project_id``.

    Raises
    ------
    ValueError
        On a bad project name or spec list.
    RuntimeError
        When the server refuses the project.
    """
    if "test" in project_name.lower():
        raise ValueError("a project name containing 'test' changes Marcus's behaviour")
    validate_specs(specs)
    reply = client.call(
        "create_project",
        {
            "description": description,
            "project_name": project_name,
            "options": {
                "provider": "sqlite",
                "complexity": "prototype",
                "project_root": project_root,
                "tasks": specs,
                "task_type_registrations": TASK_TYPE_REGISTRATIONS,
            },
        },
    )
    if not reply.get("success") or not reply.get("project_id"):
        raise RuntimeError(f"create_project refused the seeded board: {reply}")
    created: dict[str, Any] = dict(reply)
    return created
