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
from datetime import datetime, timezone
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


class TestRegisterBoard:
    """Test suite for registering seeded boards with Marcus.

    Seeding writes tasks straight into the SQLite board, bypassing
    create_project, the normal path that introduces a project to
    Marcus's ProjectRegistry. Consumers of the registry (the Cato
    dashboard's project list, single_project_mode's binding) never
    learn a bypassed board exists, so the runner registers each board
    it seeds, idempotently, under the board's real name.
    """

    @pytest.fixture
    def registry(self, tmp_path: Path) -> Any:
        """Create a file-backed registry in a temporary directory."""
        from src.core.persistence import FilePersistence, Persistence
        from src.core.project_registry import ProjectRegistry

        backend = FilePersistence(storage_dir=tmp_path / "state")
        return ProjectRegistry(persistence=Persistence(backend=backend))

    @pytest.mark.asyncio
    async def test_registers_board_under_its_real_name(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """The entry carries the board's name and absolute db path."""
        db = tmp_path / "kanban-claude.db"

        project_id = await tow_runner.register_board(
            "tow-b-claude", str(db), registry=registry
        )

        stored = await registry.get_project(project_id)
        assert stored is not None
        assert stored.name == "tow-b-claude - Main Board"
        assert stored.provider == "sqlite"
        assert stored.provider_config["project_name"] == "tow-b-claude"
        assert Path(stored.provider_config["db_path"]).is_absolute()
        assert "auto-created" in stored.tags
        assert "sqlite" in stored.tags

    @pytest.mark.asyncio
    async def test_registry_id_matches_the_kanban_project_id(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """GH-306: the kanban project id is the single source of truth,
        not an auto-generated registry UUID -- a task's own
        ``project_id`` field is written from this same string at
        creation time, so a registry lookup by that id must find this
        project. Issue #830, second half: a seeded board's K2
        registration was unreachable because this id used to be a
        random UUID instead."""
        db = tmp_path / "kanban-claude.db"

        project_id = await tow_runner.register_board(
            "tow-b-claude", str(db), registry=registry
        )

        assert project_id == "tow-b-claude"

    @pytest.mark.asyncio
    async def test_registration_is_idempotent(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """Re-seeding the same board never duplicates the entry."""
        db = str(tmp_path / "kanban-claude.db")

        first = await tow_runner.register_board("tow-b-claude", db, registry=registry)
        second = await tow_runner.register_board("tow-b-claude", db, registry=registry)

        assert first == second
        assert len(await registry.list_projects(provider="sqlite")) == 1

    @pytest.mark.asyncio
    async def test_the_same_project_id_against_a_different_db_is_refused(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """Codex P2, round 1 on PR #838: ProjectConfig.id is now the
        kanban project id (GH-306), so seeding the same project_id
        into a second database must not silently overwrite the first
        registration -- add_project() replaces both persistence and
        the cache under that id unconditionally, discarding the
        original board's path and any K2 registration it carried, and
        redirecting anything already holding this project id to the
        new board."""
        from src.core.error_framework import StateConflictError

        first_db = str(tmp_path / "kanban-first.db")
        second_db = str(tmp_path / "kanban-second.db")

        await tow_runner.register_board("tow-b-claude", first_db, registry=registry)

        with pytest.raises(StateConflictError):
            await tow_runner.register_board(
                "tow-b-claude", second_db, registry=registry
            )

        stored = await registry.get_project("tow-b-claude")
        assert stored is not None
        assert Path(stored.provider_config["db_path"]) == Path(first_db).resolve()

    @pytest.mark.asyncio
    async def test_a_legacy_uuid_record_is_migrated_not_duplicated(
        self, tmp_path: Path, registry: Any
    ) -> None:
        """Codex P2, round 2 on PR #838: a board registered by the
        pre-GH-306 code carries a generated UUID as its registry id,
        not project_id. Re-registering it must not create a second,
        empty-registrations project while the original (carrying real
        K2 registrations and the active-project slot) stays live under
        the UUID -- it must migrate in place: one record, under
        project_id, with the registrations and active status carried
        over, the UUID record gone."""
        from src.core.project_registry import ProjectConfig

        db = str(tmp_path / "kanban-claude.db")
        absolute = str(Path(db).resolve())
        now = datetime.now(timezone.utc)
        legacy_id = "ad71d867-8384-4db7-b125-d84d6d2acf8c"
        legacy = ProjectConfig(
            id=legacy_id,
            name="tow-b-claude - Main Board",
            provider="sqlite",
            provider_config={
                "db_path": absolute,
                "project_name": "tow-b-claude",
                "attachments_dir": str(Path(absolute).parent / "attachments"),
                "project_id": "tow-b-claude",
            },
            created_at=now,
            last_used=now,
            tags=["auto-created", "sqlite", "tow-experiment"],
            task_type_registrations={
                "verify": {"evidence_schema": {"required": ["verdict"]}}
            },
        )
        await registry.add_project(legacy)
        await registry.set_active_project(legacy_id)

        new_id = await tow_runner.register_board("tow-b-claude", db, registry=registry)

        assert new_id == "tow-b-claude"
        boards = await registry.list_projects(provider="sqlite")
        assert len(boards) == 1
        canonical = boards[0]
        assert canonical.id == "tow-b-claude"
        assert canonical.task_type_registrations == {
            "verify": {"evidence_schema": {"required": ["verdict"]}}
        }
        active = await registry.get_active_project()
        assert active is not None
        assert active.id == "tow-b-claude"


class TestProjectPrefixedTaskIds:
    """Test suite for Cato-visible task ids.

    Cato associates a task with a registered project when the task id
    starts with the registry id plus an underscore (the exact-match
    fallback in its matcher). Issue #741: the SQLite provider now
    honors a caller-supplied id, so the seeder gives each task its
    final, prefixed id at creation time -- no post-creation rewrite of
    tasks/task_dependencies/task_labels ever runs.
    """

    @pytest.mark.asyncio
    async def test_task_ids_carry_prefix_and_dependencies_follow(
        self, tmp_path: Path
    ) -> None:
        """Every seeded id is born with the prefix; dependencies follow."""
        import sqlite3

        dataset = _write_dataset(tmp_path / "data.csv", [_row(1), _row(2)])
        db = str(tmp_path / "kanban.db")

        manifest = await tow_runner.seed_cases(
            csv_path=str(dataset),
            db_path=db,
            project_id="tow-x",
            limit=2,
            approval_rate=0.0,
            seed=737,
            manifest_path=str(tmp_path / "m.json"),
            id_prefix="reg-123",
        )

        con = sqlite3.connect(db)
        ids = [r[0] for r in con.execute("SELECT id FROM tasks")]
        assert ids and all(i.startswith("reg-123_") for i in ids)
        deps = con.execute(
            "SELECT task_id, depends_on_id FROM task_dependencies"
        ).fetchall()
        assert deps and all(
            a.startswith("reg-123_") and b.startswith("reg-123_") for a, b in deps
        )
        known = set(ids)
        assert all(a in known and b in known for a, b in deps)
        labels = con.execute("SELECT task_id FROM task_labels").fetchall()
        assert labels and all(t.startswith("reg-123_") for (t,) in labels)
        for case in manifest["cases"].values():
            assert all(t.startswith("reg-123_") for t in case["tasks"].values())


class TestSeededTaskMetadata:
    """Test suite for task_metadata persistence at seed time.

    Cato's task universe is Marcus's ``task_metadata`` persistence
    collection; tasks created through the NLP pipeline land there,
    tasks seeded straight onto a kanban board do not, so a seeded
    board renders empty. The seeder therefore persists one honest
    metadata row per task it creates, through the sanctioned write
    (``src.core.persistence.write_task_metadata_rows``, issue #741
    step 3), keyed by the prefixed task id and carrying the registry
    project id.
    """

    @pytest.mark.asyncio
    async def test_metadata_rows_written_with_prefixed_ids(
        self, tmp_path: Path
    ) -> None:
        """Each seeded task gets a task_metadata row Cato can read."""
        import json as jsonlib
        import sqlite3

        dataset = _write_dataset(tmp_path / "data.csv", [_row(1), _row(2)])
        meta_db = tmp_path / "marcus.db"

        await tow_runner.seed_cases(
            csv_path=str(dataset),
            db_path=str(tmp_path / "kanban.db"),
            project_id="tow-x",
            limit=2,
            approval_rate=0.0,
            seed=737,
            manifest_path=str(tmp_path / "m.json"),
            id_prefix="reg-123",
            metadata_db=str(meta_db),
        )

        con = sqlite3.connect(meta_db)
        rows = [
            jsonlib.loads(r[0])
            for r in con.execute(
                "SELECT data FROM persistence WHERE collection='task_metadata'"
            )
        ]
        assert len(rows) == 6  # 2 cases x (find, verify, synthesize)
        assert all(r["task_id"].startswith("reg-123_") for r in rows)
        assert all(r["project_id"] == "reg-123" for r in rows)
        assert all(r["name"] for r in rows)
        deps = [d for r in rows for d in r.get("dependencies", [])]
        assert deps and all(d.startswith("reg-123_") for d in deps)


class TestSeededBoardEndToEnd:
    """Issue #741 step 5: a seeded board is visible end to end --

    registry entry, prefixed task ids, and task_metadata rows -- using
    only the sanctioned calls (``register_sqlite_board`` for the
    registry, the provider's own caller-supplied id for prefixed ids,
    ``write_task_metadata_rows`` for Cato's rows), with no raw SQL
    anywhere in the path.
    """

    @pytest.mark.asyncio
    async def test_registry_prefixed_ids_and_metadata_all_agree(
        self, tmp_path: Path
    ) -> None:
        from src.core.persistence import (
            FilePersistence,
            Persistence,
            SQLitePersistence,
        )
        from src.core.project_registry import ProjectRegistry, register_sqlite_board

        dataset = _write_dataset(tmp_path / "data.csv", [_row(1), _row(2)])
        db_path = str(tmp_path / "kanban.db")
        meta_db = str(tmp_path / "marcus.db")
        registry = ProjectRegistry(
            persistence=Persistence(
                backend=FilePersistence(storage_dir=tmp_path / "registry")
            )
        )

        registry_id = await register_sqlite_board("reg-456", db_path, registry=registry)

        manifest = await tow_runner.seed_cases(
            csv_path=str(dataset),
            db_path=db_path,
            project_id="tow-x",
            limit=2,
            approval_rate=0.0,
            seed=737,
            manifest_path=str(tmp_path / "m.json"),
            id_prefix=registry_id,
            metadata_db=meta_db,
        )

        # Registry entry exists and matches the board that was seeded.
        stored_project = await registry.get_project(registry_id)
        assert stored_project is not None
        assert stored_project.provider_config["db_path"] == str(Path(db_path).resolve())

        # Task ids carry the registry id as their prefix.
        all_ids = [
            t for case in manifest["cases"].values() for t in case["tasks"].values()
        ]
        assert all_ids and all(i.startswith(f"{registry_id}_") for i in all_ids)

        # Each task has a task_metadata row Cato can read, under its
        # final (already-prefixed) id.
        meta_store = Persistence(backend=SQLitePersistence(db_path=Path(meta_db)))
        for task_id in all_ids:
            row = await meta_store.retrieve("task_metadata", task_id)
            assert row is not None
            assert row["project_id"] == registry_id
