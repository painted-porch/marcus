"""Stage a model evaluation into the artifact family a report is written from.

The numeric-verifier proof of concept mirrors a colleague's workflow:
train a model, stage its evaluation as files, have an agent write a
report from those files, and check every number in the report against
them. This module produces the staged side of that loop.

The artifacts deliberately match the shape his workflow produces --
an overall metrics file, per-crop metrics, a per-record error table,
quality tiers, and size quartiles -- so the claims an agent writes
about them exercise the same lookups, ratios and cross-table
relations his report does.

Ground truth for grading is written to a separate manifest that never
reaches the Marcus board; a worker that can read the answer from the
board is not being tested.

Usage::

    python dev-tools/experiments/runners/numeric_stage.py \
        --source-parquet /path/to/faostat_four_crops.parquet \
        --out-root ~/marcus-experiments/numeric-verifier/staged \
        --cases 30 --seed 737
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# FAOSTAT names its items in full; USDA Quick Stats is read through
# its own adapter and already carries the short labels.
FAOSTAT_CROPS = {
    "Maize (corn)": "maize",
    "Soya beans": "soybeans",
    "Wheat": "wheat",
    "Seed cotton, unginned": "cotton",
}

#: Canonical columns every source adapter produces.
CANONICAL_COLUMNS = ["area", "crop", "year", "yield_value", "area_value", "unit"]

# Total crop failures put a near-zero denominator under a percentage
# error. The floor is a small positive yield in whatever unit the
# source reports, which is enough to exclude them without trimming
# genuinely poor harvests.
MIN_YIELD_VALUE = 1.0

# The colleague's report covered 14,258 fields split 8,171 maize /
# 5,305 soybeans / 413 wheat / 369 cotton, so one crop carries 57
# percent of the portfolio and the smallest carries under 3 percent.
# Even sampling would give four groups of similar size, and the
# relations a report states about them -- the largest-to-smallest
# ratio above all -- would not resemble his. Cases are drawn to this
# mix instead.
CASE_CROP_MIX = {
    "maize": 0.573,
    "soybeans": 0.372,
    "wheat": 0.029,
    "cotton": 0.026,
}

TIER_EDGES = [
    (0.0, 5.0, "Excellent"),
    (5.0, 10.0, "Good"),
    (10.0, 20.0, "Average"),
    (20.0, float("inf"), "Poor"),
]


@dataclass
class StagedCase:
    """One staged evaluation plus the ground truth for grading it.

    Parameters
    ----------
    case_id : str
        Identifier of the case, e.g. ``case-007``.
    staged_dir : Path
        Directory holding the artifacts a report is written from.
    truth : Dict[str, Any]
        Correct value of every figure a report should contain. Written
        outside ``staged_dir`` and never handed to a worker.
    traps : List[Dict[str, Any]]
        The planted errors active in this case.
    """

    case_id: str
    staged_dir: Path
    truth: Dict[str, Any]
    traps: List[Dict[str, Any]] = field(default_factory=list)


def load_faostat(parquet_path: Path) -> pd.DataFrame:
    """Load a FAOSTAT extract and pivot it to one row per unit.

    A unit is one country-crop-year, which stands in for one field in
    the colleague's report.

    Parameters
    ----------
    parquet_path : Path
        Extract containing ``Area``, ``Item``, ``Element``, ``Year``
        and ``Value`` columns.

    Returns
    -------
    pd.DataFrame
        Columns ``area``, ``crop``, ``year``, ``yield_value``,
        ``area_value``.
    """
    raw = pd.read_parquet(parquet_path)
    raw = raw[raw["Item"].isin(FAOSTAT_CROPS)].copy()
    raw["crop"] = raw["Item"].map(FAOSTAT_CROPS)
    wide = raw.pivot_table(
        index=["Area", "crop", "Year"],
        columns="Element",
        values="Value",
        aggfunc="first",
    ).reset_index()
    wide = wide.rename(
        columns={
            "Area": "area",
            "Year": "year",
            "Yield": "yield_value",
            "Area harvested": "area_value",
        }
    )
    wide = wide.dropna(subset=["yield_value", "area_value"])
    wide = wide[(wide["yield_value"] > 0) & (wide["area_value"] > 0)]
    # Total crop failures (yields near zero, e.g. 0.1 kg/ha after a
    # drought) put a near-zero denominator under every percentage
    # error and let a single row dominate the mean. They are excluded
    # as undefined-error records. The exclusion is the reason a tier
    # table built from scored records can total less than the record
    # count, which is the relation the dropped_tier_rows trap plants.
    wide = wide[wide["yield_value"] >= MIN_YIELD_VALUE]
    wide["unit"] = "kg/ha"
    return wide[CANONICAL_COLUMNS].reset_index(drop=True)


def load_nass(parquet_path: Path) -> pd.DataFrame:
    """Load a USDA Quick Stats extract into the canonical shape.

    A unit is one county-crop-year, which stands in for one field in
    the colleague's report. Grains are reported in bushels per acre
    and cotton in pounds per acre, so the unit travels with the row
    and the per-crop table states it, exactly as his report does.

    Parameters
    ----------
    parquet_path : Path
        Extract written by ``numeric_fetch_nass.py``.

    Returns
    -------
    pd.DataFrame
        The canonical columns.
    """
    raw = pd.read_parquet(parquet_path)
    out = raw.rename(
        columns={
            "yield_per_acre": "yield_value",
            "acres_harvested": "area_value",
        }
    ).copy()
    out = out.dropna(subset=["yield_value", "area_value"])
    out = out[(out["yield_value"] > 0) & (out["area_value"] > 0)]
    # A near-zero yield puts a near-zero denominator under every
    # percentage error and lets one row dominate the mean. Such rows
    # are excluded as undefined-error records. That exclusion is the
    # reason a tier table built from scored records can total less
    # than the record count, which is the relation the
    # dropped_tier_rows trap plants.
    out = out[out["yield_value"] >= MIN_YIELD_VALUE]
    return out[CANONICAL_COLUMNS].reset_index(drop=True)


SOURCES = {"nass": load_nass, "faostat": load_faostat}


def load_source(parquet_path: Path, source: str = "nass") -> pd.DataFrame:
    """Load an evaluation source into the canonical shape.

    Every adapter returns the same columns, so swapping the source
    changes nothing downstream. USDA county yields are the default
    because a county is the closest public analogue to a field.

    Parameters
    ----------
    parquet_path : Path
        Extract to read.
    source : str
        Adapter name: ``nass`` or ``faostat``.

    Returns
    -------
    pd.DataFrame
        The canonical columns.

    Raises
    ------
    KeyError
        When the adapter name is unknown.
    """
    if source not in SOURCES:
        raise KeyError(f"unknown source: {source}; expected one of {sorted(SOURCES)}")
    return SOURCES[source](parquet_path)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add the lagged-yield feature the model trains on.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`load_source`.

    Returns
    -------
    pd.DataFrame
        Input plus ``prev_yield`` and ``prev_area``, with the first
        year of each area-crop series dropped.
    """
    df = df.sort_values(["area", "crop", "year"]).copy()
    grp = df.groupby(["area", "crop"], sort=False)
    df["prev_yield"] = grp["yield_value"].shift(1)
    df["prev_area"] = grp["area_value"].shift(1)
    return df.dropna(subset=["prev_yield", "prev_area"]).reset_index(drop=True)


def fit_and_predict(
    df: pd.DataFrame, test_years: Tuple[int, int], seed: int
) -> pd.DataFrame:
    """Train on years before the window and predict inside it.

    One model is fitted per crop. The crops are reported in different
    units -- bushels per acre for the grains, pounds per acre for
    cotton -- so a single model across all of them would predict
    bushel-scale values for cotton and score catastrophically for a
    reason that has nothing to do with the experiment.

    Model quality is not the subject of the experiment. What matters
    is that the errors are real and long-tailed, like the report this
    is modelled on where median error was 4.4 percent and mean error
    23.1.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`build_features`.
    test_years : Tuple[int, int]
        Inclusive year range held out for evaluation.
    seed : int
        Seed for the regressor.

    Returns
    -------
    pd.DataFrame
        The held-out rows with ``predicted`` and ``ape`` columns, for
        every crop that had both training and test rows.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor

    lo, hi = test_years
    feats = ["prev_yield", "prev_area", "year"]
    predicted = []

    for crop, group in df.groupby("crop"):
        train = group[group["year"] < lo]
        test = group[(group["year"] >= lo) & (group["year"] <= hi)].copy()
        if len(train) < 50 or test.empty:
            print(
                f"  skipping {crop}: {len(train)} train / {len(test)} test "
                "rows in the window"
            )
            continue
        model = HistGradientBoostingRegressor(
            max_iter=200, learning_rate=0.08, random_state=seed
        )
        model.fit(train[feats], train["yield_value"])
        test["predicted"] = model.predict(test[feats])
        predicted.append(test)

    if not predicted:
        raise SystemExit(
            "no crop had both training and test rows in the window; "
            "widen --test-year-lo/--test-year-hi"
        )

    out = pd.concat(predicted, ignore_index=True)
    out["ape"] = (
        (out["predicted"] - out["yield_value"]).abs() / out["yield_value"] * 100.0
    )
    return out


def _r2(actual: pd.Series, predicted: pd.Series) -> float:
    """Coefficient of determination.

    Parameters
    ----------
    actual : pd.Series
        Observed values.
    predicted : pd.Series
        Model predictions.

    Returns
    -------
    float
        R-squared, or ``float('nan')`` when the series is constant.
    """
    ss_res = float(((actual - predicted) ** 2).sum())
    ss_tot = float(((actual - actual.mean()) ** 2).sum())
    if ss_tot == 0:
        return float("nan")
    return 1.0 - ss_res / ss_tot


def _tier_for(ape: float) -> str:
    """Map an absolute percentage error onto its quality tier.

    Parameters
    ----------
    ape : float
        Absolute percentage error.

    Returns
    -------
    str
        One of Excellent, Good, Average, Poor.
    """
    for lo, hi, name in TIER_EDGES:
        if lo <= ape < hi:
            return name
    return "Poor"


def _sha256(path: Path) -> str:
    """Content hash of a staged file.

    Parameters
    ----------
    path : Path
        File to hash.

    Returns
    -------
    str
        Hex digest.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_case(
    evaluated: pd.DataFrame,
    out_dir: Path,
    case_id: str,
    traps: Optional[List[str]] = None,
    rng: Optional[np.random.Generator] = None,
) -> StagedCase:
    """Write one case's artifacts and record its ground truth.

    Parameters
    ----------
    evaluated : pd.DataFrame
        Held-out rows with predictions, from :func:`fit_and_predict`.
    out_dir : Path
        Directory to write the case into.
    case_id : str
        Identifier of the case.
    traps : Optional[List[str]]
        Trap names to plant. Supported: ``dropped_tier_rows`` (the
        tier table omits records with undefined error, so its total
        no longer equals the record count), ``stale_sibling`` (a
        second evaluation directory with perturbed figures sits
        beside the current one), ``rounding_ratio`` (the crop ratio
        falls just below a round number).
    rng : Optional[np.random.Generator]
        Source of randomness for trap placement.

    Returns
    -------
    StagedCase
        The staged directory, ground truth and active traps.
    """
    traps = traps or []
    rng = rng or np.random.default_rng(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    eval_dir = out_dir / f"eval_{stamp}"
    eval_dir.mkdir(parents=True, exist_ok=True)

    df = evaluated.copy()
    if "rounding_ratio" in traps:
        df = _bias_crop_ratio(df, rng)
    df["record_id"] = [f"r{i:06d}" for i in range(len(df))]
    df["tier"] = df["ape"].apply(_tier_for)

    # --- per-record table -------------------------------------------------
    per_record = df[
        [
            "record_id",
            "crop",
            "area",
            "year",
            "yield_value",
            "predicted",
            "area_value",
            "ape",
            "tier",
        ]
    ].rename(columns={"yield_value": "actual"})
    per_record.to_csv(eval_dir / "per_record.csv", index=False)

    # --- per-crop table ---------------------------------------------------
    rows = []
    for crop, g in df.groupby("crop"):
        rows.append(
            {
                "crop": crop,
                "n_records": int(len(g)),
                "r2": round(_r2(g["yield_value"], g["predicted"]), 3),
                "median_ape": round(float(g["ape"].median()), 1),
                "mean_ape": round(float(g["ape"].mean()), 1),
                "unit": str(g["unit"].iloc[0]),
            }
        )
    per_crop = (
        pd.DataFrame(rows)
        .sort_values("n_records", ascending=False)
        .reset_index(drop=True)
    )
    per_crop.to_csv(eval_dir / "per_crop.csv", index=False)

    # --- tiers ------------------------------------------------------------
    tier_src = df
    dropped = 0
    if "dropped_tier_rows" in traps:
        dropped = int(rng.integers(3, 9))
        tier_src = df.iloc[: len(df) - dropped]
    tier_rows = []
    tier_total = int(len(tier_src))
    for _, _, name in TIER_EDGES:
        n = int((tier_src["tier"] == name).sum())
        tier_rows.append(
            {
                "tier": name,
                "n_records": n,
                "rate_pct": round(100.0 * n / tier_total, 1),
            }
        )
    pd.DataFrame(tier_rows).to_csv(eval_dir / "tiers.csv", index=False)

    # --- size quartiles ---------------------------------------------------
    # Ties in harvested area can collapse quantile edges, which would
    # raise rather than produce fewer bins. Dropping duplicate edges
    # keeps staging working on data with repeated values; the labels
    # are assigned after the fact so they always match the bin count.
    binned = pd.qcut(df["area_value"], 4, duplicates="drop")
    n_bins = len(binned.cat.categories)
    df["quartile"] = binned.cat.rename_categories([f"Q{i + 1}" for i in range(n_bins)])
    qrows = []
    for q, g in df.groupby("quartile", observed=True):
        qrows.append(
            {
                "quartile": str(q),
                "n_records": int(len(g)),
                "median_area_value": int(g["area_value"].median()),
                "median_ape": round(float(g["ape"].median()), 1),
            }
        )
    pd.DataFrame(qrows).to_csv(eval_dir / "quartiles.csv", index=False)

    # --- overall metrics --------------------------------------------------
    metrics: Dict[str, Any] = {
        "n_records": int(len(df)),
        "r2_overall": round(_r2(df["yield_value"], df["predicted"]), 3),
        "median_ape": round(float(df["ape"].median()), 1),
        "mean_ape": round(float(df["ape"].mean()), 1),
        "eval_timestamp": stamp,
        "crops": sorted(df["crop"].unique().tolist()),
    }
    (eval_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    # --- stale sibling trap -----------------------------------------------
    if "stale_sibling" in traps:
        stale = out_dir / f"eval_{_shift_stamp(stamp)}"
        stale.mkdir(parents=True, exist_ok=True)
        shifted: Dict[str, Any] = dict(metrics)
        shifted["r2_overall"] = round(float(metrics["r2_overall"]) - 0.042, 3)
        shifted["median_ape"] = round(float(metrics["median_ape"]) + 1.3, 1)
        shifted["mean_ape"] = round(float(metrics["mean_ape"]) + 2.7, 1)
        shifted["eval_timestamp"] = _shift_stamp(stamp)
        (stale / "metrics.json").write_text(json.dumps(shifted, indent=2))
        per_crop.assign(r2=(per_crop["r2"] - 0.03).round(3)).to_csv(
            stale / "per_crop.csv", index=False
        )

    # --- hashes -----------------------------------------------------------
    hashes = {p.name: _sha256(p) for p in sorted(eval_dir.glob("*")) if p.is_file()}
    (eval_dir / "HASHES.json").write_text(json.dumps(hashes, indent=2))

    truth = {
        "case_id": case_id,
        "eval_dir": str(eval_dir),
        "metrics": metrics,
        "per_crop": per_crop.to_dict(orient="records"),
        "tiers": tier_rows,
        "tier_total": tier_total,
        "record_count": int(len(df)),
        "tier_rows_dropped": dropped,
        "largest_to_smallest_crop_ratio": round(
            float(per_crop["n_records"].iloc[0])
            / float(per_crop["n_records"].iloc[-1]),
            2,
        ),
        "quartiles": qrows,
    }
    return StagedCase(
        case_id=case_id,
        staged_dir=eval_dir,
        truth=truth,
        traps=[{"name": t} for t in traps],
    )


def sample_case(
    evaluated: pd.DataFrame, size: int, rng: np.random.Generator
) -> pd.DataFrame:
    """Draw one case's rows to the report-like crop mix.

    Parameters
    ----------
    evaluated : pd.DataFrame
        Held-out rows with predictions.
    size : int
        Target number of rows in the case.
    rng : np.random.Generator
        Source of randomness; each case jitters the mix slightly so
        two cases never state the same figures.

    Returns
    -------
    pd.DataFrame
        The sampled rows.
    """
    parts = []
    for crop, share in CASE_CROP_MIX.items():
        pool = evaluated[evaluated["crop"] == crop]
        if pool.empty:
            continue
        jitter = float(rng.uniform(0.9, 1.1))
        want = max(5, min(len(pool), int(round(size * share * jitter))))
        parts.append(pool.sample(n=want, random_state=int(rng.integers(0, 1000000))))
    if not parts:
        return evaluated
    return pd.concat(parts).reset_index(drop=True)


def _bias_crop_ratio(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Shape the crop counts so the largest-to-smallest ratio tempts a round-up.

    The colleague's deck stated "cotton ~23x smaller than corn" where
    8,171 / 369 is 22.14. The correct figure rounds down; the report
    rounded up. This trap recreates the opportunity by trimming the
    smallest crop until the ratio's fractional part sits well below a
    half, so a writer that rounds up (or reaches for the next whole
    number) is wrong by a margin the checker can see, while the
    correctly rounded figure passes.

    Parameters
    ----------
    df : pd.DataFrame
        Evaluated rows for one case.
    rng : np.random.Generator
        Source of randomness for the target fraction.

    Returns
    -------
    pd.DataFrame
        The input with the smallest crop trimmed, or unchanged when no
        trim lands the ratio in the target zone.
    """
    counts = df["crop"].value_counts()
    if len(counts) < 2:
        return df
    largest = int(counts.iloc[0])
    smallest_crop = str(counts.index[-1])
    smallest = int(counts.iloc[-1])
    # Trimming can only raise the ratio, so aim at the next whole
    # number up plus a small fraction. That guarantees the target is
    # reachable and lands the fraction well below a half, which is
    # where rounding up is wrong.
    current = largest / smallest
    target_fraction = float(rng.uniform(0.10, 0.40))
    desired_ratio = math.floor(current) + 1 + target_fraction
    keep = int(round(largest / desired_ratio))
    if keep < 5 or keep >= smallest:
        return df
    drop_n = smallest - keep
    droppable = df.index[df["crop"] == smallest_crop]
    to_drop = rng.choice(np.asarray(droppable), size=drop_n, replace=False)
    return df.drop(index=list(to_drop))


def _shift_stamp(stamp: str) -> str:
    """Produce a plausible earlier timestamp for the stale sibling.

    Parameters
    ----------
    stamp : str
        Timestamp of the current evaluation.

    Returns
    -------
    str
        A timestamp one day earlier.
    """
    dt = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    return (dt - timedelta(days=1)).strftime("%Y%m%d_%H%M%S")


def build_parser() -> argparse.ArgumentParser:
    """Build the staging script's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_stage")
    p.add_argument("--source-parquet", required=True)
    p.add_argument("--source", default="nass", choices=sorted(SOURCES))
    p.add_argument("--out-root", required=True)
    p.add_argument("--cases", type=int, default=30)
    p.add_argument("--seed", type=int, default=737)
    p.add_argument("--test-year-lo", type=int, default=2015)
    p.add_argument("--test-year-hi", type=int, default=2024)
    p.add_argument("--case-size", type=int, default=1500)
    p.add_argument("--manifest", default=None)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Stage every case and write the grading manifest.

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
    rng = np.random.default_rng(args.seed)
    out_root = Path(args.out_root).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    src = load_source(Path(args.source_parquet).expanduser(), args.source)
    feats = build_features(src)
    evaluated = fit_and_predict(
        feats, (args.test_year_lo, args.test_year_hi), args.seed
    )
    print(
        f"evaluated rows={len(evaluated)} "
        f"median_ape={evaluated['ape'].median():.1f} "
        f"mean_ape={evaluated['ape'].mean():.1f}"
    )

    trap_pool = ["dropped_tier_rows", "stale_sibling", "rounding_ratio"]
    manifest: Dict[str, Any] = {"cases": [], "seed": args.seed}

    for i in range(args.cases):
        case_id = f"case-{i:03d}"
        # Each case samples a slice so its figures differ from its
        # neighbours; a report that is right for one is wrong for another.
        slice_df = sample_case(evaluated, args.case_size, rng)
        n_traps = int(rng.integers(0, 3))
        traps = (
            list(rng.choice(trap_pool, size=n_traps, replace=False)) if n_traps else []
        )
        staged = stage_case(slice_df, out_root / case_id, case_id, traps=traps, rng=rng)
        manifest["cases"].append(
            {
                "case_id": case_id,
                "staged_dir": str(staged.staged_dir),
                "truth": staged.truth,
                "traps": staged.traps,
            }
        )

    manifest_path = Path(
        args.manifest or (out_root.parent / "manifest.json")
    ).expanduser()
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"staged {args.cases} cases -> {out_root}")
    print(f"manifest (ground truth, never reaches the board) -> {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
