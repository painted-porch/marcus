"""
Unit tests for runtime validation (test execution).

Tests the runtime validation that runs task-scoped tests to catch
configuration issues like missing dependencies.
"""

import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.ai.validation.validation_models import SourceFile, WorkEvidence
from src.ai.validation.work_analyzer import WorkAnalyzer

pytestmark = pytest.mark.unit


class TestRuntimeValidation:
    """Test suite for runtime validation feature."""

    @pytest.fixture
    def analyzer(self) -> WorkAnalyzer:
        """Create WorkAnalyzer instance.

        WorkAnalyzer.__init__ unconditionally constructs a real
        LLMAbstraction(), which calls get_config() and raises without
        a config_marcus.json/CLAUDE_API_KEY -- a real filesystem/
        environment dependency this "unit" test must not have (issue
        #873's own follow-up: this test only passed by accident, on a
        machine or inside a run where that config happened to already
        be present). Patched the same way test_work_analyzer.py's own
        fixtures already do.
        """
        with patch("src.ai.validation.work_analyzer.LLMAbstraction"):
            return WorkAnalyzer()

    @pytest.fixture
    def mock_task(self) -> Mock:
        """Create mock task."""
        task = Mock()
        task.id = "task-123"
        task.name = "Implement calculator"
        task.completion_criteria = [
            "All functionality is implemented as per specifications",
            "All tests run successfully without errors (npm test or pytest passes)",
        ]
        return task

    @pytest.fixture
    def nodejs_evidence(self, tmp_path: Path) -> WorkEvidence:
        """Create evidence for Node.js project with tests."""
        # Create package.json with jest test script
        (tmp_path / "package.json").write_text(
            '{"name": "calculator", "scripts": {"test": "jest"}}'
        )

        # Create source file
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "calculator.js").write_text("function add(a, b) { return a + b; }")

        # Create test file
        (src_dir / "calculator.test.js").write_text(
            "test('adds', () => { expect(add(1, 2)).toBe(3); });"
        )

        return WorkEvidence(
            source_files=[
                SourceFile(
                    path=str(src_dir / "calculator.js"),
                    relative_path="src/calculator.js",
                    size_bytes=100,
                    content="function add(a, b) { return a + b; }",
                    has_placeholders=False,
                    extension=".js",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),
        )

    @pytest.mark.asyncio
    async def test_detect_nodejs_project_type(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test detection of Node.js project."""
        # Create package.json
        (tmp_path / "package.json").write_text('{"name": "test"}')

        project_type = analyzer._detect_project_type(tmp_path)

        assert project_type is not None
        assert project_type["type"] == "nodejs"
        assert project_type["test_runner"] == "npm"

    @pytest.mark.asyncio
    async def test_detect_python_project_type(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test detection of Python project."""
        # Create pyproject.toml
        (tmp_path / "pyproject.toml").write_text("[project]")

        project_type = analyzer._detect_project_type(tmp_path)

        assert project_type is not None
        assert project_type["type"] == "python"
        assert project_type["test_runner"] == "pytest"

    @pytest.mark.asyncio
    async def test_detect_no_project_type(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test no project type detected for empty directory."""
        project_type = analyzer._detect_project_type(tmp_path)

        assert project_type is None

    def test_discover_task_tests_javascript(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test discovery of JavaScript test files."""
        # Create source file
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src_file = src_dir / "calculator.js"
        src_file.write_text("function add() {}")

        # Create test file
        test_file = src_dir / "calculator.test.js"
        test_file.write_text("test('adds', () => {});")

        source_files = [
            SourceFile(
                path=str(src_file),
                relative_path="src/calculator.js",
                size_bytes=100,
                content="function add() {}",
                has_placeholders=False,
                extension=".js",
                modified_time=datetime.utcnow(),
            )
        ]

        test_files = analyzer._discover_task_tests(source_files, tmp_path)

        assert len(test_files) == 1
        assert "src/calculator.test.js" in test_files[0]

    def test_discover_task_tests_python(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test discovery of Python test files."""
        # Create source file
        src_file = tmp_path / "calculator.py"
        src_file.write_text("def add(a, b): return a + b")

        # Create test file
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_calculator.py"
        test_file.write_text("def test_add(): assert add(1, 2) == 3")

        source_files = [
            SourceFile(
                path=str(src_file),
                relative_path="calculator.py",
                size_bytes=100,
                content="def add(a, b): return a + b",
                has_placeholders=False,
                extension=".py",
                modified_time=datetime.utcnow(),
            )
        ]

        test_files = analyzer._discover_task_tests(source_files, tmp_path)

        assert len(test_files) == 1
        assert "tests/test_calculator.py" in test_files[0]

    def test_build_test_command_nodejs(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test building test command for Node.js."""
        project_type = {"type": "nodejs", "test_runner": "npm"}
        test_files = ["src/calculator.test.js"]

        command = analyzer._build_test_command(project_type, test_files, tmp_path)

        assert command == "npx jest src/calculator.test.js"

    def test_build_test_command_python(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test building test command for Python."""
        project_type = {"type": "python", "test_runner": "pytest"}
        test_files = ["tests/test_calculator.py"]

        command = analyzer._build_test_command(project_type, test_files, tmp_path)

        assert command == "pytest tests/test_calculator.py"

    def test_parse_test_failure_missing_nodejs_dependency(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """Test parsing missing Node.js dependency error."""
        error_output = """
        Error: Cannot find module 'identity-obj-proxy'
        Require stack:
        - /path/to/jest.config.js
        """
        project_type = {"type": "nodejs", "test_runner": "npm"}

        issues = analyzer._parse_test_failure(error_output, project_type)

        assert len(issues) == 1
        assert "identity-obj-proxy" in issues[0].issue
        assert issues[0].severity.value == "critical"
        assert "npm install --save-dev identity-obj-proxy" in issues[0].remediation

    def test_parse_test_failure_missing_python_dependency(
        self, analyzer: WorkAnalyzer
    ) -> None:
        """Test parsing missing Python dependency error."""
        error_output = """
        ModuleNotFoundError: No module named 'pytest-cov'
        """
        project_type = {"type": "python", "test_runner": "pytest"}

        issues = analyzer._parse_test_failure(error_output, project_type)

        assert len(issues) == 1
        assert "pytest-cov" in issues[0].issue
        assert issues[0].severity.value == "critical"
        assert "pip install pytest-cov" in issues[0].remediation

    def test_parse_test_failure_generic(self, analyzer: WorkAnalyzer) -> None:
        """Test parsing generic test failure."""
        error_output = """
        FAIL src/calculator.test.js
          ✕ adds numbers (5 ms)

        ● adds numbers

          expect(received).toBe(expected) // Object.is equality

          Expected: 3
          Received: 4
        """
        project_type = {"type": "nodejs", "test_runner": "npm"}

        issues = analyzer._parse_test_failure(error_output, project_type)

        assert len(issues) == 1
        assert "Tests failed" in issues[0].issue
        assert issues[0].severity.value == "critical"

    @pytest.mark.asyncio
    async def test_validate_runtime_no_test_runner(
        self, analyzer: WorkAnalyzer, mock_task: Mock, tmp_path: Path
    ) -> None:
        """Test runtime validation skips when no test runner detected."""
        evidence = WorkEvidence(
            source_files=[],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),  # No package.json or pyproject.toml
        )

        result = await analyzer._validate_runtime(mock_task, evidence)

        assert result.passed is True  # Skipped, not failed

    @pytest.mark.asyncio
    async def test_validate_runtime_no_tests_found(
        self, analyzer: WorkAnalyzer, mock_task: Mock, tmp_path: Path
    ) -> None:
        """Test runtime validation skips when no tests found for task."""
        # Create package.json (project type detected)
        (tmp_path / "package.json").write_text('{"name": "test"}')

        # But no test files for this task
        evidence = WorkEvidence(
            source_files=[
                SourceFile(
                    path=str(tmp_path / "src" / "calculator.js"),
                    relative_path="src/calculator.js",
                    size_bytes=100,
                    content="function add() {}",
                    has_placeholders=False,
                    extension=".js",
                    modified_time=datetime.utcnow(),
                )
            ],
            design_artifacts=[],
            decisions=[],
            project_root=str(tmp_path),
        )

        result = await analyzer._validate_runtime(mock_task, evidence)

        assert result.passed is True  # Skipped, not failed

    @pytest.mark.asyncio
    async def test_validate_runtime_tests_pass(
        self, analyzer: WorkAnalyzer, mock_task: Mock, nodejs_evidence: WorkEvidence
    ) -> None:
        """Test runtime validation passes when tests succeed."""
        # Mock subprocess to return success
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"PASS", b""))

        with patch(
            "asyncio.create_subprocess_shell", return_value=mock_proc
        ) as mock_subprocess:
            result = await analyzer._validate_runtime(mock_task, nodejs_evidence)

            # Verify test command was run
            mock_subprocess.assert_called_once()
            call_args = mock_subprocess.call_args
            assert "npx jest" in call_args[0][0]

            assert result.passed is True
            assert len(result.issues) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_validate_runtime_spawns_with_safe_stdin(
        self, analyzer: WorkAnalyzer, mock_task: Mock, nodejs_evidence: WorkEvidence
    ) -> None:
        """Runtime tests spawn with stdin=DEVNULL and a new session.

        Regression guard: runtime verification runs inside the Marcus
        server process, whose stdin (fd 0) may be closed or invalid.
        Without an explicit stdin the test subprocess inherits that bad
        fd and Python aborts at startup with "init_sys_streams: can't
        initialize sys standard streams" — failing every run for a
        reason unrelated to the deliverable.
        """
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"PASS", b""))

        with patch(
            "asyncio.create_subprocess_shell", return_value=mock_proc
        ) as mock_subprocess:
            await analyzer._validate_runtime(mock_task, nodejs_evidence)

        kwargs = mock_subprocess.call_args.kwargs
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_validate_runtime_tests_fail_missing_dependency(
        self, analyzer: WorkAnalyzer, mock_task: Mock, nodejs_evidence: WorkEvidence
    ) -> None:
        """Test runtime validation catches missing dependency."""
        # Mock subprocess to return failure with missing module error
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"",
                b"Error: Cannot find module 'identity-obj-proxy'\n"
                b"Require stack:\n- jest.config.js",
            )
        )

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
            result = await analyzer._validate_runtime(mock_task, nodejs_evidence)

            assert result.passed is False
            assert len(result.issues) == 1
            assert "identity-obj-proxy" in result.issues[0].issue
            assert (
                "npm install --save-dev identity-obj-proxy"
                in result.issues[0].remediation
            )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_validate_runtime_reads_error_from_stdout(
        self,
        analyzer: WorkAnalyzer,
        mock_task: Mock,
        nodejs_evidence: WorkEvidence,
    ) -> None:
        """Runtime validation must surface the real error from stdout.

        Test runners write the actual failure (collection errors such as
        a Python ImportError, tracebacks, and the failure summary) to
        stdout, while stderr often carries only warnings
        (PytestConfigWarning, DeprecationWarning, Node deprecations).
        Decoding stderr alone drops the real error and surfaces the
        warnings as evidence, so the stored evidence must reflect the
        error on stdout.
        """
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(
                # Real error on STDOUT, where runners actually emit it.
                b"Error: Cannot find module 'identity-obj-proxy'\n"
                b"Require stack:\n- jest.config.js",
                # STDERR carries only warnings.
                b"(node:123) Warning: deprecated API usage\n",
            )
        )

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
            result = await analyzer._validate_runtime(mock_task, nodejs_evidence)

        assert result.passed is False
        # The missing dependency reported on stdout must be detected,
        # not dropped in favour of the stderr warning.
        assert len(result.issues) == 1
        assert "identity-obj-proxy" in result.issues[0].issue
        # The surfaced reasoning must contain the real error.
        assert "identity-obj-proxy" in result.ai_reasoning

    @pytest.mark.asyncio
    async def test_validate_runtime_timeout(
        self, analyzer: WorkAnalyzer, mock_task: Mock, nodejs_evidence: WorkEvidence
    ) -> None:
        """Test runtime validation handles timeout."""
        # Mock subprocess that never completes
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
            result = await analyzer._validate_runtime(mock_task, nodejs_evidence)

            assert result.passed is False
            assert len(result.issues) == 1
            assert "timed out" in result.issues[0].issue.lower()

    def test_discover_tests_in_separate_directory(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test discovery of tests in separate tests/ directory."""
        # Create source file
        src_dir = tmp_path / "app"
        src_dir.mkdir()
        src_file = src_dir / "calculator.py"
        src_file.write_text("def add(a, b): return a + b")

        # Create test in separate tests/ directory
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_calculator.py"
        test_file.write_text("def test_add(): assert add(1, 2) == 3")

        source_files = [
            SourceFile(
                path=str(src_file),
                relative_path="app/calculator.py",
                size_bytes=100,
                content="def add(a, b): return a + b",
                has_placeholders=False,
                extension=".py",
                modified_time=datetime.utcnow(),
            )
        ]

        test_files = analyzer._discover_task_tests(source_files, tmp_path)

        assert len(test_files) == 1
        assert "tests/test_calculator.py" in test_files[0]

    def test_discover_tests_mirrored_structure(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test discovery with mirrored directory structure."""
        # Create source file in app/models/
        src_dir = tmp_path / "app" / "models"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "user.py"
        src_file.write_text("class User: pass")

        # Create test in tests/models/ (mirrored structure)
        test_dir = tmp_path / "tests" / "models"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "test_user.py"
        test_file.write_text("def test_user_creation(): pass")

        source_files = [
            SourceFile(
                path=str(src_file),
                relative_path="app/models/user.py",
                size_bytes=100,
                content="class User: pass",
                has_placeholders=False,
                extension=".py",
                modified_time=datetime.utcnow(),
            )
        ]

        test_files = analyzer._discover_task_tests(source_files, tmp_path)

        assert len(test_files) == 1
        assert "tests/models/test_user.py" in test_files[0]

    def test_discover_react_tests_in_tests_subdir(
        self, analyzer: WorkAnalyzer, tmp_path: Path
    ) -> None:
        """Test discovery of React tests in __tests__/ subdirectory."""
        # Create source component
        src_dir = tmp_path / "src" / "components"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "Calculator.tsx"
        src_file.write_text("export const Calculator = () => <div>Calc</div>")

        # Create test in __tests__/ subdirectory
        test_dir = src_dir / "__tests__"
        test_dir.mkdir()
        test_file = test_dir / "Calculator.test.tsx"
        test_file.write_text("test('renders', () => {})")

        source_files = [
            SourceFile(
                path=str(src_file),
                relative_path="src/components/Calculator.tsx",
                size_bytes=100,
                content="export const Calculator = () => <div>Calc</div>",
                has_placeholders=False,
                extension=".tsx",
                modified_time=datetime.utcnow(),
            )
        ]

        test_files = analyzer._discover_task_tests(source_files, tmp_path)

        assert len(test_files) == 1
        assert "src/components/__tests__/Calculator.test.tsx" in test_files[0]
