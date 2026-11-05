"""Unit tests for the numeric-verifier seeding runner.

The seeding runner writes the five-task chain per case directly onto
a SQLite board and keeps ground truth off it. These tests pin the
task chain's shape, its dependency wiring with and without an
approval sample, and that nothing a worker could read back gives away
the answer.
"""

import json

import pytest
from runners.numeric_runner import (
    build_case_spec,
    discover_cases,
    sample_approvals,
)


@pytest.fixture
def staged_case(tmp_path):
    """Lay out one staged case directory as numeric_stage.py would.

    Returns
    -------
    pathlib.Path
        The case's evaluation directory.
    """
    case_dir = tmp_path / "case-000"
    eval_dir = case_dir / "eval_20261103_120000"
    eval_dir.mkdir(parents=True)
    (eval_dir / "metrics.json").write_text(json.dumps({"n_records": 10}))
    return eval_dir


class TestCaseDiscovery:
    """Cases are found by walking the staged root."""

    @pytest.mark.unit
    def test_finds_every_case_directory(self, tmp_path):
        """One entry per case subdirectory."""
        for name in ("case-000", "case-001"):
            (tmp_path / name / "eval_1").mkdir(parents=True)
        found = discover_cases(tmp_path)
        assert [c for c, _ in found] == ["case-000", "case-001"]

    @pytest.mark.unit
    def test_picks_the_newest_evaluation(self, tmp_path):
        """A stale sibling evaluation is not what gets seeded."""
        case_dir = tmp_path / "case-000"
        (case_dir / "eval_20261101_000000").mkdir(parents=True)
        (case_dir / "eval_20261103_000000").mkdir(parents=True)
        found = discover_cases(tmp_path)
        assert found[0][1].name == "eval_20261103_000000"

    @pytest.mark.unit
    def test_a_case_with_no_evaluation_is_skipped(self, tmp_path):
        """An empty case directory contributes nothing."""
        (tmp_path / "case-999").mkdir()
        assert discover_cases(tmp_path) == []


class TestApprovalSampling:
    """The human-review sample is deterministic."""

    @pytest.mark.unit
    def test_same_seed_gives_the_same_sample(self):
        """A rerun samples identically."""
        ids = [f"case-{i:03d}" for i in range(20)]
        a = sample_approvals(ids, 0.2, seed=737)
        b = sample_approvals(ids, 0.2, seed=737)
        assert a == b

    @pytest.mark.unit
    def test_rate_zero_samples_nothing(self):
        """No approval tasks when the rate is zero."""
        ids = [f"case-{i:03d}" for i in range(10)]
        assert sample_approvals(ids, 0.0, seed=1) == set()

    @pytest.mark.unit
    def test_sample_size_matches_the_rate(self):
        """Roughly rate times the case count is sampled."""
        ids = [f"case-{i:03d}" for i in range(50)]
        sampled = sample_approvals(ids, 0.2, seed=1)
        assert sampled <= set(ids)
        assert len(sampled) == 10


class TestCaseSpec:
    """The five-task chain and its dependency wiring."""

    @pytest.mark.unit
    def test_unsampled_case_has_four_tasks(self, staged_case):
        """No approve task when the case was not sampled."""
        spec = build_case_spec("case-000", staged_case, False, "proj")
        roles = [role for role, _, _ in spec]
        assert roles == ["find", "verify_report", "synthesize", "verify_slides"]

    @pytest.mark.unit
    def test_sampled_case_has_five_tasks(self, staged_case):
        """An approve task is inserted for a sampled case."""
        spec = build_case_spec("case-000", staged_case, True, "proj")
        roles = [role for role, _, _ in spec]
        assert roles == [
            "find",
            "verify_report",
            "approve",
            "synthesize",
            "verify_slides",
        ]

    @pytest.mark.unit
    def test_synthesize_depends_on_approve_when_sampled(self, staged_case):
        """Synthesize waits on the human, not the raw verify, when sampled."""
        spec = build_case_spec("case-000", staged_case, True, "proj")
        by_role = {role: deps for role, _, deps in spec}
        assert by_role["synthesize"] == ["approve"]

    @pytest.mark.unit
    def test_synthesize_depends_on_verify_when_not_sampled(self, staged_case):
        """Synthesize waits directly on verify when there is no approval."""
        spec = build_case_spec("case-000", staged_case, False, "proj")
        by_role = {role: deps for role, _, deps in spec}
        assert by_role["synthesize"] == ["verify_report"]

    @pytest.mark.unit
    def test_verify_slides_depends_only_on_synthesize(self, staged_case):
        """The slide check runs after the deck exists, not after approval."""
        spec = build_case_spec("case-000", staged_case, True, "proj")
        by_role = {role: deps for role, _, deps in spec}
        assert by_role["verify_slides"] == ["synthesize"]

    @pytest.mark.unit
    def test_find_task_carries_the_staged_directory(self, staged_case):
        """The writer is told where to read, and nothing more."""
        spec = build_case_spec("case-000", staged_case, False, "proj")
        find_data = next(data for role, data, _ in spec if role == "find")
        assert find_data["inputs"]["staged_dir"] == str(staged_case)

    @pytest.mark.unit
    def test_no_ground_truth_reaches_any_task(self, tmp_path_factory):
        """Nothing a worker can read back gives away the answer."""
        eval_dir = tmp_path_factory.mktemp("staged") / "case-000" / "eval_1"
        eval_dir.mkdir(parents=True)
        spec = build_case_spec("case-000", eval_dir, True, "proj")
        serialized = json.dumps([data for _, data, _ in spec])
        for forbidden in ("trap_name", "ground_truth", "largest_to_smallest"):
            assert forbidden not in serialized

    @pytest.mark.unit
    def test_every_task_carries_the_project_id(self, staged_case):
        """Tasks are scoped to the seeding project."""
        spec = build_case_spec("case-000", staged_case, False, "my-proj")
        for _, data, _ in spec:
            assert data["project_id"] == "my-proj"

    @pytest.mark.unit
    def test_verify_tasks_require_a_verdict(self, staged_case):
        """The report-check task's schema demands a verdict and failures."""
        spec = build_case_spec("case-000", staged_case, False, "proj")
        verify_data = next(data for role, data, _ in spec if role == "verify_report")
        required = verify_data["output_schema"]["required"]
        assert "verdict" in required and "failed_claims" in required
