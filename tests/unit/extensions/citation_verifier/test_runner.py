"""
Unit tests for the Tow case runner (#737, step 6).

The runner reads the Tow Center dataset (200 excerpts, 1,600 rows of
original 2025 engine answers) and seeds one case per excerpt: a find
task, a dependent verify task, an approve task on a deterministic
sample, and a synthesize task.  No decomposer and no planner model are
involved, which is the proof that decomposition is optional.  Ground
truth never reaches the board: it lives in a manifest file for the
grader (step 9), because a worker that can see the answer is not being
tested.

Since issue #911 the cards reach the board over MCP only: one
``create_project`` call carrying every seed spec and the chain's K2
task-type declaration (#909). These tests drive that call through a
fake, record what the runner sends, and check it is exactly what the
kernel's own seed-spec validator accepts. No board file is written.
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ``pytest.ini`` puts ``extensions/citation_verifier`` on ``pythonpath``.
from citation_verifier import runner as tow_runner

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


class TestStratifiedSelection:
    """Test suite for publisher-stratified excerpt selection.

    The dataset is ordered by publisher (10 articles each), so the
    first N excerpts cover only N/10 publishers and whole-publisher
    bot walls predetermine the pilot. Stratified selection takes
    excerpts round-robin across publishers, deterministically.
    """

    def test_round_robin_across_publishers(self, tmp_path: Path) -> None:
        """With limit 4 over 3 publishers: one each, then wrap."""
        rows = []
        for number in range(1, 7):
            row = _row(number)
            row["Publication"] = f"Publisher {((number - 1) % 3) + 1}"
            rows.append(row)
        dataset = _write_dataset(tmp_path / "tow.csv", rows)
        excerpts = tow_runner.load_excerpts(str(dataset))

        selected = tow_runner.select_excerpts(excerpts, limit=4, stratify=True)

        publishers = [e.publication for e in selected]
        assert publishers[:3] == ["Publisher 1", "Publisher 2", "Publisher 3"]
        assert publishers[3] == "Publisher 1"

    def test_unstratified_keeps_dataset_order(self, tmp_path: Path) -> None:
        """Without the flag, selection is simply the first N."""
        dataset = _write_dataset(tmp_path / "tow.csv", [_row(n) for n in (1, 2, 3)])
        excerpts = tow_runner.load_excerpts(str(dataset))

        selected = tow_runner.select_excerpts(excerpts, limit=2, stratify=False)

        assert [e.number for e in selected] == [1, 2]

    def test_stratified_is_deterministic(self, tmp_path: Path) -> None:
        """Preregistration needs the same selection every run."""
        rows = []
        for number in range(1, 9):
            row = _row(number)
            row["Publication"] = f"Publisher {((number - 1) % 4) + 1}"
            rows.append(row)
        dataset = _write_dataset(tmp_path / "tow.csv", rows)
        excerpts = tow_runner.load_excerpts(str(dataset))

        first = [e.number for e in tow_runner.select_excerpts(excerpts, 6, True)]
        second = [e.number for e in tow_runner.select_excerpts(excerpts, 6, True)]

        assert first == second


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


class _FakeMarcus:
    """A recording stand-in for the one MCP call the seeder makes."""

    def __init__(self, reply: Dict[str, Any] | None = None) -> None:
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self.reply = (
            reply
            if reply is not None
            else {
                "success": True,
                "project_id": "reg-abc123",
                "tasks_created": 0,
            }
        )

    async def __call__(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, arguments))
        return self.reply


def _excerpt(number: int = 7) -> Any:
    return tow_runner.Excerpt(
        number=number,
        excerpt=f"Synthetic excerpt number {number} for testing.",
        publication=f"Publisher {number}",
        date=f"1/{number}/2024",
        source_url=f"https://example-{number}.org/article",
        crawler="Allowed",
        paywalled="No",
        affiliation="None",
    )


class TestCaseSpec:
    """Test suite for the per-excerpt task wiring."""

    def test_sampled_case_has_four_tasks_in_a_chain(self) -> None:
        """find -> verify -> approve -> synthesize when sampled."""
        spec = tow_runner.build_case_spec(
            _excerpt(), approval_sampled=True, project_name="tow-b"
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
            _excerpt(), approval_sampled=False, project_name="tow-b"
        )

        roles = [role for role, _, _ in spec]
        deps = {role: depends_on for role, _, depends_on in spec}
        assert roles == ["find", "verify", "synthesize"]
        assert deps["synthesize"] == ["verify"]

    def test_each_card_carries_type_inputs_and_a_final_id(self) -> None:
        """Issue #911: the three fields the MCP path could not carry before."""
        spec = tow_runner.build_case_spec(
            _excerpt(), approval_sampled=True, project_name="tow-b"
        )
        data = {role: seed for role, seed, _ in spec}

        assert data["find"]["task_type"] == "find"
        assert data["find"]["id"] == "tow-b-tow-0007-find"
        assert data["find"]["inputs"]["case_id"] == "tow-0007"
        assert "query" in data["find"]["inputs"]
        assert data["verify"]["id"] == "tow-b-tow-0007-verify"
        assert data["approve"]["task_type"] == "approve"
        assert data["synthesize"]["id"] == "tow-b-tow-0007-synthesize"
        assert all("case:tow-0007" in seed["labels"] for seed in data.values())

    def test_evidence_shape_is_declared_per_project_not_per_card(self) -> None:
        """Issue #909: no card carries an output_schema; the registration does."""
        spec = tow_runner.build_case_spec(
            _excerpt(), approval_sampled=True, project_name="tow-b"
        )
        assert all("output_schema" not in seed for _, seed, _ in spec)

    def test_ids_use_the_boards_charset_whatever_the_project_is_called(self) -> None:
        """A project name with spaces or dots still yields a legal id."""
        assert tow_runner.task_id_for("Tow B (gemini) v1.2", "tow-0001", "find") == (
            "Tow-B-gemini-v1-2-tow-0001-find"
        )

    def test_ground_truth_never_reaches_the_board(self) -> None:
        """No seed spec contains the answer the workers must find.

        The publisher name, the source URL, and the article date are
        the grader's secret; a finder or verifier that can read them
        from the board is not being tested.
        """
        excerpt = _excerpt()
        spec = tow_runner.build_case_spec(
            excerpt, approval_sampled=True, project_name="tow-b"
        )

        board_payload = json.dumps([seed for _, seed, _ in spec])

        assert excerpt.source_url not in board_payload
        assert excerpt.publication not in board_payload
        assert excerpt.date not in board_payload


class TestTaskTypeRegistrations:
    """Issue #909: the chain declares its four types the way the kernel reads them."""

    def test_all_four_types_are_declared(self) -> None:
        """find, verify, approve and synthesize each have an entry."""
        assert set(tow_runner.TASK_TYPE_REGISTRATIONS) == {
            "find",
            "verify",
            "approve",
            "synthesize",
        }

    def test_verify_declares_what_it_checks(self) -> None:
        """Offer-time eligibility reads ``verifies_task_type`` to protect the finder's claim."""
        assert (
            tow_runner.TASK_TYPE_REGISTRATIONS["verify"]["verifies_task_type"] == "find"
        )

    def test_evidence_schemas_match_what_the_workers_report(self) -> None:
        """The required fields are the ones worker_common and the approvals CLI send."""
        from citation_verifier.worker_common import CLAIM_FIELDS

        from src.cli.approvals import _decision_evidence

        reg = tow_runner.TASK_TYPE_REGISTRATIONS
        assert set(reg["find"]["evidence_schema"]["required"]) == set(CLAIM_FIELDS)
        assert set(reg["verify"]["evidence_schema"]["required"]) == {
            "verdict",
            "checks",
        }
        assert set(reg["synthesize"]["evidence_schema"]["required"]) == {"answer"}
        assert set(reg["approve"]["evidence_schema"]["required"]) <= set(
            _decision_evidence("reviewer", "approved")
        )

    def test_the_declaration_passes_the_kernels_own_validator(self) -> None:
        """What the seeder sends is exactly what ``create_project`` accepts."""
        from src.core.project_registry import validate_task_type_registrations

        validate_task_type_registrations(tow_runner.TASK_TYPE_REGISTRATIONS)

    def test_no_kernel_check_injection_is_requested(self) -> None:
        """ADR-0013 K4: the kernel adds nothing to this chain's evidence."""
        assert all(
            reg["checks_injectable"] is False
            for reg in tow_runner.TASK_TYPE_REGISTRATIONS.values()
        )


class TestSeedingOverMcp:
    """Issue #911: one create_project call carries the whole board."""

    async def _seed(
        self, tmp_path: Path, fake: _FakeMarcus, *, approval_rate: float, rows: int = 3
    ) -> Dict[str, Any]:
        dataset = _write_dataset(
            tmp_path / "data.csv", [_row(n) for n in range(1, rows + 1)]
        )
        return await tow_runner.seed_cases(
            csv_path=str(dataset),
            url="http://127.0.0.1:1/mcp",
            project_name="tow-b",
            limit=rows,
            approval_rate=approval_rate,
            seed=737,
            manifest_path=str(tmp_path / "m.json"),
            project_root=str(tmp_path / "root"),
            call_tool=fake,
        )

    @pytest.mark.asyncio
    async def test_exactly_one_create_project_call_carries_every_card(
        self, tmp_path: Path
    ) -> None:
        """Three sampled cases: one call, twelve specs, the declaration, a sqlite project."""
        fake = _FakeMarcus()
        await self._seed(tmp_path, fake, approval_rate=1.0)

        assert [name for name, _ in fake.calls] == ["create_project"]
        _, args = fake.calls[0]
        options = args["options"]
        assert args["project_name"] == "tow-b"
        assert options["provider"] == "sqlite"
        assert options["project_root"] == str((tmp_path / "root").resolve())
        assert options["task_type_registrations"] == tow_runner.TASK_TYPE_REGISTRATIONS
        assert len(options["tasks"]) == 12

    @pytest.mark.asyncio
    async def test_the_specs_pass_the_kernels_seed_validator(
        self, tmp_path: Path
    ) -> None:
        """The runner sends what ``validate_seed_specs`` accepts, problem-free."""
        from src.integrations.seeded_tasks import validate_seed_specs

        fake = _FakeMarcus()
        await self._seed(tmp_path, fake, approval_rate=1.0)

        assert validate_seed_specs(fake.calls[0][1]["options"]["tasks"]) == []

    @pytest.mark.asyncio
    async def test_dependencies_are_wired_by_name_within_the_call(
        self, tmp_path: Path
    ) -> None:
        """verify depends on its case's find by name; approve on verify; synthesize on approve."""
        fake = _FakeMarcus()
        await self._seed(tmp_path, fake, approval_rate=1.0)

        by_id = {spec["id"]: spec for spec in fake.calls[0][1]["options"]["tasks"]}
        assert by_id["tow-b-tow-0001-verify"]["depends_on"] == [
            "Find the source of tow-0001"
        ]
        assert by_id["tow-b-tow-0001-approve"]["depends_on"] == [
            "Verify the claim for tow-0001"
        ]
        assert by_id["tow-b-tow-0001-synthesize"]["depends_on"] == [
            "Approve the verified claim for tow-0001"
        ]

    @pytest.mark.asyncio
    async def test_unsampled_cases_wire_synthesize_to_verify(
        self, tmp_path: Path
    ) -> None:
        """No approvals: nine specs, no approve card, synthesize follows verify."""
        fake = _FakeMarcus()
        manifest = await self._seed(tmp_path, fake, approval_rate=0.0)

        specs = fake.calls[0][1]["options"]["tasks"]
        assert len(specs) == 9
        assert not any(spec["task_type"] == "approve" for spec in specs)
        by_id = {spec["id"]: spec for spec in specs}
        assert by_id["tow-b-tow-0002-synthesize"]["depends_on"] == [
            "Verify the claim for tow-0002"
        ]
        assert "approve" not in manifest["cases"]["tow-0002"]["tasks"]

    @pytest.mark.asyncio
    async def test_manifest_records_the_registry_id_and_the_card_ids(
        self, tmp_path: Path
    ) -> None:
        """Workers and the exporter find the project and its cards from the manifest."""
        fake = _FakeMarcus({"success": True, "project_id": "reg-xyz789"})
        manifest = await self._seed(tmp_path, fake, approval_rate=1.0)

        written = json.loads((tmp_path / "m.json").read_text())
        assert written["project_id"] == "reg-xyz789"
        assert written["project_name"] == "tow-b"
        assert written["tasks_seeded"] == 12
        assert written["cases"]["tow-0001"]["tasks"] == {
            "find": "tow-b-tow-0001-find",
            "verify": "tow-b-tow-0001-verify",
            "approve": "tow-b-tow-0001-approve",
            "synthesize": "tow-b-tow-0001-synthesize",
        }
        assert manifest == written

    @pytest.mark.asyncio
    async def test_ground_truth_is_in_the_manifest_and_not_in_the_call(
        self, tmp_path: Path
    ) -> None:
        """The manifest holds the answers; the board payload never does."""
        fake = _FakeMarcus()
        manifest = await self._seed(tmp_path, fake, approval_rate=0.0)

        truth = manifest["cases"]["tow-0001"]["ground_truth"]
        assert truth == {
            "publication": "Publisher 1",
            "date": "1/1/2024",
            "source_url": "https://example-1.org/article",
        }
        payload = json.dumps(fake.calls[0][1])
        assert "https://example-1.org/article" not in payload
        assert "Publisher 1" not in payload

    @pytest.mark.asyncio
    async def test_a_refusal_is_raised_with_the_reply_body(
        self, tmp_path: Path
    ) -> None:
        """Marcus's rejection reaches the operator verbatim, and no manifest is written."""
        fake = _FakeMarcus(
            {"success": False, "error_code": "tasks_invalid", "error": "tasks[0] bad"}
        )
        with pytest.raises(RuntimeError, match="tasks_invalid"):
            await self._seed(tmp_path, fake, approval_rate=0.0)
        assert not (tmp_path / "m.json").exists()

    @pytest.mark.asyncio
    async def test_the_project_root_is_created(self, tmp_path: Path) -> None:
        """The kernel refuses a sqlite project without a root; the seeder makes one."""
        await self._seed(tmp_path, _FakeMarcus(), approval_rate=0.0)
        assert (tmp_path / "root").is_dir()
