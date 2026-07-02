"""Unit tests for WorkAnalyzer validation engine.

Tests the core validation engine that:
1. Discovers source files from project_root
2. Gathers design artifacts and decisions
3. Validates implementations against acceptance criteria using AI
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.ai.validation.validation_models import (
    SourceFile,
    ValidationSeverity,
    WorkEvidence,
)
from src.ai.validation.work_analyzer import WorkAnalyzer


class TestWorkAnalyzer:
    """Test suite for WorkAnalyzer."""

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """Create WorkAnalyzer instance for testing."""
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.fixture
    def mock_task(self) -> Mock:
        """Create a mock task with completion criteria.

        ``assigned_to`` is explicitly ``None`` so that
        ``WorkAnalyzer._get_project_root`` skips the worktree branch
        (added for GH-250 isolated agent worktrees). Without this,
        ``getattr(task, "assigned_to", None)`` on a vanilla ``Mock``
        returns a new ``Mock`` (Mock auto-generates attributes on
        access), which is truthy, so the worktree branch fires and
        ``main_repo.parent / "worktrees" / agent_id`` raises
        ``TypeError: unsupported operand type(s) for /: 'PosixPath'
        and 'Mock'``. Setting the attribute explicitly makes the
        mock behave like a task with no assigned agent.
        """
        task = Mock()
        task.id = "task-123"
        task.name = "Implement user registration"
        task.description = "Create user registration with email validation"
        task.type = "implementation"
        task.completion_criteria = [
            "Form includes email, password, confirm password fields",
            "Email validation implemented",
            "Password strength validation implemented",
            "Passwords match validation implemented",
        ]
        task.dependencies = []
        task.assigned_to = None
        return task

    @pytest.fixture
    def mock_state(self) -> Mock:
        """Create mock Marcus state."""
        state = Mock()
        state.task_artifacts = {}
        state.workspace_manager = Mock()
        state.workspace_manager.project_config = Mock()
        state.workspace_manager.project_config.main_workspace = "/fake/project/root"
        # Mock kanban_client._load_workspace_state() to return None
        # so code falls through to workspace_manager (which tests can update)
        state.kanban_client = Mock()
        state.kanban_client._load_workspace_state.return_value = None
        return state

    @pytest.mark.asyncio
    async def test_gather_evidence_gets_project_root_from_workspace(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test gathering evidence retrieves project_root from workspace manager."""
        # Mock os.walk to return empty (no files)
        with patch("src.ai.validation.work_analyzer.os.walk", return_value=[]):
            # Mock get_task_context
            with patch(
                "src.ai.validation.work_analyzer.get_task_context",
                new_callable=AsyncMock,
            ) as mock_context:
                mock_context.return_value = {
                    "success": True,
                    "context": {"decisions": []},
                }

                evidence = await analyzer.gather_evidence(mock_task, mock_state)

                assert evidence.project_root == "/fake/project/root"

    @pytest.mark.asyncio
    async def test_gather_evidence_discovers_source_files(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock, tmp_path: Path
    ) -> None:
        """Test source file discovery via directory scanning."""
        # Create temporary directory structure with real files
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        # Create source files
        (src_dir / "app.py").write_text("print('hello')")
        (src_dir / "utils.js").write_text("console.log('hello');")
        (src_dir / "README.md").write_text("# README")  # Should be excluded

        # Update mock_state to use tmp_path
        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }

        with patch(
            "src.ai.validation.work_analyzer.get_task_context",
            new_callable=AsyncMock,
        ) as mock_context:
            mock_context.return_value = {"success": True, "context": {"decisions": []}}

            evidence = await analyzer.gather_evidence(mock_task, mock_state)

            # Should find 2 source files (.py and .js), skip .md
            assert len(evidence.source_files) == 2
            assert any(f.extension == ".py" for f in evidence.source_files)
            assert any(f.extension == ".js" for f in evidence.source_files)
            # Verify content was read
            py_file = next(f for f in evidence.source_files if f.extension == ".py")
            assert "print('hello')" in py_file.content

    @pytest.mark.asyncio
    async def test_gather_evidence_detects_empty_files(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock, tmp_path: Path
    ) -> None:
        """Test empty file detection (0 bytes)."""
        # Create empty file
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "empty.py").write_text("")

        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }

        with patch(
            "src.ai.validation.work_analyzer.get_task_context",
            new_callable=AsyncMock,
        ) as mock_context:
            mock_context.return_value = {"success": True, "context": {"decisions": []}}

            evidence = await analyzer.gather_evidence(mock_task, mock_state)

            assert len(evidence.source_files) == 1
            assert evidence.source_files[0].is_empty()
            assert evidence.source_files[0].size_bytes == 0

    @pytest.mark.asyncio
    async def test_gather_evidence_detects_placeholders(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock, tmp_path: Path
    ) -> None:
        """Test placeholder detection (TODO, FIXME, NotImplementedError)."""
        # Create file with placeholder
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "incomplete.py").write_text(
            "def foo():\n    # TODO: implement this\n    pass"
        )

        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }

        with patch(
            "src.ai.validation.work_analyzer.get_task_context",
            new_callable=AsyncMock,
        ) as mock_context:
            mock_context.return_value = {"success": True, "context": {"decisions": []}}

            evidence = await analyzer.gather_evidence(mock_task, mock_state)

            assert len(evidence.source_files) == 1
            assert evidence.source_files[0].has_placeholders is True

    @pytest.mark.asyncio
    async def test_gather_evidence_gets_design_artifacts(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test retrieval of design artifacts from state."""
        # Add artifacts to state
        mock_state.task_artifacts = {
            "task-123": [
                {
                    "filename": "api-spec.yaml",
                    "location": "docs/api/api-spec.yaml",
                    "artifact_type": "api",
                }
            ]
        }

        with patch("src.ai.validation.work_analyzer.os.walk", return_value=[]):
            with patch(
                "src.ai.validation.work_analyzer.get_task_context",
                new_callable=AsyncMock,
            ) as mock_context:
                mock_context.return_value = {
                    "success": True,
                    "context": {"decisions": []},
                }

                evidence = await analyzer.gather_evidence(mock_task, mock_state)

                assert len(evidence.design_artifacts) == 1
                assert evidence.design_artifacts[0]["filename"] == "api-spec.yaml"

    @pytest.mark.asyncio
    async def test_gather_evidence_gets_decisions_from_context(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test retrieval of decisions via get_task_context."""
        with patch("src.ai.validation.work_analyzer.os.walk", return_value=[]):
            with patch(
                "src.ai.validation.work_analyzer.get_task_context",
                new_callable=AsyncMock,
            ) as mock_context:
                mock_context.return_value = {
                    "success": True,
                    "context": {
                        "decisions": [
                            {
                                "what": "Use bcrypt for passwords",
                                "why": "Industry standard",
                                "impact": "All password fields",
                            }
                        ]
                    },
                }

                evidence = await analyzer.gather_evidence(mock_task, mock_state)

                assert len(evidence.decisions) == 1
                assert evidence.decisions[0]["what"] == "Use bcrypt for passwords"

    @pytest.mark.asyncio
    async def test_validate_implementation_task_passes_complete_implementation(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test validation passes when all criteria are met."""
        # Mock evidence with complete source code
        mock_evidence = WorkEvidence(
            source_files=[
                SourceFile(
                    path="/fake/project/root/src/registration.js",
                    relative_path="src/registration.js",
                    size_bytes=2000,
                    content=(
                        "function validateEmail(email) { "
                        "return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email); }"
                        "\nfunction validatePassword(pwd) { "
                        "return pwd.length >= 8; }\n"
                        "function passwordsMatch(p1, p2) { "
                        "return p1 === p2; }"
                    ),
                    has_placeholders=False,
                    extension=".js",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project/root",
        )

        # Mock AI response (validation passes)
        mock_ai_response = """
VALIDATION RESULT: PASS

All acceptance criteria have been fully implemented:

1. ✅ Form includes email, password, confirm - VERIFIED in registration.js
2. ✅ Email validation implemented - validateEmail() function found
3. ✅ Password strength validation implemented - validatePassword() function found
4. ✅ Passwords match validation implemented - passwordsMatch() function found

The implementation is complete and functional.
"""

        with patch.object(analyzer, "gather_evidence", return_value=mock_evidence):
            with patch.object(
                analyzer, "_validate_with_ai", return_value=mock_ai_response
            ):
                result = await analyzer.validate_implementation_task(
                    mock_task, mock_state
                )

                assert result.passed is True
                assert len(result.issues) == 0

    @pytest.mark.asyncio
    async def test_validate_implementation_task_fails_missing_features(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test validation fails when features are missing."""
        # Mock evidence with incomplete implementation
        mock_evidence = WorkEvidence(
            source_files=[
                SourceFile(
                    path="/fake/project/root/src/registration.js",
                    relative_path="src/registration.js",
                    size_bytes=500,
                    content=(
                        "function validateEmail(email) { return true; }  "
                        "// TODO: implement proper validation"
                    ),
                    has_placeholders=True,
                    extension=".js",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project/root",
        )

        # Mock AI response with verifiable file:line citations.
        # The content at line 1 is the validateEmail TODO, which
        # proves the file has placeholder code. Both issues cite
        # that line and quote the actual content — the citation
        # verifier will confirm they match and keep the issues.
        mock_ai_response = (
            "\nVALIDATION RESULT: FAIL\n"
            "\nMissing implementations:\n"
            "\n1. ❌ Password strength validation - No validatePassword() "
            "function found\n"
            "   SEVERITY: CRITICAL\n"
            "   EVIDENCE: src/registration.js:1\n"
            "   QUOTE: `function validateEmail(email) { return true; }  "
            "// TODO: implement proper validation`\n"
            "   REMEDIATION: Add validatePassword() function "
            "(src/registration.js:1)\n"
            "   CRITERION: Password strength validation implemented\n"
            "\n2. ❌ Passwords match validation - No passwordsMatch() "
            "function found\n"
            "   SEVERITY: CRITICAL\n"
            "   EVIDENCE: src/registration.js:1\n"
            "   QUOTE: `function validateEmail(email) { return true; }`\n"
            "   REMEDIATION: Add passwordsMatch(p1, p2) function "
            "(src/registration.js:1)\n"
            "   CRITERION: Passwords match validation implemented\n"
        )

        with patch.object(analyzer, "gather_evidence", return_value=mock_evidence):
            with patch.object(
                analyzer, "_validate_with_ai", return_value=mock_ai_response
            ):
                result = await analyzer.validate_implementation_task(
                    mock_task, mock_state
                )

                assert result.passed is False
                assert len(result.issues) == 2
                assert result.issues[0].severity == ValidationSeverity.CRITICAL
                assert "password strength" in result.issues[0].issue.lower()

    @pytest.mark.asyncio
    async def test_validate_implementation_task_fails_empty_files(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """
        Empty source files fail validation via the structural
        check in _build_validation_prompt, not via LLM review.

        The empty-file case is one of the few structural failure
        modes that can be detected without a verified citation —
        a 0-byte file has nothing to cite. The test now exercises
        this path via a non-empty file with a TODO marker that can
        be quoted, since the LLM path needs citations to survive
        the post-validation check.
        """
        # Mock evidence with a file that has a quotable TODO
        mock_evidence = WorkEvidence(
            source_files=[
                SourceFile(
                    path="/fake/project/root/src/validation.js",
                    relative_path="src/validation.js",
                    size_bytes=50,
                    content="// TODO: implement validation functions",
                    has_placeholders=True,
                    extension=".js",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project/root",
        )

        # Mock AI response with a verifiable citation that quotes
        # the TODO line exactly.
        mock_ai_response = (
            "\nVALIDATION RESULT: FAIL\n"
            "\n1. ❌ No validation features implemented\n"
            "   SEVERITY: CRITICAL\n"
            "   EVIDENCE: src/validation.js:1\n"
            "   QUOTE: `// TODO: implement validation functions`\n"
            "   REMEDIATION: Implement all validation functions "
            "(src/validation.js:1)\n"
            "   CRITERION: Email validation implemented\n"
        )

        with patch.object(analyzer, "gather_evidence", return_value=mock_evidence):
            with patch.object(
                analyzer, "_validate_with_ai", return_value=mock_ai_response
            ):
                result = await analyzer.validate_implementation_task(
                    mock_task, mock_state
                )

                assert result.passed is False
                assert len(result.issues) >= 1
                assert (
                    "no validation features" in result.issues[0].issue.lower()
                    or "empty" in result.issues[0].issue.lower()
                )

    @pytest.mark.asyncio
    async def test_validate_treats_fail_with_zero_issues_as_pass(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test that LLM returning fail with no issues is treated as pass.

        When the LLM says 'fail' but provides zero specific issues,
        there's nothing actionable — so we treat it as a pass.
        """
        mock_evidence = WorkEvidence(
            source_files=[
                SourceFile(
                    path="/fake/project/root/src/app.py",
                    relative_path="src/app.py",
                    size_bytes=500,
                    content="def main(): pass",
                    has_placeholders=False,
                    extension=".py",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project/root",
        )

        # LLM returns fail with empty issues array
        mock_ai_response = '{"passed": false, "issues": []}'

        with patch.object(analyzer, "gather_evidence", return_value=mock_evidence):
            with patch.object(
                analyzer,
                "_validate_with_ai",
                return_value=mock_ai_response,
            ):
                result = await analyzer.validate_implementation_task(
                    mock_task, mock_state
                )

                assert result.passed is True
                assert len(result.issues) == 0
                assert "Auto-passed" in result.ai_reasoning

    @pytest.mark.asyncio
    async def test_validate_implementation_task_no_source_files(
        self, analyzer: WorkAnalyzer, mock_task: Mock, mock_state: Mock
    ) -> None:
        """Test validation fails when no source files discovered."""
        # Mock evidence with NO source files
        mock_evidence = WorkEvidence(
            source_files=[],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project/root",
        )

        with patch.object(analyzer, "gather_evidence", return_value=mock_evidence):
            result = await analyzer.validate_implementation_task(mock_task, mock_state)

            assert result.passed is False
            assert len(result.issues) >= 1
            assert "no source files" in result.issues[0].issue.lower()
            # Should fail immediately without calling AI


class TestGitDeltaEvidence:
    """Tests for git-delta-scoped evidence collection (issue #696).

    Validates that when a baseline_commit is available on the agent's
    assignment, gather_evidence uses git diff to collect only the files
    the agent changed rather than the entire merged worktree.
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """Create WorkAnalyzer instance."""
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.fixture
    def mock_task(self) -> Mock:
        """Create minimal mock task."""
        task = Mock()
        task.id = "task-456"
        task.name = "Implement executor"
        task.assigned_to = None
        task.completion_criteria = ["Executor handles retries"]
        task.dependencies = []
        return task

    @pytest.fixture
    def mock_state(self) -> Mock:
        """Create mock state with agent_tasks carrying a baseline_commit."""
        state = Mock()
        state.task_artifacts = {}
        state.kanban_client = Mock()
        state.kanban_client._load_workspace_state.return_value = None
        state.workspace_manager = Mock()
        state.workspace_manager.project_config = Mock()
        state.workspace_manager.project_config.main_workspace = "/fake/root"
        state.agent_tasks = {}
        return state

    @pytest.fixture
    def mock_assignment(self) -> Mock:
        """Assignment with baseline_commit set."""
        assignment = Mock()
        assignment.baseline_commit = "abc1234"
        return assignment

    @pytest.mark.asyncio
    async def test_gather_evidence_uses_git_delta_when_baseline_commit_set(
        self,
        analyzer: WorkAnalyzer,
        mock_task: Mock,
        mock_state: Mock,
        mock_assignment: Mock,
        tmp_path: Path,
    ) -> None:
        """When assignment has baseline_commit, only delta files are collected.

        Verifies that the validator does not load all 47 files from a merged
        worktree — only the 2 files the agent actually changed.
        """
        # 3 files exist on disk; only 2 are in the agent's git delta
        (tmp_path / "executor.py").write_text("class Executor: pass")
        (tmp_path / "test_executor.py").write_text("def test_ok(): pass")
        (tmp_path / "merged_by_other_agent.py").write_text("# not mine")

        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }
        mock_state.agent_tasks["agent-1"] = mock_assignment

        git_diff_output = "executor.py\ntest_executor.py\n"

        with patch("src.ai.validation.work_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0, stdout=git_diff_output, stderr=""
            )
            with patch(
                "src.ai.validation.work_analyzer.get_task_context",
                new_callable=AsyncMock,
                return_value={"success": True, "context": {"decisions": []}},
            ):
                evidence = await analyzer.gather_evidence(
                    mock_task, mock_state, agent_id="agent-1"
                )

        # Only the 2 delta files loaded — merged_by_other_agent.py excluded
        assert len(evidence.source_files) == 2
        names = {f.relative_path for f in evidence.source_files}
        assert "executor.py" in names
        assert "test_executor.py" in names
        assert "merged_by_other_agent.py" not in names

    @pytest.mark.asyncio
    async def test_gather_evidence_falls_back_to_full_scan_when_no_baseline(
        self,
        analyzer: WorkAnalyzer,
        mock_task: Mock,
        mock_state: Mock,
        tmp_path: Path,
    ) -> None:
        """When no baseline_commit exists, all files are collected (old behaviour)."""
        (tmp_path / "file_a.py").write_text("x = 1")
        (tmp_path / "file_b.py").write_text("y = 2")

        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }
        # No agent_tasks entry → no baseline_commit
        mock_state.agent_tasks = {}

        with patch(
            "src.ai.validation.work_analyzer.get_task_context",
            new_callable=AsyncMock,
            return_value={"success": True, "context": {"decisions": []}},
        ):
            evidence = await analyzer.gather_evidence(
                mock_task, mock_state, agent_id="agent-1"
            )

        assert len(evidence.source_files) == 2

    @pytest.mark.asyncio
    async def test_gather_evidence_falls_back_to_full_scan_when_git_fails(
        self,
        analyzer: WorkAnalyzer,
        mock_task: Mock,
        mock_state: Mock,
        mock_assignment: Mock,
        tmp_path: Path,
    ) -> None:
        """When git diff exits non-zero, fall back to scanning all files."""
        (tmp_path / "executor.py").write_text("class Executor: pass")
        (tmp_path / "other.py").write_text("# other")

        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }
        mock_state.agent_tasks["agent-1"] = mock_assignment

        with patch("src.ai.validation.work_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=128, stdout="", stderr="not a git repository"
            )
            with patch(
                "src.ai.validation.work_analyzer.get_task_context",
                new_callable=AsyncMock,
                return_value={"success": True, "context": {"decisions": []}},
            ):
                evidence = await analyzer.gather_evidence(
                    mock_task, mock_state, agent_id="agent-1"
                )

        # Full scan: both files returned
        assert len(evidence.source_files) == 2

    def test_discover_source_files_with_allowed_files_scopes_to_delta(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """_discover_source_files only loads files in the allowed_files list."""
        (tmp_path / "executor.py").write_text("class Executor: pass")
        (tmp_path / "merged.py").write_text("# merged from another agent")
        (tmp_path / "test_executor.py").write_text("def test_ok(): pass")

        result = analyzer._discover_source_files(
            str(tmp_path), allowed_files=["executor.py", "test_executor.py"]
        )

        assert len(result) == 2
        names = {f.relative_path for f in result}
        assert "executor.py" in names
        assert "test_executor.py" in names
        assert "merged.py" not in names

    def test_discover_source_files_with_empty_allowed_files_returns_empty(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Empty allowed_files list returns no source files."""
        (tmp_path / "executor.py").write_text("class Executor: pass")

        result = analyzer._discover_source_files(str(tmp_path), allowed_files=[])

        assert result == []

    def test_discover_source_files_with_none_allowed_files_walks_all(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """When allowed_files=None, all matching files are returned (unchanged behaviour)."""
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")

        result = analyzer._discover_source_files(str(tmp_path), allowed_files=None)

        assert len(result) == 2

    def test_discover_source_files_skips_missing_allowed_files_gracefully(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Files in allowed_files that don't exist on disk are silently skipped."""
        (tmp_path / "exists.py").write_text("x = 1")

        result = analyzer._discover_source_files(
            str(tmp_path),
            allowed_files=["exists.py", "phantom.py"],
        )

        assert len(result) == 1
        assert result[0].relative_path == "exists.py"

    # --- Codex P2 (PR #697): empty successful delta must NOT full-scan ---

    def test_get_git_delta_returns_empty_list_on_successful_empty_diff(
        self, analyzer: WorkAnalyzer, mock_state: Mock, mock_assignment: Mock
    ) -> None:
        """A successful git diff with no changed files returns ``[]``, not None.

        ``[]`` means "the agent changed nothing" → no source-file evidence →
        the correct "no source files" completion failure. Returning None would
        wrongly fall back to scanning the entire merged worktree — the exact
        #696 mis-scoping this PR removes. Regression for Codex P2 on PR #697.
        """
        mock_state.agent_tasks["agent-1"] = mock_assignment

        with patch("src.ai.validation.work_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            result = analyzer._get_git_delta_files("/fake/root", mock_state, "agent-1")

        assert result == []

    def test_get_git_delta_returns_none_when_no_baseline(
        self, analyzer: WorkAnalyzer, mock_state: Mock
    ) -> None:
        """No baseline_commit → None (full-scan fallback), distinct from ``[]``."""
        mock_state.agent_tasks = {}

        result = analyzer._get_git_delta_files("/fake/root", mock_state, "agent-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_gather_evidence_empty_delta_yields_no_source_files(
        self,
        analyzer: WorkAnalyzer,
        mock_task: Mock,
        mock_state: Mock,
        mock_assignment: Mock,
        tmp_path: Path,
    ) -> None:
        """An agent that committed nothing is not graded against merged neighbors.

        The worktree contains files merged from other agents, but the agent's
        own git delta is empty. Evidence must be zero source files (→ "no
        source files" failure), NOT a full-worktree scan. Codex P2 on PR #697.
        """
        (tmp_path / "merged_by_other_agent.py").write_text("# not mine")
        (tmp_path / "another_merged.py").write_text("# also not mine")

        mock_state.kanban_client._load_workspace_state.return_value = {
            "project_root": str(tmp_path)
        }
        mock_state.agent_tasks["agent-1"] = mock_assignment

        with patch("src.ai.validation.work_analyzer.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "src.ai.validation.work_analyzer.get_task_context",
                new_callable=AsyncMock,
                return_value={"success": True, "context": {"decisions": []}},
            ):
                evidence = await analyzer.gather_evidence(
                    mock_task, mock_state, agent_id="agent-1"
                )

        assert evidence.source_files == []

    # --- Codex P2 (PR #697): symlink-escape guard on delta paths ---

    def test_discover_source_files_skips_symlink_escaping_project_root(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """A delta path that symlinks outside the worktree is skipped, not read.

        Guards against leaking external file content into the validation
        prompt via a source-named symlink (e.g. ``leak.py`` -> an external
        secret). Regression for Codex P2 on PR #697.
        """
        external = tmp_path / "external"
        external.mkdir()
        secret = external / "secret.py"
        secret.write_text("SECRET = 'leaked'")

        project = tmp_path / "project"
        project.mkdir()
        (project / "app.py").write_text("x = 1")
        (project / "leak.py").symlink_to(secret)

        result = analyzer._discover_source_files(
            str(project), allowed_files=["app.py", "leak.py"]
        )

        names = {f.relative_path for f in result}
        assert "app.py" in names
        assert "leak.py" not in names
        # The external secret must never enter evidence content.
        assert all("leaked" not in f.content for f in result)


class TestFileManifest:
    """Tests for the file manifest injected into validation prompts (issue #696).

    The manifest lists every file that exists in the project (names only,
    no content) so the LLM knows a file exists even when it is excluded
    from SOURCE_EXTENSIONS (e.g. pyproject.toml, Makefile).
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """Create WorkAnalyzer instance."""
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.fixture
    def mock_task(self) -> Mock:
        """Minimal mock task."""
        task = Mock()
        task.id = "task-manifest"
        task.name = "Packaging Foundation"
        task.assigned_to = None
        task.completion_criteria = ["pyproject.toml configured with package metadata"]
        task.dependencies = []
        return task

    @pytest.fixture
    def mock_state(self) -> Mock:
        """Mock state with no agent assignment."""
        state = Mock()
        state.task_artifacts = {}
        state.kanban_client = Mock()
        state.kanban_client._load_workspace_state.return_value = None
        state.workspace_manager = Mock()
        state.workspace_manager.project_config = Mock()
        state.workspace_manager.project_config.main_workspace = "/fake/root"
        state.agent_tasks = {}
        return state

    def test_collect_file_manifest_includes_all_extensions(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Manifest includes files regardless of extension — .toml, Makefile, .yml etc."""
        (tmp_path / "pyproject.toml").write_text("[project]")
        (tmp_path / "setup.py").write_text("from setuptools import setup")
        (tmp_path / "Makefile").write_text("test:\n\tpytest")
        (tmp_path / ".flake8").write_text("[flake8]")
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "app.py").write_text("x = 1")

        manifest = analyzer._collect_file_manifest(str(tmp_path))

        assert "pyproject.toml" in manifest
        assert "setup.py" in manifest
        assert "Makefile" in manifest
        assert ".flake8" in manifest
        assert str(Path("src") / "app.py") in manifest

    def test_collect_file_manifest_excludes_excluded_dirs(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Manifest skips node_modules, .git, __pycache__ etc."""
        (tmp_path / "app.py").write_text("x = 1")
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "some_package.js").write_text("module.exports = {}")
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "app.cpython-312.pyc").write_bytes(b"")

        manifest = analyzer._collect_file_manifest(str(tmp_path))

        assert "app.py" in manifest
        assert not any("node_modules" in f for f in manifest)
        assert not any("__pycache__" in f for f in manifest)

    @pytest.mark.asyncio
    async def test_gather_evidence_populates_file_manifest(
        self,
        analyzer: WorkAnalyzer,
        mock_task: Mock,
        mock_state: Mock,
        tmp_path: Path,
    ) -> None:
        """gather_evidence populates WorkEvidence.file_manifest with all project files."""
        (tmp_path / "pyproject.toml").write_text("[project]")
        (tmp_path / "setup.py").write_text("from setuptools import setup; setup()")
        (tmp_path / "app.py").write_text("x = 1")

        mock_state.workspace_manager.project_config.main_workspace = str(tmp_path)

        with patch(
            "src.ai.validation.work_analyzer.get_task_context",
            new_callable=AsyncMock,
            return_value={"success": True, "context": {"decisions": []}},
        ):
            evidence = await analyzer.gather_evidence(mock_task, mock_state)

        assert hasattr(evidence, "file_manifest")
        assert "pyproject.toml" in evidence.file_manifest
        assert "setup.py" in evidence.file_manifest
        assert "app.py" in evidence.file_manifest

    def test_build_validation_prompt_includes_manifest_section(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Validation prompt contains a PROJECT FILE MANIFEST section."""
        from src.ai.validation.validation_models import WorkEvidence

        evidence = WorkEvidence(
            source_files=[],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),
            file_manifest=["pyproject.toml", "setup.py", "Makefile", "src/app.py"],
        )
        task = Mock()
        task.name = "Packaging Foundation"
        task.description = "Set up packaging"
        task.completion_criteria = ["pyproject.toml must exist"]
        task.acceptance_criteria = []

        prompt = analyzer._build_validation_prompt(task, evidence)

        assert "PROJECT FILE MANIFEST" in prompt
        assert "pyproject.toml" in prompt
        assert "Makefile" in prompt

    def test_manifest_appears_before_source_file_content(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Manifest section comes before source file content in the prompt."""
        from src.ai.validation.validation_models import WorkEvidence

        evidence = WorkEvidence(
            source_files=[],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),
            file_manifest=["pyproject.toml"],
        )
        task = Mock()
        task.name = "Test"
        task.description = "Test"
        task.completion_criteria = ["criterion"]
        task.acceptance_criteria = []

        prompt = analyzer._build_validation_prompt(task, evidence)

        manifest_pos = prompt.find("PROJECT FILE MANIFEST")
        evidence_pos = prompt.find("EVIDENCE - DISCOVERED SOURCE FILES")
        assert manifest_pos < evidence_pos


class TestParseValidationResponse:
    """Regression tests for the validation LLM response parser.

    GH-320 Experiment 4 exposed a bug where the LLM emits its
    response in a freeform markdown style (``### CRITERION N:``
    section headers, multi-line evidence prose, occasional JSON
    code fences) that the strict-keyword text parser couldn't
    extract. Every issue fell through to the ``"No evidence
    provided"`` default string and all completion attempts were
    rejected with shifting, meaningless critiques.

    Both agents in Experiment 4 hit this bug ~10 times each before
    giving up. The task's own ``historical_blockers`` field already
    documented the pattern: *"Implementation is 100% complete but
    validator rejects with 'Acceptance Criteria Not Provided'. All
    code working, all 33 tests passing."*

    These tests pin the correct parser behavior against realistic
    LLM outputs captured during Experiment 4.
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """Work analyzer with mocked LLM."""
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    def test_parses_json_response_pass(self, analyzer: WorkAnalyzer) -> None:
        """JSON-formatted PASS response parses cleanly."""
        response = """
        {
            "passed": true,
            "issues": [],
            "reasoning": "All criteria verified in source."
        }
        """
        result = analyzer._parse_validation_response(response)
        assert result.passed is True
        assert result.issues == []

    def test_parses_json_response_fail_with_full_issue_fields(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """JSON FAIL response with all issue fields populated."""
        response = """
{
    "passed": false,
    "issues": [
        {
            "severity": "CRITICAL",
            "issue": "DashboardLayout validation missing",
            "evidence": "src/dashboard-presentation/validation.ts has no DashboardLayout.validate() function",
            "remediation": "Add DashboardLayout.validate() that enforces gridColumns 1-12",
            "criterion": "Criterion 1: DashboardLayout entity"
        }
    ],
    "reasoning": "One critical issue found."
}
"""
        result = analyzer._parse_validation_response(response)
        assert result.passed is False
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert "DashboardLayout validation missing" in issue.issue
        assert "validation.ts" in issue.evidence
        assert "validate()" in issue.remediation
        assert "Criterion 1" in issue.criterion

    def test_text_response_freeform_evidence_after_emoji_regression(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """
        Regression test for GH-320 Experiment 4 validator bug.

        When the LLM emits freeform markdown with ``### CRITERION``
        headers and prose evidence in the lines following each
        ``❌`` symbol (instead of the strict ``EVIDENCE:`` keyword
        format), the parser must still extract the evidence text
        for each issue. Before this fix, the parser would fall
        through to the ``"No evidence provided"`` default for every
        issue because it only matched lines starting with the exact
        keyword ``EVIDENCE:``.
        """
        # Realistic output captured from Experiment 4 validator runs
        response = """VALIDATION RESULT: FAIL

### CRITERION 1: DashboardLayout entity defined and validated

✅ src/dashboard-presentation/types.ts contains the DashboardLayout
type with all required fields.

### CRITERION 2: Widget placement API

❌ Widget placement API incomplete
The src/dashboard-presentation/api.ts file is missing the POST
endpoint for widget registration. Only GET is implemented.
Evidence: grep for "app.post" in api.ts returns 0 matches.
Remediation: add app.post('/widgets', ...) handler following
the contract shape in docs/api/dashboard-presentation-api-contracts.md.
Criterion: CRITERION 2

### CRITERION 3: Responsive breakpoints

❌ Breakpoint resolver has hardcoded thresholds
evidence: src/dashboard-presentation/responsive.ts line 42 uses
literal values 640 and 1024 instead of reading from the
BreakpointConfig entity.
remediation: refactor resolveBreakpoint() to accept a
BreakpointConfig parameter and iterate its breakpoints array.
criterion: CRITERION 3
"""
        result = analyzer._parse_validation_response(response)

        assert result.passed is False, "Should parse as FAIL"
        assert len(result.issues) == 2, (
            f"Should extract 2 issues (CRITERION 2 and 3), got "
            f"{len(result.issues)}: "
            f"{[i.issue for i in result.issues]}"
        )

        # The bug was: every issue fell through to "No evidence provided"
        for issue in result.issues:
            assert issue.evidence != "No evidence provided", (
                f"Issue '{issue.issue}' has no evidence — parser "
                f"failed to extract freeform prose evidence."
            )
            assert issue.remediation != "No remediation provided", (
                f"Issue '{issue.issue}' has no remediation — parser "
                f"failed to extract freeform prose remediation."
            )

        # Check specific content ended up in the right fields
        issue_2 = next(
            (i for i in result.issues if "Widget placement" in i.issue), None
        )
        assert issue_2 is not None
        assert (
            "api.ts" in issue_2.evidence.lower() or "post" in issue_2.evidence.lower()
        ), f"Issue 2 evidence missing context: {issue_2.evidence}"
        assert (
            "app.post" in issue_2.remediation.lower()
            or "handler" in issue_2.remediation.lower()
        ), f"Issue 2 remediation missing context: {issue_2.remediation}"

        issue_3 = next((i for i in result.issues if "Breakpoint" in i.issue), None)
        assert issue_3 is not None
        # Evidence/remediation use lowercase keywords in this example
        assert (
            "responsive.ts" in issue_3.evidence.lower()
            or "hardcoded" in issue_3.evidence.lower()
        )

    def test_text_response_case_insensitive_keywords(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """Parser matches EVIDENCE/evidence/Evidence case-insensitively."""
        response = """VALIDATION RESULT: FAIL

❌ Issue one
Evidence: file X is missing
Remediation: create file X
CRITERION: Criterion 1
"""
        result = analyzer._parse_validation_response(response)
        assert result.passed is False
        assert len(result.issues) == 1
        assert "file X is missing" in result.issues[0].evidence
        assert "create file X" in result.issues[0].remediation

    def test_text_response_multiline_evidence_window(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """
        Parser collects evidence prose across multiple lines until
        the next section marker (another ❌, ### header, EOF, or a
        blank line followed by another keyword).
        """
        response = """VALIDATION RESULT: FAIL

❌ Missing validation layer
The validation.ts file does not export a validate() function.
Without it, callers cannot enforce field constraints at runtime.
Evidence: grep -n "export function validate" validation.ts returns 0 matches.
Remediation: add `export function validate(layout: DashboardLayout): ValidationResult`
that checks each field against the contract constraints.
Criterion: Criterion 1 — DashboardLayout validation
"""
        result = analyzer._parse_validation_response(response)
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert (
            "validate() function" in issue.issue or "Missing validation" in issue.issue
        )
        assert "validation.ts" in issue.evidence
        assert "validate" in issue.remediation
        assert "Criterion 1" in issue.criterion

    def test_text_response_subheading_inside_issue_block_preserved(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """
        Parser must NOT terminate an issue block on generic ``###``
        subheadings like ``### Evidence`` or ``### Remediation``.
        Only ``### CRITERION`` headers delimit separate issues.

        Regression test for Codex P1 on PR #331. The first fix used
        ``stripped.startswith("### ")`` as a block terminator, which
        would close the block on any markdown subheading inside the
        issue and silently drop the metadata that followed it — re-
        introducing the ``"No evidence provided"`` fallback the PR
        was supposed to fix.
        """
        response = """VALIDATION RESULT: FAIL

❌ Missing POST endpoint for widget registration

### Evidence
grep for "app.post" in src/dashboard-presentation/api.ts returns 0
matches. Only GET /widgets is implemented.

### Remediation
Add ``app.post('/widgets', registerWidget)`` wired to the handler
defined in the contract file.

Criterion: CRITERION 2
"""
        result = analyzer._parse_validation_response(response)

        assert result.passed is False
        assert len(result.issues) == 1, (
            f"Expected 1 issue, got {len(result.issues)}: "
            f"{[i.issue for i in result.issues]}"
        )
        issue = result.issues[0]
        assert issue.evidence != "No evidence provided", (
            "Parser closed block on '### Evidence' subheading and "
            "dropped the evidence text."
        )
        assert issue.remediation != "No remediation provided", (
            "Parser closed block on '### Remediation' subheading and "
            "dropped the remediation text."
        )
        assert "app.post" in issue.evidence.lower()
        assert "registerwidget" in issue.remediation.lower()

    def test_text_response_pass_with_no_issues(self, analyzer: WorkAnalyzer) -> None:
        """VALIDATION RESULT: PASS with checkmarks only → no issues."""
        response = """VALIDATION RESULT: PASS

✅ Criterion 1 - VERIFIED in src/dashboard-presentation/types.ts
✅ Criterion 2 - VERIFIED in src/dashboard-presentation/validation.ts
✅ Criterion 3 - VERIFIED in src/dashboard-presentation/api.ts
"""
        result = analyzer._parse_validation_response(response)
        assert result.passed is True
        assert len(result.issues) == 0


class TestCitationVerification:
    """
    Tests for ``_verify_citations`` — the post-validation check
    that drops issues with unverifiable or hallucinated file:line
    citations. This is the ground-truth check on the ground-truth
    checker.
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """WorkAnalyzer with LLM stubbed."""
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.fixture
    def evidence_with_file(self) -> WorkEvidence:
        """Evidence containing a single source file for citation lookup."""
        content = (
            "function validateEmail(email) {\n"
            "  return email.includes('@');\n"
            "}\n"
            "function validatePassword(pw) {\n"
            "  return pw.length >= 8;\n"
            "}\n"
        )
        return WorkEvidence(
            source_files=[
                SourceFile(
                    path="/fake/project/src/validation.js",
                    relative_path="src/validation.js",
                    size_bytes=len(content),
                    content=content,
                    has_placeholders=False,
                    extension=".js",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project",
        )

    def test_drops_issue_with_no_citation(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """Issue with no file:line citation is dropped as hallucinated."""
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Password validation missing",
                    evidence="The code doesn't check password length",
                    remediation="Add length check",
                    criterion="Password strength",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is True
        assert len(verified.issues) == 0

    def test_drops_issue_with_nonexistent_file_citation(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """Citation to a file not in evidence → dropped."""
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Missing feature",
                    evidence="src/nonexistent.js:5",
                    remediation="Create the file",
                    criterion="Feature X",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is True
        assert len(verified.issues) == 0

    def test_drops_issue_with_out_of_range_line(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """Line number beyond file length → dropped."""
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Missing feature at line 999",
                    evidence="src/validation.js:999",
                    remediation="Add it",
                    criterion="Feature X",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is True

    def test_drops_issue_with_mismatched_quote(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """
        Citation exists but the quote doesn't match the actual
        line content → dropped. This catches the "LLM knows the
        file exists but invents text" failure mode.
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        # Line 1 is "function validateEmail(email) {" but the LLM
        # claims it's something else entirely.
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Invalid function signature",
                    evidence=(
                        "src/validation.js:1 "
                        "`function loginUser(username, password) {`"
                    ),
                    remediation="Fix signature",
                    criterion="Email validation",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is True
        assert len(verified.issues) == 0

    def test_keeps_issue_with_verified_citation_and_quote(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """
        Valid citation + matching quote → issue is kept.
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        # Line 2 is "  return email.includes('@');"
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Email validation too permissive",
                    evidence=("src/validation.js:2 " "`return email.includes('@');`"),
                    remediation="Use a proper regex",
                    criterion="Email validation must reject invalid formats",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is False
        assert len(verified.issues) == 1
        assert "email validation" in verified.issues[0].issue.lower()

    def test_keeps_issue_with_quote_whitespace_tolerant(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """
        Quote with different whitespace than the actual line is
        still accepted. LLMs re-indent when quoting.
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        # Line 2 has leading whitespace; LLM drops it in the quote.
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.MAJOR,
                    issue="Email check too permissive",
                    evidence=("src/validation.js:2 " "`return email.includes('@');`"),
                    remediation="Use regex",
                    criterion="Email validation",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is False
        assert len(verified.issues) == 1

    def test_mixed_issues_drops_only_hallucinations(
        self, analyzer: WorkAnalyzer, evidence_with_file: WorkEvidence
    ) -> None:
        """
        Multiple issues: verified ones are kept, hallucinated
        ones are dropped. Passed stays False if any verified
        issues remain.
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        result = ValidationResult(
            passed=False,
            issues=[
                # Verified
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Real issue",
                    evidence=("src/validation.js:2 " "`return email.includes('@');`"),
                    remediation="Fix it",
                    criterion="Email validation",
                ),
                # Hallucinated — no citation
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Fake issue",
                    evidence="Something vague",
                    remediation="Do something",
                    criterion="Password strength",
                ),
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence_with_file)
        assert verified.passed is False
        assert len(verified.issues) == 1
        assert "real issue" in verified.issues[0].issue.lower()


class TestStructuralCitations:
    """
    Regression coverage for Codex P2 on PR #337: ``file:STRUCTURAL``
    citations must be preserved when the cited file is actually
    empty/stub, and dropped when the file contains real code.
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    def _make_evidence(self, path: str, content: str) -> WorkEvidence:
        return WorkEvidence(
            source_files=[
                SourceFile(
                    path=f"/fake/project/{path}",
                    relative_path=path,
                    size_bytes=len(content),
                    content=content,
                    has_placeholders=False,
                    extension=Path(path).suffix,
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root="/fake/project",
        )

    def test_preserves_structural_citation_on_empty_file(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """Empty required file + ``file:STRUCTURAL`` citation → kept."""
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        evidence = self._make_evidence("src/weather.ts", "")
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Required file src/weather.ts is empty (0 bytes)",
                    evidence="src/weather.ts:STRUCTURAL",
                    remediation="Implement the WeatherProvider interface",
                    criterion="Weather provider exists",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence)
        assert verified.passed is False
        assert len(verified.issues) == 1
        assert "weather.ts" in verified.issues[0].issue

    def test_preserves_structural_citation_on_stub_only_file(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """File containing only TODO/FIXME placeholders is treated as empty."""
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        stub = "// TODO: implement\n// FIXME: stub\n"
        evidence = self._make_evidence("src/stub.ts", stub)
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="src/stub.ts is stub-only",
                    evidence="src/stub.ts:STRUCTURAL",
                    remediation="Replace stub with real implementation",
                    criterion="Feature implemented",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence)
        assert verified.passed is False
        assert len(verified.issues) == 1

    def test_drops_structural_citation_on_real_file(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """
        STRUCTURAL citation against a file with real code is
        dropped — catches the LLM hallucinating "empty" against
        a non-empty file.
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        real = (
            "export function greet(name: string): string {\n"
            "  return `Hello, ${name}`;\n"
            "}\n"
        )
        evidence = self._make_evidence("src/greet.ts", real)
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="src/greet.ts is empty",
                    evidence="src/greet.ts:STRUCTURAL",
                    remediation="Implement greet",
                    criterion="Greet function exists",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence)
        # File is NOT empty, so the STRUCTURAL claim is
        # hallucinated and the issue is dropped → passed flips
        # True.
        assert verified.passed is True
        assert len(verified.issues) == 0

    def test_drops_structural_citation_on_unknown_file(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """STRUCTURAL citation to a file not in evidence is dropped."""
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        evidence = self._make_evidence("src/real.ts", "// TODO: stub\n")
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="src/nonexistent.ts is empty",
                    evidence="src/nonexistent.ts:STRUCTURAL",
                    remediation="Create it",
                    criterion="Feature X",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence)
        assert verified.passed is True
        assert len(verified.issues) == 0

    def test_is_structurally_empty_rejects_real_code(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """Real code is never classified as structurally empty."""
        assert analyzer._is_structurally_empty("") is True
        assert analyzer._is_structurally_empty("   \n  \n") is True
        assert analyzer._is_structurally_empty("// TODO\n") is True
        assert analyzer._is_structurally_empty("// TODO\n// FIXME\n") is True
        assert analyzer._is_structurally_empty("const x = 1;\n") is False
        assert analyzer._is_structurally_empty("// TODO\nconst x = 1;\n") is False

    def test_missing_import_handled_by_line_citation(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """
        Post-push review concern: structural issues like "missing
        import" must not be dropped. Verifies they're preserved
        via the existing line-citation path — the LLM cites the
        file line where the import should live, quotes whatever
        is actually at that line, and the verifier keeps the
        issue. ``file:STRUCTURAL`` is scoped to file-content
        emptiness only; line-anchored structural claims like
        missing imports flow through the normal citation path.
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        # File has real code but is missing a required import.
        # LLM cites line 1 with the actual line content as the
        # quote; the "missing import" story lives in the issue
        # text.
        evidence = self._make_evidence(
            "src/app.ts",
            "const user = getUser();\nconsole.log(user);\n",
        )
        result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Missing import for getUser at top of file",
                    evidence="src/app.ts:1 `const user = getUser();`",
                    remediation=("Add `import { getUser } from './user'` above line 1"),
                    criterion="All external calls must be imported",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        verified = analyzer._verify_citations(result, evidence)
        assert verified.passed is False
        assert len(verified.issues) == 1
        assert "missing import" in verified.issues[0].issue.lower()


class TestRuntimeExecutedFlag:
    """
    Regression coverage for Codex P1 on PR #337: the ``executed``
    flag on ValidationResult must be False when ``_validate_runtime``
    skipped (no runner detected or no test files), and True when a
    runner actually ran.
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.mark.asyncio
    async def test_executed_false_when_no_runner_detected(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """No pyproject.toml / package.json / pom.xml → executed=False."""
        # tmp_path is empty → no project type detected.
        task = Mock()
        task.id = "t1"
        evidence = WorkEvidence(
            source_files=[],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),
        )

        result = await analyzer._validate_runtime(task, evidence)
        assert result.executed is False
        assert result.passed is True  # skip returns pass-through

    @pytest.mark.asyncio
    async def test_executed_false_when_no_task_tests(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Runner detected but no matching task tests → executed=False."""
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        task = Mock()
        task.id = "t2"
        evidence = WorkEvidence(
            source_files=[
                SourceFile(
                    path=str(tmp_path / "src/foo.py"),
                    relative_path="src/foo.py",
                    size_bytes=10,
                    content="print('hi')\n",
                    has_placeholders=False,
                    extension=".py",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),
        )

        result = await analyzer._validate_runtime(task, evidence)
        assert result.executed is False
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_runtime_authority_uses_executed_flag(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """
        ``analyze_work_completion`` must treat runtime results as
        authoritative only when ``executed=True``. When runtime
        skipped (executed=False) but LLM flagged a real issue,
        the LLM verdict must stand — previously the skipped
        runtime's pass-through result incorrectly overrode the
        LLM verdict (Codex P1 on PR #337).
        """
        from src.ai.validation.validation_models import (
            ValidationIssue,
            ValidationResult,
        )

        # Skipped runtime — pass-through with executed=False
        skipped_runtime = ValidationResult(
            passed=True, issues=[], ai_reasoning="", executed=False
        )
        # LLM found a real issue with a real citation
        failing_llm = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Feature missing",
                    evidence="src/foo.py:1 `print('hi')`",
                    remediation="Add feature",
                    criterion="Feature X",
                )
            ],
            ai_reasoning="FAIL",
            validation_time=datetime.utcnow(),
        )

        # Before the fix, the caller checked test file existence
        # and wrongly promoted the skipped runtime to authority.
        # Now it reads ``runtime_result.executed`` directly, so the
        # LLM verdict wins when nothing actually ran.
        runtime_tests_ran = skipped_runtime.executed
        assert runtime_tests_ran is False
        # With runtime non-authoritative, the LLM's failing result
        # is the final verdict.
        final = skipped_runtime if runtime_tests_ran else failing_llm
        assert final.passed is False
        assert len(final.issues) == 1


class TestWorktreeResolutionOnRecovery:
    """Validator must use the reporting agent_id to find the worktree.

    Root cause: task.assigned_to is set to the RECOVERING agent at
    validation time, but the actual work (and file deletions) lives in
    the ORIGINAL agent's worktree. _get_project_root was reading
    task.assigned_to, so it looked for worktrees/<recovering_agent>/
    which doesn't exist, fell back to main implementation/, and saw
    files the original agent had already deleted.

    Fix: thread the authoritative agent_id from report_task_progress
    through validate_implementation_task and gather_evidence into
    _get_project_root, where it takes precedence over task.assigned_to.
    """

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """Create WorkAnalyzer instance for testing."""
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    def _make_state(self, project_root: str) -> Mock:
        state = Mock()
        state.task_artifacts = {}
        state.kanban_client = Mock()
        state.kanban_client._load_workspace_state.return_value = {
            "project_root": project_root
        }
        return state

    def _make_task(self, assigned_to: str) -> Mock:
        task = Mock()
        task.id = "task-99"
        task.name = "Impl"
        task.assigned_to = assigned_to
        return task

    def test_get_project_root_uses_explicit_agent_id_over_assigned_to(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """When agent_id is passed explicitly it must be used, not task.assigned_to.

        Simulates: task.assigned_to = 'agent_unicorn_4' (recovering agent)
        but the actual worktree belongs to 'agent_unicorn_3' (original agent).
        The explicit agent_id='agent_unicorn_3' must win.
        """
        impl = tmp_path / "implementation"
        impl.mkdir()
        worktrees = tmp_path / "worktrees"
        worktrees.mkdir()
        original_wt = worktrees / "agent_unicorn_3"
        original_wt.mkdir()

        task = self._make_task(assigned_to="agent_unicorn_4")
        state = self._make_state(str(impl))

        result = analyzer._get_project_root(task, state, agent_id="agent_unicorn_3")
        assert result == str(original_wt)

    def test_get_project_root_falls_back_to_assigned_to_when_no_explicit_id(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Without explicit agent_id, assigned_to is used as before (backward compat)."""
        impl = tmp_path / "implementation"
        impl.mkdir()
        worktrees = tmp_path / "worktrees"
        worktrees.mkdir()
        (worktrees / "agent_unicorn_1").mkdir()

        task = self._make_task(assigned_to="agent_unicorn_1")
        state = self._make_state(str(impl))

        result = analyzer._get_project_root(task, state)
        assert result == str(worktrees / "agent_unicorn_1")

    def test_get_project_root_returns_main_impl_when_explicit_worktree_missing(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """If explicit agent_id worktree doesn't exist, fall through to project_root."""
        impl = tmp_path / "implementation"
        impl.mkdir()

        task = self._make_task(assigned_to="agent_unicorn_4")
        state = self._make_state(str(impl))

        result = analyzer._get_project_root(task, state, agent_id="agent_unicorn_99")
        assert result == str(impl)

    @pytest.mark.asyncio
    async def test_gather_evidence_passes_agent_id_to_get_project_root(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """gather_evidence must forward agent_id to _get_project_root."""
        impl = tmp_path / "implementation"
        impl.mkdir()
        worktrees = tmp_path / "worktrees"
        worktrees.mkdir()
        wt = worktrees / "agent_unicorn_3"
        wt.mkdir()
        (wt / "app.py").write_text("x = 1")

        task = self._make_task(assigned_to="agent_unicorn_4")
        state = self._make_state(str(impl))

        with patch(
            "src.ai.validation.work_analyzer.get_task_context",
            new_callable=AsyncMock,
            return_value={"success": True, "context": {"decisions": []}},
        ):
            evidence = await analyzer.gather_evidence(
                task, state, agent_id="agent_unicorn_3"
            )

        assert evidence.project_root == str(wt)
        assert any(f.path.endswith("app.py") for f in evidence.source_files)
