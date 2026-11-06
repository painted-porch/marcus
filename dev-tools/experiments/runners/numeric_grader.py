"""Label-blind grader for the numeric-verifier trial (POC runner, step 8).

Scores every case from every condition against the ground truth
``numeric_stage.py`` wrote to its manifest, and reports the six
preregistered rates: wrong figures shipped (by claim kind), false
declines, claim coverage, trap catch rate (by trap type), rounds per
case, and cost per case.

Label-blindness is mechanical, mirroring ``tow_grader.py``'s
precedent: every record from every condition is normalized into one
shape, assigned a blind id, shuffled with a fixed seed, and scored by
one function that never sees which condition produced it. Rates are
computed afterward by rejoining on the blind id, so nothing about
"which condition is this" can influence a single scoring decision.

Ground truth lives in the staging manifest, which never touches the
board; this grader is the only consumer of it outside the trap
generator itself.

Known limitation: ``trap_catch_rate`` cannot yet distinguish "the
checker missed the planted defect" from "the writer never created
the opportunity the trap relies on." The rounding-ratio trap only
bites if the report states a ratio at all; a writer that states none
registers as 0.0 catch rate here, identical to a checker that failed
to catch a stated one. Resolving this needs inspecting the writer's
own claims for the specific assertion each trap targets, which is
left for a later pass; readers of this grader's output should treat
a low catch rate as "investigate," not "the checker is weak."
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.numeric_claims import (  # noqa: E402
    VERDICT_FAILED,
    VERDICT_UNCOVERED,
    VERDICT_UNRESOLVABLE,
    VERDICT_VERIFIED,
)

CONDITION_A = "A"
CONDITION_B = "B"
CONDITION_C = "C"


@dataclass
class ClaimRecord:
    """One claim's outcome, stripped to what the grader needs.

    Parameters
    ----------
    claim_id : str
        The claim's identifier, or a synthetic id for a coverage gap
        or a harness-required check.
    kind : str
        ``lookup``, ``derived``, ``consistency``, or ``coverage`` for
        a figure with no claim behind it.
    verdict : str
        One of the checker's verdict strings.
    stated : Any
        The value as the report (or slide) stated it.
    resolved : Any
        The value the staged data supports, when resolvable.
    reason : str
        What kind of failure this is, when ``verdict`` is
        ``"failed"`` -- ``"value_mismatch"`` or ``"label_mismatch"``.
        A false decline is only a correct figure rejected on its
        value; a label-mismatch rejection is the checker working as
        intended (right number, wrong row) and must not count as one.
    """

    claim_id: str
    kind: str
    verdict: str
    stated: Any = None
    resolved: Any = None
    reason: str = ""


@dataclass
class CaseRecord:
    """One case's outcome from one condition, normalized for grading.

    Parameters
    ----------
    case_id : str
        The staged case this run answered.
    condition : str
        ``A``, ``B``, or ``C``.
    claims : List[ClaimRecord]
        Every claim's final outcome, after any retries.
    rounds : int
        How many write-then-check rounds the case went through.
    declined : bool
        True when the case closed as declined after exhausting its
        retry budget on a board. False for a one-shot check (no
        board retry involved) even if that check failed: ``declined``
        is specifically the exhausted-retries outcome, not "failed
        verification." See ``verified_clean``.
    verified_clean : bool
        True when the checker's overall verdict was ``verified`` on
        this attempt, whether or not a board ever retried it. This,
        not ``declined``, is what "the checker caught something"
        means for a single check, since most of this trial's
        evidence so far is one-shot writer-then-check runs with no
        board wiring yet.
    cost_usd : float
        Model spend attributed to this case, zero for a no-model
        checker run.
    """

    case_id: str
    condition: str
    claims: List[ClaimRecord] = field(default_factory=list)
    rounds: int = 1
    declined: bool = False
    verified_clean: bool = True
    cost_usd: float = 0.0


def load_condition_b(
    rehearsal_root: Path, rounds_by_case: Optional[Dict[str, int]] = None
) -> List[CaseRecord]:
    """Load Condition B records from scored writer-rehearsal workspaces.

    Parameters
    ----------
    rehearsal_root : Path
        Directory holding one subdirectory per case, each already
        checked by :func:`runners.numeric_verify.verify_workspace`.
    rounds_by_case : Optional[Dict[str, int]]
        Rounds spent per case, when the board ran bounded retry; a
        case absent from this map is assumed to have passed first
        try (one round).

    Returns
    -------
    List[CaseRecord]
        One record per case directory found.
    """
    from runners.numeric_verify import verify_workspace

    rounds_by_case = rounds_by_case or {}
    records: List[CaseRecord] = []
    for case_dir in sorted(Path(rehearsal_root).expanduser().iterdir()):
        # A case is identified by having a workspace at all (staged/,
        # which build_workspace always creates), not by whether the
        # writer went on to produce claims.json -- a writer that
        # failed to produce output is a real, gradeable outcome, not
        # a reason to drop the case from every rate's denominator.
        if not case_dir.is_dir() or not (case_dir / "staged").exists():
            continue
        outcome = verify_workspace(case_dir)
        claims = [
            ClaimRecord(
                claim_id=r.claim_id,
                kind=_kind_of(r.claim_id),
                verdict=r.verdict,
                stated=r.value,
                resolved=r.expected,
                reason=r.reason,
            )
            for r in outcome["results"]
        ]
        rounds = rounds_by_case.get(case_dir.name, 1)
        records.append(
            CaseRecord(
                case_id=case_dir.name,
                condition=CONDITION_B,
                claims=claims,
                rounds=rounds,
                declined=outcome["verdict"] != VERDICT_VERIFIED and rounds >= 3,
                verified_clean=outcome["verdict"] == VERDICT_VERIFIED,
            )
        )
    return records


def load_condition_a(results_dir: Path) -> List[CaseRecord]:
    """Load Condition A records: writer plus same-model self-check.

    Not yet implemented. Condition A needs its own runner, a writer
    whose checking loop shares the writer's model and conversation
    context, which has not been built; this trial's writer sessions
    so far have all run as Condition B (a harness-authored checker,
    no model in the loop). Left as an explicit gap rather than a
    silent stub returning no records, since an empty Condition A
    would otherwise look identical to "ran clean."

    Parameters
    ----------
    results_dir : Path
        Reserved for the Condition A runner's output directory.

    Raises
    ------
    NotImplementedError
        Always, until the Condition A runner exists.
    """
    raise NotImplementedError(
        "Condition A (same-model self-check, no board) has no runner yet; "
        "see PROTOCOL.md's conditions section"
    )


def load_condition_c(results_dir: Path) -> List[CaseRecord]:
    """Load Condition C records: the same checker, no record-keeping.

    Not yet implemented; depends on Condition A's output, since
    Condition C applies :mod:`runners.numeric_claims` to Condition
    A's finished reports as a post-hoc filter.

    Parameters
    ----------
    results_dir : Path
        Reserved for the Condition C runner's output directory.

    Raises
    ------
    NotImplementedError
        Always, until Condition A exists for it to filter.
    """
    raise NotImplementedError(
        "Condition C (checker with no record) depends on Condition A's "
        "output, which does not exist yet"
    )


def _kind_of(claim_id: str) -> str:
    """Classify a claim id for the per-kind breakdown.

    Parameters
    ----------
    claim_id : str
        A claim's identifier, possibly one of the grader's synthetic
        prefixes.

    Returns
    -------
    str
        ``coverage`` for an uncovered-figure id, ``harness`` for a
        required cross-check, otherwise ``declared`` (the caller's
        own claim, whose kind the grader does not need to split
        further for the rates it reports).
    """
    if claim_id.startswith("uncovered:"):
        return "coverage"
    if claim_id.startswith("required:"):
        return "harness"
    return "declared"


def blind_pool(records: List[CaseRecord], seed: int) -> List[Tuple[str, CaseRecord]]:
    """Strip conditions and shuffle: the scorer's view.

    Parameters
    ----------
    records : List[CaseRecord]
        Every case from every condition.
    seed : int
        Shuffle seed, fixed so the ordering is reproducible.

    Returns
    -------
    List[Tuple[str, CaseRecord]]
        ``(blind_id, record)`` pairs. The record still carries
        ``condition`` internally; the scoring function below is
        written to never read that field, and the rejoin at the end
        is what the grader itself does, not the scorer.
    """
    rng = random.Random(seed)
    order = list(range(len(records)))
    rng.shuffle(order)
    return [
        (f"blind-{position:05d}", records[index])
        for position, index in enumerate(order)
    ]


def score_case(record: CaseRecord, truth: Dict[str, Any]) -> Dict[str, Any]:
    """Score one case against its ground truth.

    This function is the label-blind boundary: it is handed a case
    record and the truth for that case's id, and nothing else. It
    cannot see which condition produced the record, because the
    caller never passes that in.

    Parameters
    ----------
    record : CaseRecord
        A case's claims, stripped of condition.
    truth : Dict[str, Any]
        This case's entry from the staging manifest: ``traps``
        (planted defect names) and whatever else the manifest holds.

    Returns
    -------
    Dict[str, Any]
        ``wrong_shipped`` (claims verified that should have failed,
        there are none by construction once the checker ran, so this
        is always empty for Condition B and is the live question for
        A), ``false_declines`` (claims failed that were in fact
        correct), ``coverage_gaps`` (uncovered figures), and
        ``traps_present`` echoed back for the catch-rate join.
    """
    wrong_shipped = [
        c
        for c in record.claims
        if c.verdict == VERDICT_VERIFIED
        and c.resolved is not None
        and _materially_different(c.stated, c.resolved)
    ]
    false_declines = [
        c
        for c in record.claims
        if c.verdict in (VERDICT_FAILED, VERDICT_UNRESOLVABLE)
        and c.reason != "label_mismatch"
        and c.resolved is not None
        and not _materially_different(c.stated, c.resolved)
    ]
    coverage_gaps = [c for c in record.claims if c.verdict == VERDICT_UNCOVERED]
    return {
        "wrong_shipped": wrong_shipped,
        "false_declines": false_declines,
        "coverage_gaps": coverage_gaps,
        "traps_present": _trap_names(truth.get("traps", [])),
        "declined": record.declined,
        "verified_clean": record.verified_clean,
    }


#: A stated figure within this fraction of the truth's own magnitude
#: is ordinary rounding, not an error. Reports state numbers to a
#: couple of decimals, and a checker correctly honoring a declared
#: tolerance will often leave ``stated != resolved`` by a tiny
#: margin; grading on bit-exact equality would flag every one of
#: those as a wrong figure shipped, which they are not. Scaled to
#: the truth's magnitude rather than a flat amount: a flat 0.1, large
#: enough to be reasonable rounding noise for a percentage-scale
#: figure (94.0 vs 93.97), is roughly 20% of a fractional metric like
#: R2 (bounded 0-1) and would hide a genuine wrong figure (0.58
#: stated against a real 0.481) as if it were rounding.
MATERIAL_REL_TOLERANCE = 0.01

#: Floor under the scaled allowance, so a truth of (or near) zero
#: still tolerates ordinary floating-point noise rather than
#: demanding bit-exact equality.
MATERIAL_ABS_FLOOR = 1e-6


def _materially_different(stated: Any, resolved: Any) -> bool:
    """Decide whether a stated value is wrong, not merely rounded.

    Parameters
    ----------
    stated : Any
        The value as the report (or slide) stated it.
    resolved : Any
        The value the staged data supports.

    Returns
    -------
    bool
        True when the gap is bigger than ordinary rounding noise.
    """
    try:
        s, t = float(stated), float(resolved)
    except (TypeError, ValueError):
        return str(stated) != str(resolved)
    gap = abs(s - t)
    allowance = max(MATERIAL_ABS_FLOOR, abs(t) * MATERIAL_REL_TOLERANCE)
    return gap > allowance


def _trap_names(traps: List[Any]) -> List[str]:
    """Normalize the manifest's trap entries to plain names.

    ``numeric_stage.py`` writes each planted trap as ``{"name": ...}``
    rather than a bare string, so the trap list can carry other
    fields later without breaking this reader.

    Parameters
    ----------
    traps : List[Any]
        Raw trap entries from the staging manifest.

    Returns
    -------
    List[str]
        Trap names.
    """
    names = []
    for trap in traps:
        if isinstance(trap, dict):
            names.append(str(trap.get("name", "")))
        else:
            names.append(str(trap))
    return [n for n in names if n]


def compute_rates(
    scored_by_condition: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Compute the six preregistered rates per condition.

    Parameters
    ----------
    scored_by_condition : Dict[str, List[Dict[str, Any]]]
        Output of :func:`score_case`, grouped back by condition after
        blind scoring.

    Returns
    -------
    Dict[str, Any]
        One entry per condition with ``cases``, ``wrong_shipped``,
        ``false_decline_rate``, ``coverage``, ``trap_catch_rate``
        (by trap name), ``mean_rounds``, and ``declined_rate``.
    """
    report: Dict[str, Any] = {}
    for condition, cases in scored_by_condition.items():
        if not cases:
            report[condition] = {"cases": 0}
            continue

        n = len(cases)
        wrong_shipped = sum(len(c["wrong_shipped"]) for c in cases)
        false_declines = sum(len(c["false_declines"]) for c in cases)
        coverage_gaps = sum(len(c["coverage_gaps"]) for c in cases)
        declined = sum(1 for c in cases if c["declined"])
        rounds = sum(c.get("rounds", 1) for c in cases)

        # A trap is caught when the checker's overall verdict was not
        # clean: either it failed outright, or it was declined after
        # exhausting retries. wrong_shipped is the opposite signal
        # (a trap that slipped through verified) and must never be
        # counted toward a catch.
        trap_opportunities: Counter[str] = Counter()
        trap_caught: Counter[str] = Counter()
        for c in cases:
            for trap in c["traps_present"]:
                trap_opportunities[trap] += 1
                if not c["verified_clean"] or c["declined"]:
                    trap_caught[trap] += 1

        report[condition] = {
            "cases": n,
            "wrong_figures_shipped": wrong_shipped,
            "false_decline_rate": round(false_declines / n, 3),
            "coverage_gap_rate": round(coverage_gaps / n, 3),
            "declined_rate": round(declined / n, 3),
            "mean_rounds": round(rounds / n, 2),
            "trap_catch_rate": {
                trap: round(trap_caught[trap] / count, 3)
                for trap, count in trap_opportunities.items()
            },
        }
    return report


def grade(
    records: List[CaseRecord],
    truth_by_case: Dict[str, Any],
    seed: int,
) -> Dict[str, Any]:
    """Run the full label-blind grading pass.

    Parameters
    ----------
    records : List[CaseRecord]
        Every case from every condition present.
    truth_by_case : Dict[str, Any]
        Ground truth keyed by case id, from the staging manifest.
    seed : int
        Blinding shuffle seed.

    Returns
    -------
    Dict[str, Any]
        ``rates`` (output of :func:`compute_rates`) and
        ``case_count`` by condition, for a sanity check against the
        manifest.
    """
    pool = blind_pool(records, seed)
    scored_by_condition: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for _blind_id, record in pool:
        truth = truth_by_case.get(record.case_id, {})
        scored = score_case(record, truth)
        scored["rounds"] = record.rounds
        # The rejoin: scoring above never looked at .condition, and
        # the join happens only now, after every case was scored.
        scored_by_condition[record.condition].append(scored)

    return {
        "rates": compute_rates(scored_by_condition),
        "case_count": {k: len(v) for k, v in scored_by_condition.items()},
    }


def load_truth_manifest(path: Path) -> Dict[str, Any]:
    """Load the staging manifest's ground truth, keyed by case id.

    Parameters
    ----------
    path : Path
        ``numeric_stage.py``'s manifest.

    Returns
    -------
    Dict[str, Any]
        Case id to its truth entry.
    """
    raw = json.loads(Path(path).expanduser().read_text())
    return {c["case_id"]: c for c in raw.get("cases", [])}


def build_parser() -> argparse.ArgumentParser:
    """Build the grader's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_grader")
    p.add_argument("--truth-manifest", required=True)
    p.add_argument("--condition-b-dir", default=None)
    p.add_argument("--condition-a-dir", default=None)
    p.add_argument("--condition-c-dir", default=None)
    p.add_argument("--seed", type=int, default=737)
    p.add_argument("--out", default=None)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Load whichever conditions are available, grade, and print rates.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Zero on success.
    """
    args = build_parser().parse_args(argv)
    truth = load_truth_manifest(Path(args.truth_manifest))

    records: List[CaseRecord] = []
    if args.condition_b_dir:
        records += load_condition_b(Path(args.condition_b_dir))
    if args.condition_a_dir:
        records += load_condition_a(Path(args.condition_a_dir))
    if args.condition_c_dir:
        records += load_condition_c(Path(args.condition_c_dir))

    if not records:
        print("no condition directories given; nothing to grade")
        return 1

    result = grade(records, truth, args.seed)
    print(json.dumps(result, indent=2, default=str))
    if args.out:
        Path(args.out).expanduser().write_text(
            json.dumps(result, indent=2, default=str)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
