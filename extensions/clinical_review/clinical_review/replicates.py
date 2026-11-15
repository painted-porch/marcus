"""
Aggregate several identical runs: mean, spread, and per-item agreement.

Replicates answer three questions and not a fourth.

They say whether a run reproduces, which is what a reader who tries to repeat
the study will do first. They say how much each arm's rate moves between
identical runs, which is the providers' own non-determinism since nothing else
varies. And they say whether a checker reaches the same verdict on the same
recommendation every time, which is a reliability property no single run can
show.

They do **not** raise statistical power. The power calculation is in decision
points: repeating the same 30 cases five times does not produce 150
independent items, because each case is perfectly correlated with itself. The
report this module writes says so in its own text, so nobody reads a tighter
interval off it than the data supports.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .arms import read_jsonl
from .grader import ARMS

#: A verdict that is not a flag and not a clean pass: unusable, however it got
#: that way. ``declined`` is the board's terminal outcome, ``indeterminate``
#: the off-board equivalent, ``missing`` a chain whose verify card never ran.
UNUSABLE: frozenset[str] = frozenset({"indeterminate", "declined", "missing"})


@dataclass
class RunSummary:
    """
    One run's figures, as the grader computed them.

    Parameters
    ----------
    stamp : str
        The run directory's name.
    gate : Dict[str, Any]
        The A0 advocacy gate's counts.
    arms : Dict[str, Dict[str, Any]]
        Per arm: caught, denominator, rate, false_flags, aligned_n,
        false_flag_rate, unusable.
    verdicts : Dict[str, Dict[str, str]]
        Per arm, chain id to verdict, for the agreement measure.
    spend_usd : float
        What the run recorded spending.
    settings : Dict[str, str]
        The run's own config lines, so a reader can see what varied.
    """

    stamp: str
    gate: dict[str, Any]
    arms: dict[str, dict[str, Any]]
    verdicts: dict[str, dict[str, str]] = field(default_factory=dict)
    spend_usd: float = 0.0
    settings: dict[str, str] = field(default_factory=dict)


def read_config(run_dir: Path) -> dict[str, str]:
    """
    Read ``run-config.txt`` back as a mapping.

    Parameters
    ----------
    run_dir : Path
        The run record.

    Returns
    -------
    Dict[str, str]
    """
    out: dict[str, str] = {}
    path = run_dir / "run-config.txt"
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def load_run(run_dir: Path) -> RunSummary | None:
    """
    Summarise one run directory, or return None when it has no graded output.

    Parameters
    ----------
    run_dir : Path
        The run record.

    Returns
    -------
    Optional[RunSummary]
    """
    outcomes_path = run_dir / "grading" / "outcomes.json"
    if not outcomes_path.is_file():
        return None
    outcomes = json.loads(outcomes_path.read_text())["outcomes"]
    if not outcomes.get("arms"):
        return None
    config = read_config(run_dir)
    try:
        spend = float(config.get("spend_usd", "0") or 0)
    except ValueError:
        spend = 0.0
    verdicts: dict[str, dict[str, str]] = {}
    for arm in ARMS:
        rows = read_jsonl(run_dir / "arms" / arm / "verdicts.jsonl")
        if rows:
            verdicts[arm] = {r["chain_id"]: str(r.get("verdict") or "") for r in rows}
    arms: dict[str, dict[str, Any]] = {}
    for arm, figures in outcomes["arms"].items():
        arms[arm] = {
            "caught": figures["flagged"],
            "denominator": figures["denominator"],
            "rate": figures["rate"],
            "false_flags": figures["false_flags"],
            "aligned_n": figures["aligned_n"],
            "false_flag_rate": figures["false_flag_rate"],
            "unusable": figures["indeterminate"],
        }
    return RunSummary(
        stamp=run_dir.name,
        gate=outcomes.get("gate", {}),
        arms=arms,
        verdicts=verdicts,
        spend_usd=spend,
        settings=config,
    )


def spread(values: list[float]) -> dict[str, Any]:
    """
    Mean, standard deviation and range of a list, safe for one value.

    Parameters
    ----------
    values : List[float]
        The values.

    Returns
    -------
    Dict[str, Any]
        ``n``, ``mean``, ``sd`` (None when fewer than two values), ``min``,
        ``max``.
    """
    if not values:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values),
        "max": max(values),
    }


def item_agreement(runs: list[RunSummary], arm: str) -> dict[str, Any]:
    """
    How often an arm reaches the same verdict on the same recommendation.

    Parameters
    ----------
    runs : List[RunSummary]
        The replicates.
    arm : str
        The arm.

    Returns
    -------
    Dict[str, Any]
        ``items`` compared, ``unanimous`` count and rate, ``split`` items with
        their verdict tallies, and ``runs_compared``.
    """
    per_item: dict[str, list[str]] = {}
    compared = 0
    for run in runs:
        verdicts = run.verdicts.get(arm)
        if not verdicts:
            continue
        compared += 1
        for chain, verdict in verdicts.items():
            per_item.setdefault(chain, []).append(verdict)
    if compared < 2:
        return {"runs_compared": compared, "items": len(per_item), "unanimous": None}
    full = {c: v for c, v in per_item.items() if len(v) == compared}
    unanimous = sum(1 for v in full.values() if len(set(v)) == 1)
    split = {chain: dict(Counter(v)) for chain, v in full.items() if len(set(v)) > 1}
    return {
        "runs_compared": compared,
        "items": len(full),
        "unanimous": unanimous,
        "unanimous_rate": unanimous / len(full) if full else None,
        "split_items": len(split),
        "split_detail": split,
    }


def aggregate(run_dirs: list[Path]) -> dict[str, Any]:
    """
    Combine every graded run in ``run_dirs``.

    Parameters
    ----------
    run_dirs : List[Path]
        Run records, in any order; the earliest stamp is treated as the
        reference run for the reproducibility check.

    Returns
    -------
    Dict[str, Any]
        ``runs`` (per-run figures), ``arms`` (spread per arm), ``agreement``
        (per arm), ``gate`` (spread) and ``spend``.
    """
    loaded = [r for r in (load_run(d) for d in sorted(run_dirs)) if r is not None]
    if not loaded:
        return {"runs": [], "arms": {}, "agreement": {}, "gate": {}, "spend": {}}
    arms: dict[str, Any] = {}
    for arm in ARMS:
        caught = [r.arms[arm]["rate"] for r in loaded if arm in r.arms]
        false_flags = [r.arms[arm]["false_flag_rate"] for r in loaded if arm in r.arms]
        unusable = [float(r.arms[arm]["unusable"]) for r in loaded if arm in r.arms]
        counts = [float(r.arms[arm]["caught"]) for r in loaded if arm in r.arms]
        if not caught:
            continue
        denominators = {r.arms[arm]["denominator"] for r in loaded if arm in r.arms}
        pooled_n = sum(r.arms[arm]["denominator"] for r in loaded if arm in r.arms)
        pooled_k = sum(r.arms[arm]["caught"] for r in loaded if arm in r.arms)
        arms[arm] = {
            "caught_rate": spread(caught),
            "caught_count": spread(counts),
            "false_flag_rate": spread(false_flags),
            "unusable": spread(unusable),
            "denominators": sorted(denominators),
            # Pooled across replicates, reported ONLY as a descriptive summary.
            # Its interval is deliberately omitted: the replicates share the
            # same cases, so a pooled interval would be far too narrow.
            "pooled": {"caught": pooled_k, "of": pooled_n},
        }
    # The grader's gate names this key ``adversarial_harmful_rate``; the
    # runner's own gate.json names it ``adversarial_rate``. Read either, or
    # the report silently claims it compared zero runs, which is how this
    # first aggregation read on 2026-11-15.
    gate_rates: list[float] = []
    for run in loaded:
        rate = run.gate.get("adversarial_harmful_rate")
        if rate is None:
            rate = run.gate.get("adversarial_rate")
        if rate is not None:
            gate_rates.append(float(rate))
    reference = loaded[0]
    return {
        "runs": [
            {
                "stamp": r.stamp,
                "spend_usd": r.spend_usd,
                "temperature": r.settings.get("temperature", "not recorded"),
                "arms": {a: f["caught"] for a, f in r.arms.items()},
                "false_flags": {a: f["false_flags"] for a, f in r.arms.items()},
                "unusable": {a: f["unusable"] for a, f in r.arms.items()},
            }
            for r in loaded
        ],
        "reference_run": reference.stamp,
        "arms": arms,
        "agreement": {arm: item_agreement(loaded, arm) for arm in ARMS if arm in arms},
        "gate": spread(gate_rates),
        "spend": {"total_usd": sum(r.spend_usd for r in loaded), "runs": len(loaded)},
    }


def _fmt(value: float | None, pct: bool = False) -> str:
    """Render a number, or a dash when there is none."""
    if value is None:
        return "—"
    return f"{value:.1%}" if pct else f"{value:.2f}"


def render(agg: dict[str, Any]) -> str:
    """
    Render the replicate report, including what replicates cannot do.

    Parameters
    ----------
    agg : Dict[str, Any]
        From :func:`aggregate`.

    Returns
    -------
    str
        Markdown, one sentence per line.
    """
    runs = agg["runs"]
    if not runs:
        return "# Replicates\n\nNo graded run was found.\n"
    lines = [
        "# Replicates: Hospital",
        "",
        f"{len(runs)} graded runs, identical in every setting, "
        f"${agg['spend']['total_usd']:.4f} in total.",
        "Nothing varies between them, so any difference is the providers' own "
        "non-determinism.",
        "",
        "**What these replicates do not do.** They do not raise statistical " "power.",
        "The power calculation is in decision points, and repeating the same "
        "cases does not create independent ones.",
        "A pooled count appears below as a description of what happened, with "
        "no interval attached, because an interval over replicates of the same "
        "cases would be far narrower than the evidence warrants.",
        "",
        "## Per arm, across runs",
        "",
        "| Arm | Harmful caught, mean of 30 | SD | Range | False alarms, mean | "
        "Unusable, mean |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm in ARMS:
        figures = agg["arms"].get(arm)
        if not figures:
            continue
        caught, ff, unusable = (
            figures["caught_count"],
            figures["false_flag_rate"],
            figures["unusable"],
        )
        rng = (
            f"{caught['min']:.0f} to {caught['max']:.0f}"
            if caught["min"] is not None
            else "—"
        )
        lines.append(
            f"| {arm} | {_fmt(caught['mean'])} | {_fmt(caught['sd'])} | {rng} | "
            f"{_fmt(ff['mean'], pct=True)} | {_fmt(unusable['mean'])} |"
        )
    lines += ["", "## Did the runs agree item by item?", ""]
    for arm in ARMS:
        agreement = agg["agreement"].get(arm)
        if not agreement or agreement.get("unanimous") is None:
            continue
        rate = agreement.get("unanimous_rate")
        lines.append(
            f"{arm}: the same verdict on {agreement['unanimous']} of "
            f"{agreement['items']} recommendations across "
            f"{agreement['runs_compared']} runs"
            + (f" ({rate:.0%})." if rate is not None else ".")
        )
    lines += ["", "## The advocacy gate, across runs", ""]
    gate = agg["gate"]
    lines.append(
        f"Adversarial advocacy: mean {_fmt(gate['mean'], pct=True)}, "
        f"SD {_fmt(gate['sd'])}, over {gate['n']} runs."
    )
    lines += [
        "",
        "## Run by run",
        "",
        "| Run | Temperature | Spend |",
        "| --- | --- | --- |",
    ]
    for run in runs:
        lines.append(
            f"| {run['stamp']} | {run['temperature']} | ${run['spend_usd']:.4f} |"
        )
    reference = agg.get("reference_run")
    if reference:
        lines += [
            "",
            f"The earliest run here, `{reference}`, is the reference for the "
            "reproducibility check.",
        ]
    pooled_note = [
        f"{arm}: {agg['arms'][arm]['pooled']['caught']} of "
        f"{agg['arms'][arm]['pooled']['of']}"
        for arm in ARMS
        if arm in agg["arms"]
    ]
    if pooled_note:
        lines += [
            "",
            "## Pooled counts, descriptive only",
            "",
            "; ".join(pooled_note) + ".",
            "No interval or test is computed on these, for the reason given at "
            "the top.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI: aggregate the given run directories and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_root", help="the directory holding run records")
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="specific run stamps; every graded run under runs_root by default",
    )
    parser.add_argument(
        "--out", default=None, help="where to write REPLICATES.md; stdout by default"
    )
    args = parser.parse_args(argv)
    root = Path(args.runs_root)
    dirs = (
        [root / stamp for stamp in args.only]
        if args.only
        else [d for d in root.iterdir() if d.is_dir()]
    )
    agg = aggregate(dirs)
    report = render(agg)
    if args.out:
        Path(args.out).write_text(report)
        (Path(args.out).parent / "replicates.json").write_text(
            json.dumps(agg, indent=2) + "\n"
        )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
