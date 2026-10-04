"""
Unit tests for the Tow case runner (#737, step 6).

The runner reads the Tow Center dataset (200 excerpts, 1,600 rows of
original 2025 engine answers) and seeds one case per excerpt through
the kanban interface: a find task, a dependent verify task, an
approve task on a deterministic sample, and a synthesize task.  No
decomposer and no planner model are involved, which is the proof
that decomposition is optional.  Ground truth never reaches the
board: it lives in a manifest file for the grader (step 9), because
a worker that can see the answer is not being tested.
"""

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ``conftest.py`` puts ``dev-tools/experiments/`` on ``sys.path``.
from runners import tow_runner

pytestmark = pytest.mark.unit

COLUMNS = [
    "Tech Platform",
    "Publication",
    "Affiliation",
    "Crawler",
    "Date of Article",
    "Paywalled Article?",
    "Source URL",
    "Prompt",
    "Prompt Number",
]


def _write_dataset(path: Path, rows: List[Dict[str, str]]) -> Path:
    """Write a synthetic dataset CSV with the real column names."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(number: int, platform: str = "OpenAI") -> Dict[str, str]:
    """Build one synthetic dataset row for prompt ``number``."""
    return {
        "Tech Platform": platform,
        "Publication": f"Publisher {number}",
        "Affiliation": "None",
        "Crawler": "Allowed" if number % 2 else "Blocked",
        "Date of Article": f"1/{number}/2024",
        "Paywalled Article?": "No",
        "Source URL": f"https://example-{number}.org/article",
        "Prompt": f"Synthetic excerpt number {number} for testing.",
        "Prompt Number": str(number),
    }


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    """Three prompts, two platforms each, exercising the dedup."""
    rows = []
    for number in (2, 1, 3):
        rows.append(_row(number, "OpenAI"))
        rows.append(_row(number, "Perplexity"))
    return _write_dataset(tmp_path / "tow.csv", rows)


class TestLoadExcerpts:
    """Test suite for dataset parsing."""

    def test_dedups_platforms_to_unique_prompts(self, dataset: Path) -> None:
        """Six rows across two platforms collapse to three excerpts."""
        excerpts = tow_runner.load_excerpts(str(dataset))

        assert len(excerpts) == 3

    def test_sorted_by_prompt_number(self, dataset: Path) -> None:
        """Excerpts come back in numeric prompt order."""
        excerpts = tow_runner.load_excerpts(str(dataset))

        assert [e.number for e in excerpts] == [1, 2, 3]

    def test_fields_are_mapped(self, dataset: Path) -> None:
        """Each excerpt carries its text and its ground truth."""
        first = tow_runner.load_excerpts(str(dataset))[0]

        assert first.excerpt == "Synthetic excerpt number 1 for testing."
        assert first.publication == "Publisher 1"
        assert first.source_url == "https://example-1.org/article"
        assert first.date == "1/1/2024"
        assert first.crawler == "Allowed"


class TestApprovalSampling:
    """Test suite for the deterministic approval sample."""

    def test_same_seed_same_sample(self) -> None:
        """The sample is reproducible, which the preregistration needs."""
        numbers = list(range(1, 51))

        first = tow_runner.sample_approvals(numbers, rate=0.1, seed=737)
        second = tow_runner.sample_approvals(numbers, rate=0.1, seed=737)

        assert first == second

    def test_ten_percent_of_fifty_is_five(self) -> None:
        """The pilot's sample size matches the protocol."""
        numbers = list(range(1, 51))

        sampled = tow_runner.sample_approvals(numbers, rate=0.1, seed=737)

        assert len(sampled) == 5
        assert sampled <= set(numbers)


class TestCaseSpec:
    """Test suite for the per-excerpt task wiring."""

    def _excerpt(self) -> Any:
        return tow_runner.Excerpt(
            number=7,
            excerpt="Synthetic excerpt number 7 for testing.",
            publication="Publisher 7",
            date="1/7/2024",
            source_url="https://example-7.org/article",
            crawler="Allowed",
            paywalled="No",
            affiliation="None",
        )

    def test_sampled_case_has_four_tasks_in_a_chain(self) -> None:
        """find -> verify -> approve -> synthesize when sampled."""
        spec = tow_runner.build_case_spec(
            self._excerpt(), approval_sampled=True, project_id="tow-b"
        )

        roles = [role for role, _, _ in spec]
        deps = {role: depends_on for role, _, depends_on in spec}
        assert roles == ["find", "verify", "approve", "synthesize"]
        assert deps["verify"] == ["find"]
        assert deps["approve"] == ["verify"]
        assert deps["synthesize"] == ["approve"]

    def test_unsampled_case_skips_approve(self) -> None:
        """find -> verify -> synthesize when not in the sample."""
        spec = tow_runner.build_case_spec(
            self._excerpt(), approval_sampled=False, project_id="tow-b"
        )

        roles = [role for role, _, _ in spec]
        deps = {role: depends_on for role, _, depends_on in spec}
        assert roles == ["find", "verify", "synthesize"]
        assert deps["synthesize"] == ["verify"]

    def test_task_types_schemas_and_case_id(self) -> None:
        """Tasks carry their type, schema, and zero-padded case id."""
        spec = tow_runner.build_case_spec(
            self._excerpt(), approval_sampled=True, project_id="tow-b"
        )
        data = {role: task_data for role, task_data, _ in spec}

        assert data["find"]["task_type"] == "find"
        assert set(data["find"]["output_schema"]["required"]) == {
            "headline",
            "publisher",
            "date",
            "url",
        }
        assert data["find"]["inputs"]["case_id"] == "tow-0007"
        assert set(data["verify"]["output_schema"]["required"]) == {
            "verdict",
            "checks",
        }
        assert data["approve"]["task_type"] == "approve"
        assert data["synthesize"]["output_schema"]["required"] == ["answer"]

    def test_ground_truth_never_reaches_the_board(self) -> None:
        """No task payload contains the answer the workers must find.

        The publisher name, the source URL, and the article date are
        the grader's secret; a finder or verifier that can read them
        from the board is not being tested.
        """
        excerpt = self._excerpt()
        spec = tow_runner.build_case_spec(
            excerpt, approval_sampled=True, project_id="tow-b"
        )

        board_payload = json.dumps([task for _, task, _ in spec])

        assert excerpt.source_url not in board_payload
        assert excerpt.publication not in board_payload
        assert excerpt.date not in board_payload


class TestSeeding:
    """Test suite for seeding cases onto a SQLite board."""

    @pytest.mark.asyncio
    async def test_seeds_cases_with_wired_dependencies(
        self, dataset: Path, tmp_path: Path
    ) -> None:
        """Every case lands with real task ids wired as dependencies."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        db = str(tmp_path / "kanban.db")
        manifest_path = tmp_path / "manifest.json"

        manifest = await tow_runner.seed_cases(
            csv_path=str(dataset),
            db_path=db,
            project_id="tow-b",
            limit=3,
            approval_rate=1.0,
            seed=737,
            manifest_path=str(manifest_path),
        )

        board = SQLiteKanban({"db_path": db, "project_name": "t"})
        await board.connect()
        tasks = {t.id: t for t in await board.get_all_tasks()}
        await board.disconnect()

        assert len(manifest["cases"]) == 3
        case = manifest["cases"]["tow-0001"]
        assert case["approval_sampled"] is True
        ids = case["tasks"]
        assert tasks[ids["verify"]].dependencies == [ids["find"]]
        assert tasks[ids["approve"]].dependencies == [ids["verify"]]
        assert tasks[ids["synthesize"]].dependencies == [ids["approve"]]
        assert tasks[ids["find"]].task_type == "find"
        assert tasks[ids["find"]].project_id == "tow-b"

    @pytest.mark.asyncio
    async def test_unsampled_seeding_wires_synthesize_to_verify(
        self, dataset: Path, tmp_path: Path
    ) -> None:
        """With no approval sample, three tasks per case, chain intact."""
        from src.integrations.providers.sqlite_kanban import SQLiteKanban

        db = str(tmp_path / "kanban.db")
        manifest = await tow_runner.seed_cases(
            csv_path=str(dataset),
            db_path=db,
            project_id="tow-b",
            limit=3,
            approval_rate=0.0,
            seed=737,
            manifest_path=str(tmp_path / "m.json"),
        )

        board = SQLiteKanban({"db_path": db, "project_name": "t"})
        await board.connect()
        tasks = {t.id: t for t in await board.get_all_tasks()}
        await board.disconnect()

        assert len(tasks) == 9
        case = manifest["cases"]["tow-0002"]
        assert case["approval_sampled"] is False
        assert "approve" not in case["tasks"]
        ids = case["tasks"]
        assert tasks[ids["synthesize"]].dependencies == [ids["verify"]]

    @pytest.mark.asyncio
    async def test_manifest_keeps_ground_truth_off_the_board(
        self, dataset: Path, tmp_path: Path
    ) -> None:
        """The manifest holds the answers; the board holds the work."""
        db = str(tmp_path / "kanban.db")
        manifest_path = tmp_path / "manifest.json"

        await tow_runner.seed_cases(
            csv_path=str(dataset),
            db_path=db,
            project_id="tow-b",
            limit=2,
            approval_rate=0.0,
            seed=737,
            manifest_path=str(manifest_path),
        )

        written = json.loads(manifest_path.read_text())
        truth = written["cases"]["tow-0001"]["ground_truth"]

        assert truth["source_url"] == "https://example-1.org/article"
        assert truth["publication"] == "Publisher 1"
        assert truth["date"] == "1/1/2024"
