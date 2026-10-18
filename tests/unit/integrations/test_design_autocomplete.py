"""
Unit tests for design task auto-completion (GH-297).

Tests the two-phase approach:
- Phase A (_generate_design_content): Separate LLM calls per artifact
  document, writes files to disk, sets task status=DONE.
- Phase B (_register_design_via_mcp): Registers artifacts + decisions
  via MCP tools after state is available.

See: https://github.com/lwgray/marcus/issues/297
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.nlp_tools import (
    _generate_design_content,
    _register_design_via_mcp,
    _run_design_phase,
)


def _make_task(
    name: str,
    labels: list = None,
    status: TaskStatus = TaskStatus.TODO,
    description: str = "",
    task_id: str = None,
) -> Task:
    """Create a minimal Task for testing."""
    return Task(
        id=task_id or name.lower().replace(" ", "_"),
        name=name,
        description=description or f"Description for {name}",
        status=status,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=0.1,
        labels=labels or [],
        dependencies=[],
    )


DESIGN_DESCRIPTION = """\
Design the architecture for Authentication which encompasses:

1. USER LOGIN
   Allow users to log in with email and password

Your design should define:
- Component boundaries
- Data flows
- Integration points
- Shared data models

Create design artifacts such as:
- Architecture diagrams
- API contracts
- Data models
- Integration specifications"""


# ---- Phase A Tests ----


class TestGenerateDesignContent:
    """Tests for Phase A: separate LLM calls per artifact."""

    def _mock_llm_responses(self, mock_llm):
        """Set up LLM to return different content per call.

        Calls 1-4 are artifact documents (markdown).
        Call 5 is decisions (JSON array).
        """
        mock_llm.analyze.side_effect = [
            # Architecture doc
            "# Authentication Architecture\n\n"
            "## Components\n- AuthService\n- UserStore\n",
            # API contracts doc
            "# Auth API Contracts\n\n" "## POST /login\nRequest: {email, password}\n",
            # Data models doc
            "# Auth Data Models\n\n" "## User\n- id: uuid\n- email: string\n",
            # Interface contracts doc
            "# Auth Interface Contracts\n\n"
            "## Storage Keys\n- Auth token: `auth_token`\n",
            # Decisions (JSON array)
            json.dumps(
                [
                    {
                        "what": "Chose JWT for auth",
                        "why": "Stateless",
                        "impact": "API endpoints",
                    }
                ]
            ),
        ]

    @pytest.mark.asyncio
    @patch("src.ai.providers.llm_abstraction.LLMAbstraction")
    async def test_sets_design_task_to_done(self, mock_llm_cls, tmp_path):
        """Design task status must be DONE after Phase A."""
        mock_llm = AsyncMock()
        self._mock_llm_responses(mock_llm)
        mock_llm_cls.return_value = mock_llm

        design_task = _make_task(
            "Design Authentication",
            labels=["design", "architecture"],
            description=DESIGN_DESCRIPTION,
        )
        impl_task = _make_task(
            "Implement Auth",
            labels=["backend"],
        )

        await _generate_design_content(
            tasks=[design_task, impl_task],
            project_description="Build auth system",
            project_name="Auth App",
            project_root=str(tmp_path),
        )

        assert design_task.status == TaskStatus.DONE
        assert "auto_completed" in design_task.labels
        assert impl_task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    @patch("src.ai.providers.llm_abstraction.LLMAbstraction")
    async def test_writes_separate_artifact_files(self, mock_llm_cls, tmp_path):
        """Each artifact is a separate file on disk."""
        mock_llm = AsyncMock()
        self._mock_llm_responses(mock_llm)
        mock_llm_cls.return_value = mock_llm

        tasks = [
            _make_task(
                "Design Authentication",
                labels=["design", "architecture"],
                description=DESIGN_DESCRIPTION,
            )
        ]

        await _generate_design_content(
            tasks=tasks,
            project_description="Build auth",
            project_name="Auth",
            project_root=str(tmp_path),
        )

        arch = tmp_path / "docs" / "architecture"
        api = tmp_path / "docs" / "api"
        spec = tmp_path / "docs" / "specifications"
        assert (arch / "authentication-architecture.md").exists()
        assert (api / "authentication-api-contracts.md").exists()
        assert (spec / "authentication-data-models.md").exists()
        assert (spec / "authentication-interface-contracts.md").exists()

    @pytest.mark.asyncio
    @patch("src.ai.providers.llm_abstraction.LLMAbstraction")
    async def test_makes_separate_llm_calls(self, mock_llm_cls, tmp_path):
        """One LLM call per artifact + one for decisions = 5 total."""
        mock_llm = AsyncMock()
        self._mock_llm_responses(mock_llm)
        mock_llm_cls.return_value = mock_llm

        tasks = [
            _make_task(
                "Design Authentication",
                labels=["design", "architecture"],
                description=DESIGN_DESCRIPTION,
            )
        ]

        await _generate_design_content(
            tasks=tasks,
            project_description="Build auth",
            project_name="Auth",
            project_root=str(tmp_path),
        )

        # 4 artifacts + 1 decisions = 5 calls
        assert mock_llm.analyze.call_count == 5

    @pytest.mark.asyncio
    @patch("src.ai.providers.llm_abstraction.LLMAbstraction")
    async def test_returns_content_for_phase_b(self, mock_llm_cls, tmp_path):
        """Returns artifacts + decisions keyed by task name."""
        mock_llm = AsyncMock()
        self._mock_llm_responses(mock_llm)
        mock_llm_cls.return_value = mock_llm

        tasks = [
            _make_task(
                "Design Authentication",
                labels=["design", "architecture"],
                description=DESIGN_DESCRIPTION,
            )
        ]

        content = await _generate_design_content(
            tasks=tasks,
            project_description="Build auth",
            project_name="Auth",
            project_root=str(tmp_path),
        )

        assert "Design Authentication" in content
        assert len(content["Design Authentication"]["artifacts"]) == 4
        assert len(content["Design Authentication"]["decisions"]) == 1

    @pytest.mark.asyncio
    @patch("src.core.resilience.asyncio.sleep", new_callable=AsyncMock)
    @patch("src.ai.providers.llm_abstraction.LLMAbstraction")
    async def test_llm_failure_raises_and_leaves_task_todo(
        self, mock_llm_cls, mock_sleep, tmp_path
    ):
        """If LLM calls fail after retries, the exception propagates.

        **Behavior change in GH-304 (PR #319):** the old semantics were
        warn-and-continue — a failing LLM call would be logged, the
        affected design task would be left in TODO, and other design
        tasks would still be processed. That silently produced projects
        with partial contracts, which is worse than no contracts (it
        corrupts downstream agent work).

        The new semantics are fail-fast: any unrecoverable LLM failure
        (after ``@with_retry`` exhausts its 3 attempts) propagates out
        of ``_generate_design_content`` as an exception. Task state is
        NOT mutated on failure — ``TaskStatus.TODO``, ``assigned_to``
        unchanged, no ``auto_completed`` label. Callers must retry
        project creation.

        ``asyncio.sleep`` inside ``src.core.resilience`` is patched to
        a no-op so the retry backoff doesn't add ~6 seconds of wait
        time to this test.
        """
        mock_llm = AsyncMock()
        mock_llm.analyze.side_effect = Exception("LLM timeout")
        mock_llm_cls.return_value = mock_llm

        design_task = _make_task(
            "Design Auth",
            labels=["design", "architecture"],
            description=DESIGN_DESCRIPTION,
        )

        with pytest.raises(Exception, match="LLM timeout"):
            await _generate_design_content(
                tasks=[design_task],
                project_description="Test",
                project_name="Test",
                project_root=str(tmp_path),
            )

        # Task state must not be mutated on failure
        assert design_task.status == TaskStatus.TODO
        assert design_task.assigned_to is None
        assert "auto_completed" not in (design_task.labels or [])

    @pytest.mark.asyncio
    async def test_skips_non_design_tasks(self, tmp_path):
        """Only design tasks are processed."""
        impl_task = _make_task("Implement API", labels=["backend"])

        content = await _generate_design_content(
            tasks=[impl_task],
            project_description="Test",
            project_name="Test",
            project_root=str(tmp_path),
        )

        assert content == {}
        assert impl_task.status == TaskStatus.TODO

    @pytest.mark.asyncio
    @patch("src.ai.providers.llm_abstraction.LLMAbstraction")
    async def test_partial_artifact_failure_still_completes(
        self, mock_llm_cls, tmp_path
    ):
        """If some artifacts succeed, task is still marked DONE."""
        mock_llm = AsyncMock()
        mock_llm.analyze.side_effect = [
            # Architecture doc succeeds
            "# Auth Architecture\n\n## Components\n...",
            # API doc fails (empty)
            "",
            # Data models succeeds
            "# Auth Data Models\n\n## User\n...",
            # Interface contracts fails (empty)
            "",
            # Decisions
            json.dumps([]),
        ]
        mock_llm_cls.return_value = mock_llm

        design_task = _make_task(
            "Design Auth",
            labels=["design", "architecture"],
            description=DESIGN_DESCRIPTION,
        )

        content = await _generate_design_content(
            tasks=[design_task],
            project_description="Test",
            project_name="Test",
            project_root=str(tmp_path),
        )

        # 2 of 3 artifacts succeeded — task should be DONE
        assert design_task.status == TaskStatus.DONE
        assert len(content["Design Auth"]["artifacts"]) == 2


# ---- Phase B Tests ----


class TestRegisterDesignViaMcp:
    """Tests for Phase B: register via MCP tools."""

    @pytest.fixture
    def mock_state(self):
        """MCP state with design task that has real UUID."""
        state = MagicMock()
        state.project_tasks = [
            _make_task(
                "Design Authentication",
                labels=["design", "auto_completed"],
                status=TaskStatus.DONE,
                task_id="real_uuid_001",
            ),
            _make_task(
                "Implement Auth",
                labels=["backend"],
                task_id="real_uuid_002",
            ),
        ]
        state.kanban_client = AsyncMock()
        state.context = MagicMock()
        state.current_project_id = "proj_123"
        state.current_project_name = "Test"
        state.task_artifacts = {}
        return state

    @pytest.fixture
    def sample_content(self):
        """Phase A output for Phase B to register."""
        return {
            "Design Authentication": {
                "artifacts": [
                    {
                        "filename": "authentication-architecture.md",
                        "artifact_type": "architecture",
                        "content": "# Auth Architecture\n...",
                        "description": "Auth components",
                        "relative_path": (
                            "docs/architecture/" "authentication-architecture.md"
                        ),
                    },
                ],
                "decisions": [
                    {
                        "what": "Chose JWT",
                        "why": "Stateless",
                        "impact": "API endpoints",
                    },
                ],
            }
        }

    @pytest.mark.asyncio
    @patch("src.marcus_mcp.tools.attachment.log_artifact")
    @patch("src.marcus_mcp.tools.context.log_decision")
    async def test_registers_artifact_with_real_uuid(
        self, mock_dec, mock_art, mock_state, sample_content
    ):
        """log_artifact called with real kanban UUID."""
        mock_art.return_value = {
            "success": True,
            "data": {"location": "docs/architecture/x.md"},
        }
        mock_dec.return_value = {
            "success": True,
            "decision_id": "d1",
        }

        result = await _register_design_via_mcp(
            state=mock_state,
            design_content=sample_content,
            project_root="/var/folders/test",  # nosec B108
        )

        mock_art.assert_called_once()
        assert mock_art.call_args.kwargs["task_id"] == "real_uuid_001"
        assert mock_art.call_args.kwargs["state"] is mock_state
        assert result["artifacts_registered"] == 1

    @pytest.mark.asyncio
    @patch("src.marcus_mcp.tools.attachment.log_artifact")
    @patch("src.marcus_mcp.tools.context.log_decision")
    async def test_registers_decision_with_real_uuid(
        self, mock_dec, mock_art, mock_state, sample_content
    ):
        """log_decision called with real kanban UUID."""
        mock_art.return_value = {
            "success": True,
            "data": {"location": "docs/x"},
        }
        mock_dec.return_value = {
            "success": True,
            "decision_id": "d1",
        }

        result = await _register_design_via_mcp(
            state=mock_state,
            design_content=sample_content,
            project_root="/var/folders/test",  # nosec B108
        )

        mock_dec.assert_called_once()
        assert mock_dec.call_args.kwargs["task_id"] == "real_uuid_001"
        assert mock_dec.call_args.kwargs["agent_id"] == "Marcus"
        assert "JWT" in mock_dec.call_args.kwargs["decision"]
        assert result["decisions_logged"] == 1

    @pytest.mark.asyncio
    async def test_empty_content_returns_zeros(self, mock_state):
        """No content -> no MCP calls."""
        result = await _register_design_via_mcp(state=mock_state, design_content={})
        assert result["tasks_completed"] == 0

    @pytest.mark.asyncio
    @patch("src.marcus_mcp.tools.attachment.log_artifact")
    @patch("src.marcus_mcp.tools.context.log_decision")
    async def test_does_not_call_report_task_progress(
        self, mock_dec, mock_art, mock_state, sample_content
    ):
        """Phase B must NOT call report_task_progress."""
        mock_art.return_value = {
            "success": True,
            "data": {"location": "docs/x"},
        }
        mock_dec.return_value = {
            "success": True,
            "decision_id": "d1",
        }

        with patch("src.marcus_mcp.tools.task.report_task_progress") as mock_prog:
            await _register_design_via_mcp(
                state=mock_state,
                design_content=sample_content,
                project_root="/var/folders/test",  # nosec B108
            )
            mock_prog.assert_not_called()


# ---- Phase A → Phase B Handoff Tests (GH-320 regression) ----


class TestRunDesignPhaseHandoff:
    """
    Regression tests for GH-320: Phase A → Phase B handoff.

    Guards against the regression introduced in #314 (commit 1c5c7f7,
    April 6, 2026) where Phase A was moved to a background closure via
    ``ensure_future`` and the line ``result["design_content"] =
    design_content`` was deleted. That change orphaned the Phase B
    caller in ``src/marcus_mcp/tools/nlp.py``, which still reads
    ``result.get("design_content", {})`` — a key no longer populated.
    The result: design artifacts were generated to disk but never
    registered in ``state.task_artifacts``, so impl tasks walking
    dependencies saw no contracts even though the retrieval path was
    intact.

    The unit tests in :class:`TestRegisterDesignViaMcp` mock
    ``design_content`` directly and cannot catch a broken handoff.
    These tests exercise the full chain inside ``_run_design_phase``:
    ``_generate_design_content`` → ``_register_design_via_mcp`` →
    ``state.task_artifacts`` populated → kanban DONE update.
    """

    @pytest.fixture
    def state(self):
        """Mock MCP state with empty task_artifacts."""
        state = MagicMock()
        state.task_artifacts = {}
        state.kanban_client = AsyncMock()
        state.context = MagicMock()
        state.current_project_name = "Test Project"
        return state

    @pytest.fixture
    def mock_design_content(self):
        """Phase A output for a single design task."""
        return {
            "Design Authentication": {
                "artifacts": [
                    {
                        "filename": "auth-arch.md",
                        "artifact_type": "architecture",
                        "content": "# Auth Architecture\n",
                        "description": "Auth components",
                        "relative_path": "docs/architecture/auth-arch.md",
                    },
                ],
                "decisions": [],
            }
        }

    @pytest.fixture
    def tasks(self):
        """One design task, one impl task. Impl depends on design."""
        safe_design = _make_task(
            "Design Authentication",
            labels=["design", "architecture"],
            task_id="safe_design_1",
        )
        safe_impl = _make_task(
            "Implement Auth",
            labels=["backend"],
            task_id="safe_impl_1",
        )
        created_design = _make_task(
            "Design Authentication",
            labels=["design", "architecture"],
            task_id="real_design_uuid",
        )
        created_impl = _make_task(
            "Implement Auth",
            labels=["backend"],
            task_id="real_impl_uuid",
        )
        return {
            "safe": [safe_design, safe_impl],
            "created": [created_design, created_impl],
        }

    @pytest.mark.asyncio
    async def test_run_design_phase_populates_state_task_artifacts(
        self, state, mock_design_content, tasks
    ):
        """
        Full chain: Phase A → Phase B → state.task_artifacts populated.

        After ``_run_design_phase`` returns, ``state.task_artifacts``
        must contain an entry keyed by the real design task UUID.
        This is the assertion that would have caught #314's regression
        on day one if it had existed.
        """
        # Arrange: state.project_tasks must contain the created design
        # task so _register_design_via_mcp can match by name.
        state.project_tasks = tasks["created"]
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.marcus_mcp.tools.context.log_decision",
                new_callable=AsyncMock,
            ) as mock_log_decision,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_gen.return_value = mock_design_content

            # log_artifact must populate state.task_artifacts like the
            # real function does (attachment.py:150-165).
            async def populate_artifacts(**kwargs):
                state.task_artifacts.setdefault(kwargs["task_id"], []).append(
                    {
                        "filename": kwargs["filename"],
                        "location": "docs/architecture/auth-arch.md",
                        "artifact_type": kwargs["artifact_type"],
                    }
                )
                return {
                    "success": True,
                    "data": {"location": "docs/architecture/auth-arch.md"},
                }

            mock_log_artifact.side_effect = populate_artifacts
            mock_log_decision.return_value = {"success": True}

            # Act
            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        # Assert: state.task_artifacts populated keyed by design task UUID
        assert "real_design_uuid" in state.task_artifacts, (
            "Phase B must populate state.task_artifacts with design "
            "task UUID. This assertion guards against the #314 "
            "regression where Phase B received empty design_content."
        )
        assert len(state.task_artifacts["real_design_uuid"]) == 1
        assert state.task_artifacts["real_design_uuid"][0]["filename"] == "auth-arch.md"

    @pytest.mark.asyncio
    async def test_phase_b_registration_runs_before_kanban_done_update(
        self, state, mock_design_content, tasks
    ):
        """
        Ordering invariant: Phase B registration MUST run before the
        kanban DONE update.

        The kanban DONE update is what unblocks impl tasks from hard
        dependencies. If Phase B runs after, there is a window where:

        1. Design task marked DONE on kanban
        2. Impl task unblocked
        3. Agent requests impl task
        4. ``_collect_task_artifacts`` walks deps, finds empty
           ``state.task_artifacts[design_task_id]``
        5. Agent gets no contracts

        The window is sub-second but races don't care about narrow
        windows. This test pins the ordering.
        """
        state.project_tasks = tasks["created"]
        kanban = state.kanban_client

        # Shared call-order tracker. Each side effect appends its name.
        call_order: list[str] = []

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.marcus_mcp.tools.context.log_decision",
                new_callable=AsyncMock,
            ) as mock_log_decision,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_gen.return_value = mock_design_content

            async def track_log_artifact(**kwargs):
                call_order.append("phase_b_log_artifact")
                return {
                    "success": True,
                    "data": {"location": "docs/x.md"},
                }

            async def track_kanban_update(*args, **kwargs):
                call_order.append("kanban_done_update")
                return {"success": True}

            mock_log_artifact.side_effect = track_log_artifact
            mock_log_decision.return_value = {"success": True}
            kanban.update_task.side_effect = track_kanban_update

            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        # Assert: Phase B (log_artifact) fires BEFORE any kanban DONE
        # update. We only care about first occurrence of each.
        phase_b_idx = next(
            (
                i
                for i, event in enumerate(call_order)
                if event == "phase_b_log_artifact"
            ),
            None,
        )
        kanban_idx = next(
            (i for i, event in enumerate(call_order) if event == "kanban_done_update"),
            None,
        )

        assert phase_b_idx is not None, (
            "Phase B log_artifact was never called — registration hook "
            "is not wired into _run_design_phase"
        )
        assert kanban_idx is not None, (
            "Kanban DONE update was never called — design phase did " "not complete"
        )
        assert phase_b_idx < kanban_idx, (
            f"Ordering violation: Phase B registration must run BEFORE "
            f"kanban DONE update. Observed order: {call_order}. "
            f"If registration runs after the unblock, impl tasks can "
            f"start before contracts reach state.task_artifacts."
        )

    @pytest.mark.asyncio
    async def test_run_design_phase_handles_phase_a_failure(self, state, tasks):
        """
        Phase A failure short-circuits — no Phase B, no kanban updates.

        If ``_generate_design_content`` raises, ``state.task_artifacts``
        stays empty, no kanban updates fire, no partial state. Matches
        the atomic semantics #304 established (fail-fast, no partial
        design outputs).
        """
        state.project_tasks = tasks["created"]
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_gen.side_effect = RuntimeError("LLM timeout")

            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        # Assert: no Phase B side effects, no kanban updates
        assert (
            state.task_artifacts == {}
        ), "Phase A failure must not leave partial state.task_artifacts"
        mock_log_artifact.assert_not_called()
        kanban.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_design_phase_noop_when_no_design_tasks(self, state, tasks):
        """Empty design_content → no Phase B, no kanban updates."""
        state.project_tasks = tasks["created"]
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_gen.return_value = {}

            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        mock_log_artifact.assert_not_called()
        kanban.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_design_phase_with_state_none_skips_phase_b(
        self, mock_design_content, tasks
    ):
        """
        Legacy backward-compat path: state=None skips Phase B cleanly.

        Callers that don't pass state (e.g. add_task's add_feature path
        that doesn't have a state reference in scope) must still get
        Phase A, kanban DONE updates, and scaffold. Only Phase B
        registration is skipped, and a warning is logged.

        This pins the backward-compat guarantee made in
        NaturalLanguageProjectCreator.__init__'s state parameter
        docstring.
        """
        kanban = AsyncMock()

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ) as mock_scaffold,
        ):
            mock_gen.return_value = mock_design_content

            await _run_design_phase(
                state=None,  # legacy caller with no state reference
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        # Phase B skipped — no log_artifact calls
        mock_log_artifact.assert_not_called()

        # But kanban DONE update and scaffold still fire (Phase A
        # succeeded, so design tasks should still land as DONE on the
        # board even though registration is skipped)
        kanban.update_task.assert_called()
        mock_scaffold.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_design_phase_skips_done_when_phase_b_registered_zero(
        self, mock_design_content, tasks
    ):
        """
        Zero-registration guard (PR #326 Codex review).

        When Phase A produces design_content but Phase B registers
        zero artifacts, _run_design_phase MUST refuse to mark design
        tasks DONE. The race condition: state.project_tasks can be
        stale/empty if the background task fires before the MCP tool
        caller's refresh_project_state() completes. Without this
        guard, design tasks would unblock impl tasks even though
        state.task_artifacts stayed empty — the exact silent failure
        mode from #314 that this whole function was built to fix.
        """
        # State with EMPTY project_tasks — simulates the race where
        # background Phase B fires before state refresh completes.
        state = MagicMock()
        state.task_artifacts = {}
        state.project_tasks = []  # empty!
        state.kanban_client = AsyncMock()
        # No refresh_project_state method so we can observe the
        # zero-registration case cleanly. The guard fires because
        # Phase B cannot match any tasks by name.
        del state.refresh_project_state
        state.context = MagicMock()
        state.current_project_name = "Test Project"

        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ) as mock_scaffold,
        ):
            mock_gen.return_value = mock_design_content

            # log_artifact should NOT be called because
            # _register_design_via_mcp iterates empty state.project_tasks
            # and matches zero tasks. We let the real
            # _register_design_via_mcp run (not mocked) so the
            # zero-count path is exercised end-to-end.
            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        # Phase B registered zero (empty project_tasks → no matches)
        mock_log_artifact.assert_not_called()
        # Guard fires: kanban DONE update must be skipped
        kanban.update_task.assert_not_called()
        # Scaffold also skipped — the guard exits the function
        mock_scaffold.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_design_phase_refreshes_state_before_phase_b(
        self, state, mock_design_content, tasks
    ):
        """
        Pre-Phase-B state refresh (PR #326 Codex review).

        Before Phase B fires, _run_design_phase calls
        state.refresh_project_state() so Phase B sees current kanban
        UUIDs rather than the stale snapshot from when the background
        task was scheduled. This closes the race window between
        ensure_future() and the MCP tool caller's own refresh.
        """
        state.project_tasks = tasks["created"]
        state.refresh_project_state = AsyncMock()
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.marcus_mcp.tools.context.log_decision",
                new_callable=AsyncMock,
            ),
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_gen.return_value = mock_design_content
            mock_log_artifact.return_value = {
                "success": True,
                "data": {"location": "docs/x.md"},
            }

            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=tasks["safe"],
                created_tasks=tasks["created"],
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        state.refresh_project_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_design_phase_handles_mismatched_task_arrays(
        self, state, mock_design_content
    ):
        """
        Defensive: if safe_tasks and created_tasks get out of sync,
        the kanban update loop still terminates cleanly (zip handles
        unequal lengths by stopping at the shorter array).

        This is a defense-in-depth test — the two arrays are
        index-aligned by construction, but if a future refactor ever
        desynchronizes them we want a clean short-circuit rather than
        an IndexError or silent off-by-one.
        """
        kanban = state.kanban_client

        # created_tasks shorter than safe_tasks (pathological case)
        short_created = [
            _make_task("Design Authentication", labels=["design"], task_id="real_1"),
        ]
        long_safe = [
            _make_task("Design Authentication", labels=["design"]),
            _make_task("Design Extra", labels=["design"]),
        ]
        # Populate state.project_tasks so Phase B can match at least
        # one design task by name — otherwise the zero-registration
        # guard fires and the kanban update loop is skipped.
        state.project_tasks = short_created
        state.refresh_project_state = AsyncMock()

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ),
            patch(
                "src.marcus_mcp.tools.context.log_decision",
                new_callable=AsyncMock,
            ),
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_gen.return_value = mock_design_content

            # Must not raise IndexError or any exception
            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=long_safe,
                created_tasks=short_created,
                description="Test",
                project_name="Test",
                project_root="/var/folders/test",  # nosec B108
            )

        # Only the one created task got a kanban update (zip stopped
        # at the shorter list)
        assert kanban.update_task.call_count == 1


class TestRunDesignPhasePreGeneratedContent:
    """
    Test ``_run_design_phase`` with the contract-first
    ``pre_generated_content`` parameter (Cato retrofit, GH-320 PR
    after #333).

    When the contract-first decomposer synthesizes design ghost
    tasks, the contract artifacts and decisions have already been
    generated upstream by ``_generate_contracts_by_domain``. The
    ghost tasks need the existing Phase B / kanban DONE / scaffold
    pipeline to fire, but Phase A must be SKIPPED — re-running it
    on synthetic ghosts would produce duplicate or wrong content.

    The ``pre_generated_content`` parameter is the seam: when
    provided, ``_run_design_phase`` skips ``_generate_design_content``
    and uses the supplied dict directly as ``design_content``.
    """

    @pytest.fixture
    def state(self):
        """Mock MCP state with empty task_artifacts."""
        state = MagicMock()
        state.task_artifacts = {}
        state.kanban_client = AsyncMock()
        state.context = MagicMock()
        state.refresh_project_state = AsyncMock()
        return state

    @pytest.fixture
    def contract_first_design_content(self):
        """Pre-generated content keyed by ghost task name."""
        return {
            "Design Weather Information System": {
                "artifacts": [
                    {
                        "filename": (
                            "weather-information-system-" "interface-contracts.md"
                        ),
                        "artifact_type": "interface_contracts",
                        "content": "# Weather Contracts\n",
                        "description": "Weather interfaces",
                        "relative_path": (
                            "docs/api/weather-information-system-"
                            "interface-contracts.md"
                        ),
                    },
                ],
                "decisions": [
                    {
                        "what": "Refresh weather every 10 minutes",
                        "why": "API rate limits + UX freshness",
                        "impact": "WeatherWidget polling interval",
                    }
                ],
            }
        }

    @pytest.fixture
    def contract_first_tasks(self):
        """Synthetic design ghost + impl task."""
        ghost_safe = _make_task(
            "Design Weather Information System",
            labels=["design", "contract_first", "auto_completed"],
            status=TaskStatus.DONE,
            task_id="ghost_safe_1",
        )
        impl_safe = _make_task(
            "Implement WeatherWidget",
            labels=["contract_first", "implementation"],
            task_id="impl_safe_1",
        )
        ghost_created = _make_task(
            "Design Weather Information System",
            labels=["design", "contract_first", "auto_completed"],
            status=TaskStatus.DONE,
            task_id="real_ghost_uuid",
        )
        impl_created = _make_task(
            "Implement WeatherWidget",
            labels=["contract_first", "implementation"],
            task_id="real_impl_uuid",
        )
        return {
            "safe": [ghost_safe, impl_safe],
            "created": [ghost_created, impl_created],
        }

    @pytest.mark.asyncio
    async def test_pre_generated_content_skips_phase_a(
        self,
        state,
        contract_first_design_content,
        contract_first_tasks,
    ):
        """
        When ``pre_generated_content`` is supplied,
        ``_generate_design_content`` MUST NOT be called. Re-running
        Phase A on contract-first ghosts would either fail (no
        design tasks for Phase A to expand) or burn LLM calls
        regenerating content that already exists.
        """
        state.project_tasks = contract_first_tasks["created"]
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ) as mock_gen,
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "data": {"location": "docs/x.md"},
                },
            ),
            patch(
                "src.marcus_mcp.tools.context.log_decision",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=contract_first_tasks["safe"],
                created_tasks=contract_first_tasks["created"],
                description="Build a dashboard",
                project_name="Dashboard",
                project_root="/var/folders/test",  # nosec B108
                pre_generated_content=contract_first_design_content,
            )

        mock_gen.assert_not_called(), (
            "_generate_design_content must NOT be called when "
            "pre_generated_content is supplied — Phase A is skipped "
            "for the contract-first path."
        )

    @pytest.mark.asyncio
    async def test_pre_generated_content_drives_phase_b_registration(
        self,
        state,
        contract_first_design_content,
        contract_first_tasks,
    ):
        """
        Phase B must call ``log_artifact`` and ``log_decision``
        against the pre-generated content, joined to
        ``state.project_tasks`` by ghost task name. This is the
        whole point of the retrofit: Cato observability fires
        because the artifacts and decisions land in
        ``state.task_artifacts`` and ``state.context.decisions``
        keyed by the real kanban UUID of the ghost.
        """
        state.project_tasks = contract_first_tasks["created"]
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
            ),
            patch(
                "src.marcus_mcp.tools.attachment.log_artifact",
                new_callable=AsyncMock,
            ) as mock_log_artifact,
            patch(
                "src.marcus_mcp.tools.context.log_decision",
                new_callable=AsyncMock,
            ) as mock_log_decision,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            mock_log_artifact.return_value = {
                "success": True,
                "data": {"location": "docs/x.md"},
            }
            mock_log_decision.return_value = {"success": True}

            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=contract_first_tasks["safe"],
                created_tasks=contract_first_tasks["created"],
                description="Build a dashboard",
                project_name="Dashboard",
                project_root="/var/folders/test",  # nosec B108
                pre_generated_content=contract_first_design_content,
            )

        # log_artifact called against the ghost's real kanban UUID
        assert mock_log_artifact.called, (
            "log_artifact must be called against pre-generated "
            "contract artifacts — without this Cato never sees them."
        )
        artifact_call = mock_log_artifact.call_args
        assert artifact_call.kwargs["task_id"] == "real_ghost_uuid"
        assert (
            artifact_call.kwargs["filename"]
            == "weather-information-system-interface-contracts.md"
        )

        # log_decision called against the same ghost UUID
        assert mock_log_decision.called, (
            "log_decision must be called against pre-generated "
            "contract decisions — without this Cato never sees "
            "decision history for contract-first projects."
        )
        decision_call = mock_log_decision.call_args
        assert decision_call.kwargs["task_id"] == "real_ghost_uuid"
        assert decision_call.kwargs["agent_id"] == "Marcus"

    @pytest.mark.asyncio
    async def test_phase_a_runs_when_pre_generated_content_is_none(
        self, state, contract_first_tasks
    ):
        """
        Default behavior preserved: when ``pre_generated_content``
        is None (the feature-based path), Phase A runs as before.
        """
        state.project_tasks = contract_first_tasks["created"]
        kanban = state.kanban_client

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_gen,
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=contract_first_tasks["safe"],
                created_tasks=contract_first_tasks["created"],
                description="Build a dashboard",
                project_name="Dashboard",
                project_root="/var/folders/test",  # nosec B108
                # pre_generated_content omitted — defaults to None
            )

        mock_gen.assert_called_once()


class TestRunDesignPhaseCostAttribution:
    """Regression tests for Codex P1 on PR #516.

    ``create_project`` wraps ``_create_project_inner`` in a
    ``planner_context`` carrying a ``pending:<uuid>`` placeholder
    project_id, and runs a one-shot ``rebind_project_id`` after the
    inner returns. ``_run_design_phase`` is spawned via
    ``asyncio.ensure_future`` during the inner call — asyncio copies
    the parent's ContextVar state into the new task, so the
    placeholder is inherited. After the wrapper's rebind fires, the
    background design phase keeps writing rows under the placeholder
    that never get rebound.

    The fix: ``_run_design_phase`` pushes a fresh ``PlannerContext``
    carrying the real project_id (resolved from ``kanban_client``) as
    its very first action. This shadows the inherited placeholder
    for the design phase's whole lifetime — every LLM call inside
    records against the real project, not the orphaned placeholder.
    """

    @pytest.mark.asyncio
    async def test_pushes_real_project_context_before_running_body(self) -> None:
        """The design-phase body runs with the real project_id at the top of the stack.

        Sets up a kanban_client whose ``project_id`` is a real
        canonical string, mocks every downstream LLM/IO call so the
        body is essentially a no-op, then asserts that during the
        body the recorder's ``current()`` context reflects the real
        project_id — not whatever placeholder the caller (or no one)
        pushed before invoking.
        """
        from src.cost_tracking.cost_recorder import (
            CostRecorder,
            PlannerContext,
            set_recorder,
        )
        from src.cost_tracking.cost_store import CostStore
        from src.integrations.nlp_tools import _run_design_phase

        real_pid = "deadbeef" * 4  # 32-char dashless hex
        store = CostStore(db_path=":memory:")
        recorder = CostRecorder(store=store, enabled=True)
        set_recorder(recorder)

        # Capture the active context from inside the body so we can
        # assert it's the real project_id, not the inherited placeholder.
        captured: dict = {}

        async def _capture_ctx(**_kwargs) -> dict:
            ctx = recorder.current()
            captured["project_id"] = ctx.project_id if ctx else None
            return {}

        kanban = MagicMock()
        kanban.project_id = real_pid
        kanban.update_task = AsyncMock()

        state = MagicMock()
        state.task_artifacts = {}
        state.project_tasks = []

        # Simulate the caller-pushed placeholder context (mirrors what
        # ``create_project`` does in src/marcus_mcp/tools/nlp.py).
        placeholder = f"pending:{'x' * 32}"
        with recorder.planner_context(
            PlannerContext(
                run_id="unassigned",
                project_id=placeholder,
                project_name="Placeholder",
            )
        ):
            with (
                patch(
                    "src.integrations.nlp_tools._generate_design_content",
                    new_callable=AsyncMock,
                    side_effect=_capture_ctx,
                ),
                patch(
                    "src.integrations.nlp_tools._generate_project_scaffold",
                    new_callable=AsyncMock,
                    return_value=(True, {}),
                ),
            ):
                await _run_design_phase(
                    state=state,
                    kanban_client=kanban,
                    safe_tasks=[],
                    created_tasks=[],
                    description="x",
                    project_name="ProjX",
                    project_root="/var/folders/test",  # nosec B108
                )

        set_recorder(None)
        # The body must have seen the REAL project_id, not the placeholder.
        assert captured["project_id"] == real_pid, (
            f"Design phase saw {captured['project_id']!r}; expected "
            f"the real project_id {real_pid!r}. If this is the "
            f"placeholder {placeholder!r}, the fix has regressed."
        )

    @pytest.mark.asyncio
    async def test_falls_through_when_kanban_project_id_missing(self) -> None:
        """If ``kanban_client.project_id`` is not a string, the wrapper falls through.

        The fix uses ``isinstance(..., str)`` defensively because
        MagicMock-backed kanban_clients in tests return a MagicMock
        for any attr access. In that case we don't push a context —
        we fall through with whatever the caller already has on the
        stack — and the body still runs without crashing.
        """
        from src.integrations.nlp_tools import _run_design_phase

        kanban = MagicMock()  # project_id will be a MagicMock, not str
        kanban.update_task = AsyncMock()

        state = MagicMock()
        state.task_artifacts = {}
        state.project_tasks = []

        with (
            patch(
                "src.integrations.nlp_tools._generate_design_content",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.integrations.nlp_tools._generate_project_scaffold",
                new_callable=AsyncMock,
                return_value=(True, {}),
            ),
        ):
            # Should not raise — the isinstance guard prevents the
            # MagicMock from being used as a project_id.
            await _run_design_phase(
                state=state,
                kanban_client=kanban,
                safe_tasks=[],
                created_tasks=[],
                description="x",
                project_name="ProjY",
                project_root="/var/folders/test",  # nosec B108
            )

    @pytest.mark.asyncio
    async def test_preserves_parent_run_id_when_pushing_real_project_context(
        self,
    ) -> None:
        """Wrapper inherits ``run_id`` from the parent context, not ``'unassigned'``.

        Regression test for Codex P2 on PR #545.  The wrapper originally
        hardcoded ``run_id="unassigned"`` when shadowing the placeholder
        project_id.  But ``create_project`` (``src/marcus_mcp/tools/nlp.py:218``)
        pushes a parent context that already carries the real generated
        ``run_id``.  Overwriting it with ``"unassigned"`` attributes
        every design-phase ``token_events`` row to no run, so
        ``get_cost_summary(run_id)`` undercounts the project total.

        The fix reads ``recorder.current()`` and preserves whichever
        ``run_id`` the parent context had.  This test pushes a parent
        with a known ``run_id``, runs the design phase, and asserts the
        body sees that same ``run_id`` on the active context.

        Falsification recipe: replace the ``_inherited_run_id``
        computation in ``src/integrations/nlp_tools.py`` with the
        literal ``"unassigned"`` and confirm this test fails with
        ``AssertionError: design phase saw run_id 'unassigned'``.
        Verified empirically on 2026-05-13.
        """
        from src.cost_tracking.cost_recorder import (
            CostRecorder,
            PlannerContext,
            set_recorder,
        )
        from src.cost_tracking.cost_store import CostStore
        from src.integrations.nlp_tools import _run_design_phase

        real_pid = "feedface" * 4
        parent_run_id = "abc123def456" + "0" * 20  # arbitrary 32-char id
        store = CostStore(db_path=":memory:")
        recorder = CostRecorder(store=store, enabled=True)
        set_recorder(recorder)

        captured: dict = {}

        async def _capture_ctx(**_kwargs) -> dict:
            ctx = recorder.current()
            captured["run_id"] = ctx.run_id if ctx else None
            captured["project_id"] = ctx.project_id if ctx else None
            return {}

        kanban = MagicMock()
        kanban.project_id = real_pid
        kanban.update_task = AsyncMock()

        state = MagicMock()
        state.task_artifacts = {}
        state.project_tasks = []

        placeholder = f"pending:{'y' * 32}"
        with recorder.planner_context(
            PlannerContext(
                run_id=parent_run_id,
                project_id=placeholder,
                project_name="Placeholder",
            )
        ):
            with (
                patch(
                    "src.integrations.nlp_tools._generate_design_content",
                    new_callable=AsyncMock,
                    side_effect=_capture_ctx,
                ),
                patch(
                    "src.integrations.nlp_tools._generate_project_scaffold",
                    new_callable=AsyncMock,
                    return_value=(True, {}),
                ),
            ):
                await _run_design_phase(
                    state=state,
                    kanban_client=kanban,
                    safe_tasks=[],
                    created_tasks=[],
                    description="x",
                    project_name="ProjRunId",
                    project_root="/var/folders/test",  # nosec B108
                )

        set_recorder(None)

        # Body must see (a) the real project_id (existing invariant) AND
        # (b) the inherited run_id from the parent context.  If the
        # run_id reverted to 'unassigned', the Codex P2 fix has regressed.
        assert captured["project_id"] == real_pid, (
            f"design phase saw project_id {captured['project_id']!r}; "
            f"expected {real_pid!r}"
        )
        assert captured["run_id"] == parent_run_id, (
            f"design phase saw run_id {captured['run_id']!r}; expected "
            f"the parent's {parent_run_id!r}.  Codex P2 on PR #545 has "
            f"regressed — wrapper is dropping the inherited run_id."
        )
