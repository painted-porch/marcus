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
    completed: str = "yes",
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
        f"run_at:  {stamp}\ntemperature:  {temperature}\n"
        f"spend_usd:  {spend}\ncompleted:  {completed}\n"
    )
    for arm, (caught, ff, _u) in arms.items():
        d = run / "arms" / arm
        d.mkdir(parents=True)
        rows = []
        # A real run carries 60 verdicts per arm: 30 cases in each of the two
        # modes. An earlier fixture wrote 30, which made every run look
        # unfinished to the completeness check.
        for i in range(30):
            rows.append(
                {
                    "chain_id": f"a{i:02d}",
                    "arm": arm,
                    "verdict": "flag" if i < caught else "no_flag",
                }
            )
        for i in range(30):
            rows.append(
                {
                    "chain_id": f"l{i:02d}",
                    "arm": arm,
                    "verdict": "flag" if i < ff else "no_flag",
                }
            )
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
        assert a["unanimous"] == 60 and a["unanimous_rate"] == 1.0
        assert a["split_items"] == 0

    def test_differing_runs_name_the_split_items(self, tmp_path: Path) -> None:
        """A run that caught two more names exactly those two as split."""
        _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 0)})
        _write_run(tmp_path, "20261115T120000", {"A1": (16, 1, 0)})
        a = replicates.aggregate(list(tmp_path.iterdir()))["agreement"]["A1"]
        assert a["unanimous"] == 58 and a["split_items"] == 2
        assert set(a["split_detail"]) == {"a14", "a15"}
        assert a["split_detail"]["a14"] == {"no_flag": 1, "flag": 1}

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


class TestCompleteness:
    """A run that stopped part-way must not be averaged in as a low score."""

    def test_an_incomplete_run_is_excluded_by_default(self, tmp_path: Path) -> None:
        """Its unperformed checks would read as misses and drag every rate down."""
        _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 0)})
        _write_run(tmp_path, "20261115T120000", {"A1": (1, 0, 29)}, completed="no")
        agg = replicates.aggregate(list(tmp_path.iterdir()))
        assert len(agg["runs"]) == 1
        assert agg["arms"]["A1"]["caught_count"]["mean"] == pytest.approx(14.0)

    def test_an_incomplete_run_can_be_included_deliberately(
        self, tmp_path: Path
    ) -> None:
        """A caller investigating a halted run can ask for it."""
        _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 0)})
        _write_run(tmp_path, "20261115T120000", {"A1": (1, 0, 29)}, completed="no")
        agg = replicates.aggregate(list(tmp_path.iterdir()), include_incomplete=True)
        assert len(agg["runs"]) == 2
        assert agg["arms"]["A1"]["caught_count"]["mean"] == pytest.approx(7.5)

    def test_a_run_with_no_completion_marker_is_excluded(self, tmp_path: Path) -> None:
        """Older runs predate the marker; they are not silently treated as good."""
        run = _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 0)})
        (run / "run-config.txt").write_text("run_at:  20261115T110000\n")
        assert replicates.load_run(run) is None
        assert replicates.load_run(run, include_incomplete=True) is not None


class TestCompletionFromEvidence:
    """Completion is established from the record when no marker is present."""

    def _full_run(self, root: Path, stamp: str, marker: str | None) -> Path:
        run = _write_run(
            root,
            stamp,
            {a: (14, 1, 0) for a in ("A1", "C", "A2", "A3")},
            completed=marker or "",
        )
        (run / "RESULTS.md").write_text("# results\n")
        if marker is None:
            (run / "run-config.txt").write_text(f"run_at:  {stamp}\nspend_usd:  0.2\n")
        return run

    def test_a_run_predating_the_marker_counts_when_its_record_is_complete(
        self, tmp_path: Path
    ) -> None:
        """The marker arrived mid-experiment; earlier runs are not discarded."""
        run = self._full_run(tmp_path, "20261114T060702", None)
        assert replicates.completed(run) is True
        assert replicates.load_run(run) is not None

    def test_a_missing_arm_means_incomplete_whatever_the_config_says(
        self, tmp_path: Path
    ) -> None:
        """Three arms of four is not a finished run."""
        run = self._full_run(tmp_path, "20261115T110000", None)
        (run / "arms" / "A3" / "verdicts.jsonl").unlink()
        assert replicates.completed(run) is False

    def test_a_short_arm_means_incomplete(self, tmp_path: Path) -> None:
        """A stop line mid-arm leaves fewer than 60 verdicts."""
        run = self._full_run(tmp_path, "20261115T110000", None)
        f = run / "arms" / "A2" / "verdicts.jsonl"
        f.write_text("\n".join(f.read_text().splitlines()[:40]) + "\n")
        assert replicates.completed(run) is False

    def test_a_stopped_line_means_incomplete(self, tmp_path: Path) -> None:
        """A recorded stop beats the evidence."""
        run = self._full_run(tmp_path, "20261115T110000", None)
        with (run / "run-config.txt").open("a") as handle:
            handle.write("stopped:  stop line reached\n")
        assert replicates.completed(run) is False

    def test_an_explicit_marker_wins_over_the_evidence(self, tmp_path: Path) -> None:
        """A run that says no is taken at its word."""
        run = self._full_run(tmp_path, "20261115T110000", "no")
        (run / "RESULTS.md").write_text("# results\n")
        assert replicates.completed(run) is False

    def test_no_results_file_means_incomplete(self, tmp_path: Path) -> None:
        """Grading writes RESULTS.md last, so its absence is decisive."""
        run = self._full_run(tmp_path, "20261115T110000", None)
        (run / "RESULTS.md").unlink()
        assert replicates.completed(run) is False


class TestDecomposition:
    """Splitting run-to-run variation into checker and adviser parts."""

    def _set(self, root: Path, counts: list[int], name: str) -> dict[str, Any]:
        """Build a set of runs whose A2 catch counts are the ones given."""
        d = root / name
        d.mkdir()
        for i, caught in enumerate(counts):
            _write_run(d, f"2026111{i}T110000", {"A2": (caught, 1, 0)})
        return replicates.aggregate(list(d.iterdir()))

    def test_a_flat_frozen_set_puts_the_variation_on_the_adviser(
        self, tmp_path: Path
    ) -> None:
        """The checker never moves, so everything left is the advice."""
        regenerated = self._set(tmp_path, [18, 20, 22, 23], "regen")
        frozen = self._set(tmp_path, [21, 21, 21, 21], "frozen")
        dec = replicates.decompose(regenerated, frozen)
        a2 = dec["arms"]["A2"]
        assert a2["checker_sd"] == pytest.approx(0.0)
        assert a2["adviser_sd"] == pytest.approx(a2["total_sd"])
        assert a2["dominant_source"] == "the adviser"
        assert replicates.render_decomposition(dec).count("| A2 |") == 1

    def test_a_frozen_set_as_spread_as_the_other_puts_it_on_the_checker(
        self, tmp_path: Path
    ) -> None:
        """Identical advice, same spread: the checker is doing it."""
        regenerated = self._set(tmp_path, [18, 20, 22, 23], "regen")
        frozen = self._set(tmp_path, [18, 20, 22, 23], "frozen")
        dec = replicates.decompose(regenerated, frozen)
        a2 = dec["arms"]["A2"]
        assert a2["checker_sd"] == pytest.approx(a2["total_sd"])
        assert a2["adviser_sd"] == pytest.approx(0.0)
        assert a2["dominant_source"] == "the checker"

    def test_a_frozen_set_more_spread_reports_nothing_rather_than_a_root_of_a_negative(
        self, tmp_path: Path
    ) -> None:
        """Noise can invert the two; say so instead of inventing a number."""
        regenerated = self._set(tmp_path, [20, 21, 21, 22], "regen")
        frozen = self._set(tmp_path, [17, 20, 22, 25], "frozen")
        dec = replicates.decompose(regenerated, frozen)
        a2 = dec["arms"]["A2"]
        assert a2["adviser_sd"] is None
        assert "cannot be separated" in a2["note"]
        assert "cannot be separated" in replicates.render_decomposition(dec)

    def test_a_single_run_set_has_no_spread_to_split(self, tmp_path: Path) -> None:
        """One run gives no standard deviation, so no split is claimed."""
        regenerated = self._set(tmp_path, [18, 22], "regen")
        frozen = self._set(tmp_path, [21], "frozen")
        dec = replicates.decompose(regenerated, frozen)
        assert dec["arms"]["A2"]["adviser_sd"] is None
        assert "one run" in dec["arms"]["A2"]["note"]

    def test_the_rendered_table_names_what_each_column_measures(
        self, tmp_path: Path
    ) -> None:
        """A reader must not have to guess which SD is which."""
        text = replicates.render_decomposition(
            replicates.decompose(
                self._set(tmp_path, [18, 20, 22, 23], "regen"),
                self._set(tmp_path, [21, 21, 21, 21], "frozen"),
            )
        )
        assert "SD, checker alone" in text
        assert "the identical 60 recommendations" in text
        assert "holds the checker and the adviser together" in text


class TestSuperseded:
    """A run invalidated by a defect found afterwards is excluded by default."""

    def test_a_superseded_run_is_excluded_even_when_complete(
        self, tmp_path: Path
    ) -> None:
        """Deviation 12: the truncation defect made six complete runs unusable."""
        good = _write_run(tmp_path, "20261116T110000", {"A1": (18, 1, 0)})
        (good / "RESULTS.md").write_text("# r\n")
        bad = _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 9)})
        (bad / "RESULTS.md").write_text("# r\n")
        with (bad / "run-config.txt").open("a") as handle:
            handle.write("superseded:  600-token truncation defect, deviation 12\n")
        assert replicates.completed(bad) is False
        agg = replicates.aggregate(list(tmp_path.iterdir()))
        assert [r["stamp"] for r in agg["runs"]] == ["20261116T110000"]

    def test_a_superseded_run_can_still_be_inspected_deliberately(
        self, tmp_path: Path
    ) -> None:
        """Its rows are not deleted; a reader can ask for them."""
        bad = _write_run(tmp_path, "20261115T110000", {"A1": (14, 1, 9)})
        with (bad / "run-config.txt").open("a") as handle:
            handle.write("superseded:  deviation 12\n")
        assert replicates.load_run(bad, include_incomplete=True) is not None


class TestUndefinedFalseFlagRates:
    """Codex P2 on PR #922: a run with no usable aligned check broke the report."""

    def test_an_undefined_rate_is_left_out_and_counted(self, tmp_path: Path) -> None:
        """Two runs, one with no measurable A1 false-flag rate: n is 1, no crash."""
        for stamp in ("20261115T000001", "20261115T000002"):
            _write_run(tmp_path, stamp, {"A1": (15, 1, 0), "C": (25, 2, 0)})
        target = tmp_path / "20261115T000002" / "grading" / "outcomes.json"
        data = json.loads(target.read_text())
        data["outcomes"]["arms"]["A1"]["false_flag_rate"] = None
        target.write_text(json.dumps(data))
        agg = replicates.aggregate(sorted(tmp_path.iterdir()))
        assert agg["arms"]["A1"]["false_flag_rate"]["n"] == 1

    def test_the_report_still_renders(self, tmp_path: Path) -> None:
        """The undefined rate does not stop the whole report."""
        _write_run(tmp_path, "20261115T000001", {"A1": (15, 1, 0)})
        target = tmp_path / "20261115T000001" / "grading" / "outcomes.json"
        data = json.loads(target.read_text())
        data["outcomes"]["arms"]["A1"]["false_flag_rate"] = None
        target.write_text(json.dumps(data))
        text = replicates.render(replicates.aggregate([tmp_path / "20261115T000001"]))
        assert "| A1 |" in text


@pytest.mark.unit
class TestCodeVersion:
    """The report has to be able to say which commit each run executed."""

    def test_reads_the_short_sha_from_the_checkout_line(self) -> None:
        """A clean checkout reports its sha and nothing else."""
        settings = {
            "checkout": "/checkouts/wt @ "
            "53c0851e6db35245b4fe244e010255d87a6d3845 (0 dirty)"
        }
        assert replicates.code_version(settings) == "53c0851e"

    def test_marks_a_dirty_checkout(self) -> None:
        """Uncommitted files make a run less reproducible, so they are shown."""
        settings = {
            "checkout": "/checkouts/wt @ "
            "5003bd7722f235c2f4b829079d9725064514caad (2 dirty)"
        }
        assert replicates.code_version(settings) == "5003bd77+2dirty"

    def test_says_so_when_no_commit_was_recorded(self) -> None:
        """A missing line is reported as missing, never as a match."""
        assert replicates.code_version({}) == "not recorded"
        assert replicates.code_version({"checkout": "/checkouts/wt"}) == "not recorded"


@pytest.mark.unit
class TestComparabilityClaim:
    """The opening claim must follow the record, not be asserted."""

    def test_one_commit_is_called_the_same_code(self) -> None:
        """Runs that match may be read as provider non-determinism."""
        agg = {
            "runs": [
                {
                    "stamp": "a",
                    "temperature": "0.0",
                    "code": "53c0851e",
                    "spend_usd": 0.1,
                },
                {
                    "stamp": "b",
                    "temperature": "0.0",
                    "code": "53c0851e",
                    "spend_usd": 0.1,
                },
            ],
            "arms": {},
            "agreement": {},
            "gate": replicates.spread([1.0, 1.0]),
            "spend": {"total_usd": 0.2},
            "reference_run": "a",
        }
        out = replicates.render(agg)
        assert "the same code" in out
        assert "did NOT all execute the same code" not in out

    def test_differing_commits_are_disclosed(self) -> None:
        """A set spanning commits says so and states the limit it imposes."""
        agg = {
            "runs": [
                {
                    "stamp": "a",
                    "temperature": "0.0",
                    "code": "53c0851e",
                    "spend_usd": 0.1,
                },
                {
                    "stamp": "b",
                    "temperature": "0.0",
                    "code": "735a3f8f",
                    "spend_usd": 0.1,
                },
            ],
            "arms": {},
            "agreement": {},
            "gate": replicates.spread([1.0, 1.0]),
            "spend": {"total_usd": 0.2},
            "reference_run": "a",
        }
        out = replicates.render(agg)
        assert "did NOT all execute the same code" in out
        assert "re-graded" in out


@pytest.mark.unit
class TestDecompositionWithoutFrozenRuns:
    """An unmeasured split must not render as an empty table under prose."""

    def test_reports_its_own_absence(self) -> None:
        """With no frozen run the section says the split is not measured."""
        out = replicates.render_decomposition(
            {"runs": {"regenerated": 4, "frozen": 0}, "arms": {}}
        )
        assert "NOT" in out
        assert "| Arm | Mean, regenerated" not in out

    def test_also_when_arms_are_empty(self) -> None:
        """A frozen count with no per-arm figures is still not a measurement."""
        out = replicates.render_decomposition(
            {"runs": {"regenerated": 4, "frozen": 2}, "arms": {}}
        )
        assert "NOT" in out
        assert "| Arm | Mean, regenerated" not in out


class TestTemperatureAndCodeAreReportedSeparately:
    """Codex P2 on PR #937: a temperature difference is not a code difference."""

    def _agg(self, temps: tuple[str, str], codes: tuple[str, str]) -> dict[str, Any]:
        return {
            "runs": [
                {
                    "stamp": "a",
                    "temperature": temps[0],
                    "code": codes[0],
                    "spend_usd": 0.1,
                },
                {
                    "stamp": "b",
                    "temperature": temps[1],
                    "code": codes[1],
                    "spend_usd": 0.1,
                },
            ],
            "arms": {},
            "agreement": {},
            "gate": replicates.spread([1.0, 1.0]),
            "spend": {"total_usd": 0.2},
            "reference_run": "a",
        }

    def test_a_temperature_difference_is_named_as_such(self) -> None:
        """Different temperatures, same code: the report says temperature."""
        out = replicates.render(self._agg(("0.0", "1.0"), ("abc", "abc")))
        assert "did NOT all use the same temperature" in out

    def test_it_is_not_misreported_as_a_code_difference(self) -> None:
        """Same code is not described as differing code."""
        out = replicates.render(self._agg(("0.0", "1.0"), ("abc", "abc")))
        assert "did NOT all execute the same code" not in out

    def test_equal_settings_are_not_claimed_when_temperatures_differ(self) -> None:
        """The old sentence claimed identical settings whatever the temperatures."""
        out = replicates.render(self._agg(("0.0", "1.0"), ("abc", "def")))
        assert "The settings were the same in every run" not in out
