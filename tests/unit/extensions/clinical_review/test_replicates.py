"""
Unit tests for aggregating replicates.

The behaviour that matters most here is what the report refuses to do: it must
not present a pooled interval or a test over replicates of the same cases,
because that would read as power the design does not have.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from clinical_review import replicates

pytestmark = pytest.mark.unit


def _write_run(
    root: Path,
    stamp: str,
    arms: dict[str, tuple[int, int, int]],
    gate_rate: float = 1.0,
    spend: float = 0.2,
    temperature: str = "0.0",
) -> Path:
    """Write a run directory: arms maps arm -> (caught, false_flags, unusable)."""
    run = root / stamp
    (run / "grading").mkdir(parents=True)
    outcomes = {
        "outcomes": {
            "gate": {"adversarial_rate": gate_rate, "adversarial_n": 30},
            "arms": {
                arm: {
                    "flagged": caught,
                    "denominator": 30,
                    "rate": caught / 30,
                    "ci": [0.0, 1.0],
                    "false_flags": ff,
                    "aligned_n": 30,
                    "false_flag_rate": ff / 30,
                    "false_flag_ci": [0.0, 1.0],
                    "indeterminate": unusable,
                }
                for arm, (caught, ff, unusable) in arms.items()
            },
            "pairs": {},
        },
        "readout": {},
        "q4": {},
    }
    (run / "grading" / "outcomes.json").write_text(json.dumps(outcomes))
    (run / "run-config.txt").write_text(
        f"run_at:  {stamp}\ntemperature:  {temperature}\nspend_usd:  {spend}\n"
    )
    for arm, (caught, _ff, _u) in arms.items():
        d = run / "arms" / arm
        d.mkdir(parents=True)
        rows = []
        for i in range(30):
            verdict = "flag" if i < caught else "no_flag"
            rows.append({"chain_id": f"c{i:02d}", "arm": arm, "verdict": verdict})
        (d / "verdicts.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return run


class TestLoading:
    """A run is summarised from its graded output, or skipped."""

    def test_an_ungraded_run_is_skipped_not_guessed(self, tmp_path: Path) -> None:
        """A directory with no grading output contributes nothing."""
        (tmp_path / "20261115T000000").mkdir()
        assert replicates.load_run(tmp_path / "20261115T000000") is None
        assert replicates.aggregate([tmp_path / "20261115T000000"])["runs"] == []

    def test_a_graded_run_carries_its_settings_and_spend(self, tmp_path: Path) -> None:
        """Temperature and spend are read off the run's own record."""
        run = _write_run(tmp_path, "20261115T110221", {"A1": (14, 1, 1)}, spend=0.21)
        summary = replicates.load_run(run)
        assert summary is not None
        assert summary.settings["temperature"] == "0.0"
        assert summary.spend_usd == pytest.approx(0.21)
        assert summary.arms["A1"]["caught"] == 14


class TestSpread:
    """Mean, SD and range, safe at one value."""

    def test_one_value_has_no_standard_deviation(self) -> None:
        """A single run cannot have a spread, and does not pretend to."""
        out = replicates.spread([0.5])
        assert out["n"] == 1 and out["mean"] == 0.5 and out["sd"] is None

    def test_several_values(self) -> None:
        """Mean, SD and range over three runs."""
        out = replicates.spread([0.4, 0.5, 0.6])
        assert out["mean"] == pytest.approx(0.5)
        assert out["sd"] == pytest.approx(0.1)
        assert (out["min"], out["max"]) == (0.4, 0.6)

    def test_no_values(self) -> None:
        """An arm nobody ran reports nothing rather than zero."""
        assert replicates.spread([])["mean"] is None


class TestAgreement:
    """Item-by-item agreement needs at least two runs to mean anything."""

    def test_identical_runs_are_unanimous(self, tmp_path: Path) -> None:
        """Two runs that flagged the same items agree on all 30."""
        for stamp in ("20261115T110000", "20261115T120000"):
            _write_run(tmp_path, stamp, {"A1": (14, 1, 0)})
            # same `caught`, so the same first 14 chains are flagged
        agg = replicates.aggregate(list(tmp_path.iterdir()))
        a = agg["agreement"]["A1"]
        assert a["runs_compared"] == 2
        assert a["unanimous"] == 30 and a["unanimous_rate"] == 1.0
        assert a["split_items"] == 0

    def test_differing_runs_name_the_split_items(self, tmp_path: Path) -> None:
        """A run that caught two more names exactly those two as split."""
        _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 0)})
        _write_run(tmp_path, "20261115T120000", {"A1": (16, 1, 0)})
        a = replicates.aggregate(list(tmp_path.iterdir()))["agreement"]["A1"]
        assert a["unanimous"] == 28 and a["split_items"] == 2
        assert set(a["split_detail"]) == {"c14", "c15"}
        assert a["split_detail"]["c14"] == {"no_flag": 1, "flag": 1}

    def test_one_run_reports_no_agreement_figure(self, tmp_path: Path) -> None:
        """Agreement over a single run is undefined, not 100%."""
        _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 0)})
        a = replicates.aggregate(list(tmp_path.iterdir()))["agreement"]["A1"]
        assert a["unanimous"] is None and a["runs_compared"] == 1


class TestReport:
    """The report states the limitation and omits a pooled interval."""

    @pytest.fixture
    def agg(self, tmp_path: Path) -> dict[str, Any]:
        _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 1), "A2": (23, 2, 9)})
        _write_run(tmp_path, "20261115T120000", {"A1": (15, 1, 0), "A2": (22, 3, 8)})
        _write_run(tmp_path, "20261115T130000", {"A1": (14, 2, 1), "A2": (24, 2, 7)})
        return replicates.aggregate(list(tmp_path.iterdir()))

    def test_arms_carry_mean_sd_and_range(self, agg: dict[str, Any]) -> None:
        """Three runs give a mean, an SD and a range per arm."""
        a1 = agg["arms"]["A1"]["caught_count"]
        assert a1["n"] == 3
        assert a1["mean"] == pytest.approx(14 + 1 / 3)
        assert a1["sd"] is not None
        assert (a1["min"], a1["max"]) == (14.0, 15.0)

    def test_pooled_counts_exist_but_carry_no_interval(
        self, agg: dict[str, Any]
    ) -> None:
        """A pooled count is descriptive; no interval is attached to it."""
        pooled = agg["arms"]["A2"]["pooled"]
        assert pooled == {"caught": 69, "of": 90}
        assert "ci" not in pooled and "interval" not in pooled

    def test_the_report_says_replicates_do_not_raise_power(
        self, agg: dict[str, Any]
    ) -> None:
        """The limitation is in the rendered text, not only in a docstring."""
        text = replicates.render(agg)
        assert "do not raise statistical power" in text
        assert "repeating the same" in text
        assert "no interval attached" in text

    def test_the_report_names_the_arms_and_the_reference_run(
        self, agg: dict[str, Any]
    ) -> None:
        """Every arm with data appears, and the earliest run is the reference."""
        text = replicates.render(agg)
        assert "| A1 |" in text and "| A2 |" in text
        assert agg["reference_run"] == "20261115T110000"
        assert "20261115T110000" in text
        assert "$0.6000" in text or "0.60" in text

    def test_an_empty_set_says_so(self) -> None:
        """No graded run renders a statement, not an empty table."""
        assert "No graded run was found" in replicates.render(
            {"runs": [], "arms": {}, "agreement": {}, "gate": {}, "spend": {}}
        )

    def test_cli_writes_the_report_and_the_json(self, tmp_path: Path) -> None:
        """The CLI writes REPLICATES.md and replicates.json beside it."""
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_run(runs, "20261115T110000", {"A1": (14, 1, 1)})
        _write_run(runs, "20261115T120000", {"A1": (15, 1, 0)})
        out = tmp_path / "REPLICATES.md"
        assert replicates.main([str(runs), "--out", str(out)]) == 0
        assert "do not raise statistical power" in out.read_text()
        data = json.loads((tmp_path / "replicates.json").read_text())
        assert len(data["runs"]) == 2


class TestGateKeys:
    """The gate rate is read whichever of the two key names it carries."""

    def test_the_graders_key_name_is_read(self, tmp_path: Path) -> None:
        """outcomes.json writes `adversarial_harmful_rate`, not `adversarial_rate`.

        Reading only the latter made the first aggregation report that it had
        compared zero runs while happily printing the arms table.
        """
        for stamp in ("20261115T110000", "20261115T120000"):
            run = _write_run(tmp_path, stamp, {"A1": (14, 1, 0)})
            outcomes = json.loads((run / "grading" / "outcomes.json").read_text())
            outcomes["outcomes"]["gate"] = {
                "adversarial_harmful_rate": 1.0,
                "adversarial_n": 30,
            }
            (run / "grading" / "outcomes.json").write_text(json.dumps(outcomes))
        gate = replicates.aggregate(list(tmp_path.iterdir()))["gate"]
        assert gate["n"] == 2 and gate["mean"] == pytest.approx(1.0)

    def test_the_runners_key_name_is_also_read(self, tmp_path: Path) -> None:
        """gate.json's `adversarial_rate` still works, for a hand-built set."""
        for stamp in ("20261115T110000", "20261115T120000"):
            _write_run(tmp_path, stamp, {"A1": (14, 1, 0)}, gate_rate=0.95)
        gate = replicates.aggregate(list(tmp_path.iterdir()))["gate"]
        assert gate["n"] == 2 and gate["mean"] == pytest.approx(0.95)
