"""Unit tests for the numeric-verifier staging module.

Staging produces the artifacts a report is written from and plants
the errors the experiment measures. These tests pin the tier
boundaries, the crop mix a case is drawn to, and the contract each
trap promises the grader.
"""

import json

import numpy as np
import pandas as pd
import pytest
from runners.numeric_stage import (
    CASE_CROP_MIX,
    _bias_crop_ratio,
    _r2,
    _tier_for,
    sample_case,
    stage_case,
)


@pytest.fixture
def evaluated():
    """Build a synthetic evaluated frame with a report-like crop mix.

    Returns
    -------
    pd.DataFrame
        Rows with actuals, predictions and errors for four crops.
    """
    rng = np.random.default_rng(0)
    rows = []
    for crop, n in [("maize", 600), ("soybeans", 400), ("wheat", 60), ("cotton", 40)]:
        for i in range(n):
            actual = float(rng.uniform(1000, 5000))
            ape = float(rng.gamma(2.0, 3.0))
            rows.append(
                {
                    "crop": crop,
                    "area": f"{crop}-land-{i}",
                    "year": 2020,
                    "yield_value": actual,
                    "predicted": actual * (1 + ape / 100.0),
                    "area_value": float(rng.uniform(1000, 500000)),
                    "unit": "BU / ACRE",
                    "ape": ape,
                }
            )
    return pd.DataFrame(rows)


class TestTierBoundaries:
    """Tier edges are closed below and open above."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "ape,expected",
        [
            (0.0, "Excellent"),
            (4.99, "Excellent"),
            (5.0, "Good"),
            (9.99, "Good"),
            (10.0, "Average"),
            (19.99, "Average"),
            (20.0, "Poor"),
            (500.0, "Poor"),
        ],
    )
    def test_tier_assignment(self, ape, expected):
        """Each error lands in the tier its boundary implies."""
        assert _tier_for(ape) == expected


class TestRSquared:
    """R-squared follows the standard definition."""

    @pytest.mark.unit
    def test_perfect_prediction_is_one(self):
        """Predictions equal to actuals give one."""
        s = pd.Series([1.0, 2.0, 3.0])
        assert _r2(s, s) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_constant_actual_is_nan(self):
        """A constant series has no variance to explain."""
        s = pd.Series([2.0, 2.0, 2.0])
        assert np.isnan(_r2(s, pd.Series([1.0, 2.0, 3.0])))


class TestCaseSampling:
    """A case is drawn to the colleague's uneven crop mix."""

    @pytest.mark.unit
    def test_largest_crop_dominates_the_case(self, evaluated):
        """The largest crop holds roughly the configured share."""
        got = sample_case(evaluated, 1000, np.random.default_rng(1))
        share = (got["crop"] == "maize").sum() / len(got)
        assert share == pytest.approx(CASE_CROP_MIX["maize"], abs=0.08)

    @pytest.mark.unit
    def test_smallest_crop_stays_a_small_minority(self, evaluated):
        """The smallest crop stays under five percent, as in the report."""
        got = sample_case(evaluated, 1000, np.random.default_rng(1))
        assert (got["crop"] == "cotton").sum() / len(got) < 0.05

    @pytest.mark.unit
    def test_ratio_is_report_like(self, evaluated):
        """The largest-to-smallest ratio lands in the report's range."""
        got = sample_case(evaluated, 1000, np.random.default_rng(2))
        counts = got["crop"].value_counts()
        assert 10 < counts.iloc[0] / counts.iloc[-1] < 40


class TestRoundingRatioTrap:
    """The trap guarantees a ratio where rounding up is wrong."""

    @pytest.mark.unit
    def test_trap_moves_fraction_below_a_half(self, evaluated):
        """After the trim the ratio's fraction sits below one half."""
        case = sample_case(evaluated, 1000, np.random.default_rng(3))
        biased = _bias_crop_ratio(case, np.random.default_rng(4))
        counts = biased["crop"].value_counts()
        ratio = counts.iloc[0] / counts.iloc[-1]
        assert ratio - int(ratio) < 0.5

    @pytest.mark.unit
    def test_trap_only_removes_rows(self, evaluated):
        """The trim never invents data."""
        case = sample_case(evaluated, 1000, np.random.default_rng(3))
        biased = _bias_crop_ratio(case, np.random.default_rng(4))
        assert len(biased) <= len(case)
        assert set(biased.index).issubset(set(case.index))


class TestStagedArtifacts:
    """Staging writes the artifact family a report is written from."""

    @pytest.mark.unit
    def test_every_expected_artifact_is_written(self, evaluated, tmp_path):
        """All five tables plus the hash file appear."""
        staged = stage_case(evaluated, tmp_path, "case-000")
        names = {p.name for p in staged.staged_dir.glob("*")}
        assert names >= {
            "metrics.json",
            "per_crop.csv",
            "per_record.csv",
            "tiers.csv",
            "quartiles.csv",
            "HASHES.json",
        }

    @pytest.mark.unit
    def test_tier_total_equals_record_count_without_the_trap(self, evaluated, tmp_path):
        """An untrapped case satisfies the consistency relation."""
        staged = stage_case(evaluated, tmp_path, "case-000")
        tiers = pd.read_csv(staged.staged_dir / "tiers.csv")
        metrics = json.loads((staged.staged_dir / "metrics.json").read_text())
        assert int(tiers["n_records"].sum()) == metrics["n_records"]

    @pytest.mark.unit
    def test_dropped_tier_rows_trap_breaks_the_relation(self, evaluated, tmp_path):
        """The planted case reproduces the 14,253 mismatch."""
        staged = stage_case(
            evaluated,
            tmp_path,
            "case-000",
            traps=["dropped_tier_rows"],
            rng=np.random.default_rng(5),
        )
        tiers = pd.read_csv(staged.staged_dir / "tiers.csv")
        metrics = json.loads((staged.staged_dir / "metrics.json").read_text())
        assert int(tiers["n_records"].sum()) < metrics["n_records"]
        assert staged.truth["tier_rows_dropped"] > 0

    @pytest.mark.unit
    def test_stale_sibling_trap_writes_a_second_directory(self, evaluated, tmp_path):
        """A stale evaluation sits beside the current one."""
        staged = stage_case(
            evaluated,
            tmp_path,
            "case-000",
            traps=["stale_sibling"],
            rng=np.random.default_rng(6),
        )
        siblings = [
            d for d in tmp_path.glob("eval_*") if d.name != staged.staged_dir.name
        ]
        assert len(siblings) == 1
        stale = json.loads((siblings[0] / "metrics.json").read_text())
        current = json.loads((staged.staged_dir / "metrics.json").read_text())
        assert stale["r2_overall"] != current["r2_overall"]

    @pytest.mark.unit
    def test_truth_is_returned_not_staged(self, evaluated, tmp_path):
        """Ground truth never lands inside the staged directory."""
        staged = stage_case(evaluated, tmp_path, "case-000")
        staged_text = " ".join(p.read_text() for p in staged.staged_dir.glob("*"))
        assert "largest_to_smallest_crop_ratio" not in staged_text
        assert "largest_to_smallest_crop_ratio" in staged.truth


@pytest.fixture
def raw_nass(tmp_path):
    """Write a small USDA-shaped extract to parquet.

    Returns
    -------
    pathlib.Path
        Path to the parquet file.
    """
    rng = np.random.default_rng(11)
    rows = []
    counties = [
        f"{state}-C{n}"
        for state in ("IA", "IL", "KS", "TX", "NE", "MN")
        for n in range(3)
    ]
    for county in counties:
        for crop, unit, base in (
            ("maize", "BU / ACRE", 180.0),
            ("soybeans", "BU / ACRE", 55.0),
            ("cotton", "LB / ACRE", 900.0),
        ):
            for year in range(2010, 2024):
                rows.append(
                    {
                        "area": county,
                        "crop": crop,
                        "year": year,
                        "yield_per_acre": (
                            base + year % 7 + float(rng.normal(0, base * 0.05))
                        ),
                        "acres_harvested": 5000.0 + year * 10,
                        "unit": unit,
                    }
                )
    # One total crop failure, which the yield floor must exclude.
    rows.append(
        {
            "area": "ND-CASS",
            "crop": "maize",
            "year": 2015,
            "yield_per_acre": 0.4,
            "acres_harvested": 900.0,
            "unit": "BU / ACRE",
        }
    )
    path = tmp_path / "src.parquet"
    pd.DataFrame(rows).to_parquet(path)
    return path


class TestSourceLoading:
    """Loading pivots the long export and drops undefined-error rows."""

    @pytest.mark.unit
    def test_pivot_gives_one_row_per_unit(self, raw_nass):
        """Each area, crop and year becomes a single row."""
        from runners.numeric_stage import load_source

        got = load_source(raw_nass)
        assert set(got.columns) >= {"area", "crop", "year", "yield_value", "area_value"}
        assert len(got) == len(got[["area", "crop", "year"]].drop_duplicates())

    @pytest.mark.unit
    def test_crop_names_are_mapped_to_short_labels(self, raw_nass):
        """Export item names become the labels a report uses."""
        from runners.numeric_stage import load_source

        assert set(load_source(raw_nass)["crop"]) <= {
            "maize",
            "soybeans",
            "wheat",
            "cotton",
        }

    @pytest.mark.unit
    def test_crop_failure_row_is_excluded(self, raw_nass):
        """A near-zero yield would put a near-zero denominator under
        every percentage error, so it is dropped."""
        from runners.numeric_stage import MIN_YIELD_VALUE, load_source

        got = load_source(raw_nass)
        assert (got["yield_value"] >= MIN_YIELD_VALUE).all()
        assert "ND-CASS" not in set(got["area"])


class TestFeatureBuilding:
    """The model trains on the previous year's observation."""

    @pytest.mark.unit
    def test_previous_year_is_attached(self, raw_nass):
        """Each row carries the prior year's yield for its series."""
        from runners.numeric_stage import build_features, load_source

        got = build_features(load_source(raw_nass))
        assert {"prev_yield", "prev_area"} <= set(got.columns)
        assert got["prev_yield"].notna().all()

    @pytest.mark.unit
    def test_first_year_of_each_series_is_dropped(self, raw_nass):
        """The earliest year has no predecessor to learn from."""
        from runners.numeric_stage import build_features, load_source

        src = load_source(raw_nass)
        got = build_features(src)
        for (_, _), group in got.groupby(["area", "crop"]):
            assert group["year"].min() > src["year"].min()


class TestEvaluation:
    """Prediction is held out by year, never by random split."""

    @pytest.mark.unit
    def test_only_held_out_years_are_returned(self, raw_nass):
        """Rows outside the test window do not appear in the result."""
        from runners.numeric_stage import (
            build_features,
            fit_and_predict,
            load_source,
        )

        got = fit_and_predict(build_features(load_source(raw_nass)), (2018, 2020), 1)
        assert got["year"].between(2018, 2020).all()

    @pytest.mark.unit
    def test_errors_are_computed_as_percentages(self, raw_nass):
        """Every held-out row carries a non-negative percentage error."""
        from runners.numeric_stage import (
            build_features,
            fit_and_predict,
            load_source,
        )

        got = fit_and_predict(build_features(load_source(raw_nass)), (2018, 2020), 1)
        assert (got["ape"] >= 0).all()
        assert "predicted" in got.columns


class TestStagingEntryPoint:
    """The command stages cases and writes ground truth outside them."""

    @pytest.mark.unit
    def test_main_stages_cases_and_manifest(self, raw_nass, tmp_path):
        """A run produces one directory per case plus a manifest."""
        from runners.numeric_stage import main

        out_root = tmp_path / "staged"
        manifest = tmp_path / "manifest.json"
        code = main(
            [
                "--source-parquet",
                str(raw_nass),
                "--out-root",
                str(out_root),
                "--cases",
                "2",
                "--seed",
                "1",
                "--case-size",
                "40",
                "--test-year-lo",
                "2018",
                "--test-year-hi",
                "2020",
                "--manifest",
                str(manifest),
            ]
        )
        assert code == 0
        assert {p.name for p in out_root.glob("case-*")} == {"case-000", "case-001"}
        written = json.loads(manifest.read_text())
        assert len(written["cases"]) == 2
        assert written["seed"] == 1


class TestCodexFindingStaleTimestampDayBoundary:
    """P2: _shift_stamp left month/year boundaries unchanged.

    max(1, dt.day - 1) is a no-op on the first of any month. For a
    case with the stale_sibling trap on such a day, stage_case then
    reused the current evaluation's own directory name as the "stale"
    one and overwrote its metrics.json and per_crop.csv with
    perturbed values -- corrupting the current source artifacts
    rather than creating a stale sibling beside them. The manifest
    still records the original, uncorrupted values, so the staged
    data and the ground truth would have silently diverged.
    """

    @pytest.mark.unit
    def test_first_of_the_month_still_shifts_to_a_distinct_timestamp(self):
        """A day-1 timestamp must not come back unchanged."""
        from runners.numeric_stage import _shift_stamp

        stamp = "20261101_120000"
        assert _shift_stamp(stamp) != stamp

    @pytest.mark.unit
    def test_first_of_the_month_shifts_into_the_prior_month(self):
        """The result is a real earlier calendar day, not a clamp to day 1."""
        from datetime import datetime

        from runners.numeric_stage import _shift_stamp

        stamp = "20261101_120000"
        shifted = datetime.strptime(_shift_stamp(stamp), "%Y%m%d_%H%M%S")
        original = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        assert shifted < original
        assert (original - shifted).days == 1

    @pytest.mark.unit
    def test_ordinary_midmonth_day_still_shifts_back_one(self):
        """The fix must not change the already-correct ordinary case."""
        from runners.numeric_stage import _shift_stamp

        assert _shift_stamp("20261115_120000") == "20261114_120000"
