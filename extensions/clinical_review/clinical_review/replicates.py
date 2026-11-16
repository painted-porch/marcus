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
import math
import re
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

#: Chains per run: 30 cases in both modes. A run missing any of them in any
#: arm did not finish, whatever its config says.
EXPECTED_CHAINS = 60


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


def code_version(settings: dict[str, str]) -> str:
    """
    Report the extension commit a run executed, from its recorded config.

    The run records a line like ``checkout: <path> @ <sha> (N dirty)``. The sha
    is what matters for comparing runs: a set whose runs executed different
    commits is not one set until the figures are shown to be unchanged across
    them, so the report has to be able to say which commit each run ran.

    Parameters
    ----------
    settings : Dict[str, str]
        From :func:`read_config`.

    Returns
    -------
    str
        Short sha, with ``+dirty`` where the checkout had uncommitted files,
        or ``not recorded``.
    """
    line = settings.get("checkout", "")
    match = re.search(r"@\s*([0-9a-f]{7,40})", line)
    if not match:
        return "not recorded"
    out = match.group(1)[:8]
    dirty = re.search(r"\((\d+) dirty\)", line)
    if dirty and dirty.group(1) != "0":
        out += f"+{dirty.group(1)}dirty"
    return out


def completed(run_dir: Path, config: dict[str, str] | None = None) -> bool:
    """
    Decide whether a run finished, from its record rather than its say-so.

    A ``superseded`` line excludes a run outright, whatever else its record
    says: a defect found after the fact does not make the run incomplete, it
    makes it unusable, and the reason belongs with the data.

    The ``completed`` config line was added after the first replicates had
    already run, so a marker check alone excludes every earlier run and the
    aggregation silently reports nothing. The line is honoured when present,
    and otherwise completion is established from evidence a finished run
    cannot fake: a written ``RESULTS.md``, every arm carrying a verdict for
    each of the 60 chains, and no ``stopped`` line.

    Parameters
    ----------
    run_dir : Path
        The run record.
    config : Optional[Dict[str, str]]
        Its config lines, read when not supplied.

    Returns
    -------
    bool
    """
    settings = config if config is not None else read_config(run_dir)
    # A run superseded by a defect found afterwards is excluded by default.
    # Marking it in its own record beats moving it to another directory: the
    # reason travels with the data, and an aggregation cannot silently mix a
    # broken run with a fixed one (deviation 12, the 600-token truncation).
    if settings.get("superseded", "").strip():
        return False
    marker = settings.get("completed", "").strip().lower()
    if marker:
        return marker == "yes"
    if "stopped" in settings:
        return False
    if not (run_dir / "RESULTS.md").is_file():
        return False
    for arm in ARMS:
        rows = read_jsonl(run_dir / "arms" / arm / "verdicts.jsonl")
        if len(rows) != EXPECTED_CHAINS:
            return False
    return True


def load_run(run_dir: Path, include_incomplete: bool = False) -> RunSummary | None:
    """
    Summarise one run directory, or return None when it must not be averaged.

    A run that reached its stop line part-way through still gets graded, since
    ``run.run`` grades whatever it has, so its ``outcomes.json`` carries a
    non-empty ``arms`` map whose unperformed checks look exactly like misses
    (Codex P2 on PR #922). Averaging that into completed replicates would pull
    every rate down and call it non-determinism. A run therefore joins the set
    only when :func:`completed` says it did, unless a caller asks for the
    incomplete ones deliberately.

    Parameters
    ----------
    run_dir : Path
        The run record.
    include_incomplete : bool
        Accept a run whose record does not say it completed. Off by default.

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
    if not include_incomplete and not completed(run_dir, config):
        return None
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


def aggregate(run_dirs: list[Path], include_incomplete: bool = False) -> dict[str, Any]:
    """
    Combine every graded run in ``run_dirs``.

    Parameters
    ----------
    run_dirs : List[Path]
        Run records, in any order; the earliest stamp is treated as the
        reference run for the reproducibility check.
    include_incomplete : bool
        Pass runs whose record does not say they completed, which are excluded
        by default so a stop-line halt is not averaged in as a low score.

    Returns
    -------
    Dict[str, Any]
        ``runs`` (per-run figures), ``arms`` (spread per arm), ``agreement``
        (per arm), ``gate`` (spread) and ``spend``.
    """
    loaded = [
        r
        for r in (
            load_run(d, include_incomplete=include_incomplete) for d in sorted(run_dirs)
        )
        if r is not None
    ]
    if not loaded:
        return {"runs": [], "arms": {}, "agreement": {}, "gate": {}, "spend": {}}
    arms: dict[str, Any] = {}
    for arm in ARMS:
        caught = [r.arms[arm]["rate"] for r in loaded if arm in r.arms]
        # A run with no usable aligned check has no false-flag rate at all
        # (``None``), and averaging it in either crashes or invents a zero
        # (Codex P2 on PR #922). It is left out, and ``n`` says how many runs
        # the mean is over.
        false_flags = [
            float(r.arms[arm]["false_flag_rate"])
            for r in loaded
            if arm in r.arms and r.arms[arm].get("false_flag_rate") is not None
        ]
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
                "code": code_version(r.settings),
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
        f"{len(runs)} graded runs, " f"${agg['spend']['total_usd']:.4f} in total.",
    ]
    # Whether the runs are comparable is a fact about the record, not a phrase
    # to assert. Runs that executed different commits are reported as such.
    codes = sorted({str(r.get("code", "not recorded")) for r in runs})
    temps = sorted({str(r["temperature"]) for r in runs})
    if len(codes) == 1 and len(temps) == 1:
        lines.append(
            "Every run used the same settings and the same code "
            f"(`{codes[0]}`), so any difference between them is the "
            "providers' own non-determinism."
        )
    else:
        lines.append(
            "The settings were the same in every run, but the runs did NOT all "
            f"execute the same code: {', '.join(f'`{c}`' for c in codes)}."
        )
        lines.append(
            "Each run was re-graded afterwards with one single version, so the "
            "scoring is uniform; what differs between them is the code that "
            "generated and checked the advice, and that is a limit on reading "
            "the spread below as non-determinism alone."
        )
    lines += [
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
        "| Run | Temperature | Code | Spend |",
        "| --- | --- | --- | --- |",
    ]
    for run in runs:
        lines.append(
            f"| {run['stamp']} | {run['temperature']} | "
            f"`{run.get('code', 'not recorded')}` | ${run['spend_usd']:.4f} |"
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


def decompose(regenerated: dict[str, Any], frozen: dict[str, Any]) -> dict[str, Any]:
    """
    Split each arm's run-to-run variation into checker and adviser parts.

    Two sets are needed because one cannot separate them. In the regenerated
    set the adviser writes new advice every run and the checker re-reads it, so
    the spread holds both. In the frozen set every run checks the identical 60
    recommendations, so the spread is the checker alone.

    Treating the two sources as independent, the adviser's contribution is
    ``sqrt(total variance - checker variance)``. Sampling noise can make the
    frozen spread the larger of the two, which would put a negative number
    under that root; that is reported as ``None`` with a note rather than
    squared away, because it means the two sets cannot be told apart at this
    number of runs.

    Parameters
    ----------
    regenerated : Dict[str, Any]
        :func:`aggregate` over the set whose advice was regenerated each run.
    frozen : Dict[str, Any]
        :func:`aggregate` over the set whose advice was held fixed.

    Returns
    -------
    Dict[str, Any]
        Per arm: both standard deviations in cases caught, the implied adviser
        contribution, and which source dominates.
    """
    out: dict[str, Any] = {"arms": {}, "runs": {}}
    out["runs"] = {
        "regenerated": len(regenerated.get("runs", [])),
        "frozen": len(frozen.get("runs", [])),
    }
    for arm in ARMS:
        total = (regenerated.get("arms", {}).get(arm) or {}).get("caught_count")
        checker = (frozen.get("arms", {}).get(arm) or {}).get("caught_count")
        if not total or not checker:
            continue
        total_sd, checker_sd = total.get("sd"), checker.get("sd")
        entry: dict[str, Any] = {
            "total_sd": total_sd,
            "checker_sd": checker_sd,
            "regenerated_mean": total.get("mean"),
            "frozen_mean": checker.get("mean"),
        }
        if total_sd is None or checker_sd is None:
            entry["adviser_sd"] = None
            entry["note"] = "a set with one run has no spread to split"
        elif checker_sd > total_sd:
            entry["adviser_sd"] = None
            entry["note"] = (
                "the frozen set varies at least as much as the regenerated "
                "one, so at this number of runs the adviser's contribution "
                "cannot be separated from noise"
            )
        else:
            entry["adviser_sd"] = math.sqrt(total_sd**2 - checker_sd**2)
            share = (checker_sd**2) / (total_sd**2) if total_sd else None
            entry["checker_share_of_variance"] = share
            entry["dominant_source"] = (
                "the checker" if share is not None and share >= 0.5 else "the adviser"
            )
        out["arms"][arm] = entry
    return out


def render_decomposition(dec: dict[str, Any]) -> str:
    """
    Render the decomposition, naming what each column is measured on.

    Parameters
    ----------
    dec : Dict[str, Any]
        From :func:`decompose`.

    Returns
    -------
    str
        Markdown, one sentence per line.
    """
    runs = dec.get("runs", {})
    head = [
        "# Where the run-to-run variation comes from",
        "",
        f"{runs.get('regenerated', 0)} runs that regenerated their advice, "
        f"{runs.get('frozen', 0)} that re-checked one frozen set of it.",
    ]
    # With no frozen run there is nothing to decompose, and an empty table under
    # confident prose reads as a measurement that was made and came out blank.
    # Say instead that the measurement is missing.
    if not runs.get("frozen", 0) or not dec.get("arms"):
        return (
            "\n".join(
                head
                + [
                    "",
                    "The split between checker noise and adviser noise is NOT "
                    "measured here, because it needs runs that re-checked one "
                    "frozen set of advice and there are none.",
                    "Every spread reported above therefore holds the checker and "
                    "the adviser together, and neither can be credited with it.",
                ]
            )
            + "\n"
        )
    lines = head + [
        "The frozen spread is the checker alone, because every run read the "
        "identical 60 recommendations.",
        "The regenerated spread holds the checker and the adviser together.",
        "",
        "| Arm | Mean, regenerated | Mean, frozen | SD, both sources | "
        "SD, checker alone | Implied adviser SD | Dominant |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in ARMS:
        e = dec.get("arms", {}).get(arm)
        if not e:
            continue
        lines.append(
            f"| {arm} | {_fmt(e['regenerated_mean'])} | {_fmt(e['frozen_mean'])} | "
            f"{_fmt(e['total_sd'])} | {_fmt(e['checker_sd'])} | "
            f"{_fmt(e.get('adviser_sd'))} | {e.get('dominant_source', '—')} |"
        )
    notes = {arm: e["note"] for arm, e in dec.get("arms", {}).items() if e.get("note")}
    if notes:
        lines += ["", "Where a figure is missing:", ""]
        for arm, note in notes.items():
            lines.append(f"- {arm}: {note}.")
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
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="average in runs whose record does not say they completed",
    )
    parser.add_argument(
        "--frozen",
        default=None,
        help=(
            "a second runs root whose advice was held fixed; with it the "
            "report splits each arm's spread into checker and adviser parts"
        ),
    )
    args = parser.parse_args(argv)
    root = Path(args.runs_root)
    dirs = (
        [root / stamp for stamp in args.only]
        if args.only
        else [d for d in root.iterdir() if d.is_dir()]
    )
    agg = aggregate(dirs, include_incomplete=args.include_incomplete)
    report = render(agg)
    if args.frozen:
        frozen_root = Path(args.frozen)
        frozen = aggregate(
            [d for d in frozen_root.iterdir() if d.is_dir()],
            include_incomplete=args.include_incomplete,
        )
        dec = decompose(agg, frozen)
        report = report + "\n" + render_decomposition(dec)
        agg = {"regenerated": agg, "frozen": frozen, "decomposition": dec}
    if args.out:
        Path(args.out).write_text(report)
        (Path(args.out).parent / "replicates.json").write_text(
            json.dumps(agg, indent=2) + "\n"
        )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
