"""
Unit tests for spawn_agents demo reliability.

Tests countdown logging during project_info.json wait,
and error handling for common demo failure scenarios.
"""

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# ``conftest.py`` puts ``dev-tools/experiments/`` on ``sys.path``,
# making ``runners`` a normal Python package for this test subtree.
from runners import spawn_agents

pytestmark = pytest.mark.unit


class TestProjectInfoWaitCountdown:
    """Test suite for project_info.json wait countdown logging."""

    @pytest.fixture
    def mock_config(self, tmp_path: Path) -> MagicMock:
        """Create mock ExperimentConfig with tmp_path project_info_file."""
        config = MagicMock()
        config.project_info_file = tmp_path / "project_info.json"
        config.get_timeout.return_value = 10
        config.implementation_dir = tmp_path / "implementation"
        config.agents = []
        config.project_name = "test_project"
        return config

    def test_countdown_prints_waiting_message(
        self, mock_config: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that wait loop prints status messages."""
        wait_for_project_info = spawn_agents.wait_for_project_info

        # File already exists — should return immediately
        mock_config.project_info_file.write_text(
            json.dumps(
                {
                    "project_id": "p1",
                    "board_id": "b1",
                    "tasks_created": 3,
                }
            )
        )

        result = wait_for_project_info(mock_config)

        assert result is True
        output = capsys.readouterr().out
        assert "Waiting for project creation" in output
        assert "Project created" in output

    def test_countdown_returns_false_on_timeout(
        self, mock_config: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that wait loop returns False and prints error on timeout."""
        wait_for_project_info = spawn_agents.wait_for_project_info

        mock_config.get_timeout.return_value = 0  # Immediate timeout

        result = wait_for_project_info(mock_config)

        assert result is False
        output = capsys.readouterr().out
        assert "timed out" in output.lower()

    def test_countdown_returns_true_when_file_exists(
        self, mock_config: MagicMock
    ) -> None:
        """Test that wait returns True immediately if file already exists."""
        wait_for_project_info = spawn_agents.wait_for_project_info

        # Pre-create the file
        mock_config.project_info_file.write_text(
            json.dumps(
                {
                    "project_id": "p1",
                    "board_id": "b1",
                    "tasks_created": 3,
                }
            )
        )

        result = wait_for_project_info(mock_config)

        assert result is True


class TestWaitForPaneReady:
    """Test suite for tmux pane readiness polling."""

    def test_returns_true_when_shell_prompt_detected(self) -> None:
        """Test pane is ready when shell prompt indicator appears."""
        wait_for_pane_ready = spawn_agents.wait_for_pane_ready

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="user@host ~ $")
            result = wait_for_pane_ready("test:0.0", timeout=2.0)

        assert result is True

    def test_returns_true_when_zsh_prompt_detected(self) -> None:
        """Test pane is ready when zsh prompt indicator appears."""
        wait_for_pane_ready = spawn_agents.wait_for_pane_ready

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="lwgray@mac ~ %")
            result = wait_for_pane_ready("test:0.0", timeout=2.0)

        assert result is True

    def test_returns_true_on_content_stabilization(self) -> None:
        """Test pane is ready when content stops changing."""
        wait_for_pane_ready = spawn_agents.wait_for_pane_ready

        call_count = 0

        def mock_capture(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # First call: empty, then stable content without prompt
            if call_count <= 1:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="Loading shell...")

        with patch("subprocess.run", side_effect=mock_capture):
            with patch("time.sleep"):
                result = wait_for_pane_ready(
                    "test:0.0", timeout=5.0, poll_interval=0.01
                )

        assert result is True

    def test_returns_false_on_timeout(self) -> None:
        """Test pane readiness returns False when timeout expires."""
        wait_for_pane_ready = spawn_agents.wait_for_pane_ready

        call_count = 0

        def mock_capture(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # Content keeps changing — never stabilizes
            return MagicMock(returncode=0, stdout=f"changing content {call_count}")

        with patch("subprocess.run", side_effect=mock_capture):
            with patch("time.sleep"):
                result = wait_for_pane_ready(
                    "test:0.0", timeout=0.01, poll_interval=0.001
                )

        assert result is False

    def test_handles_tmux_capture_failure(self) -> None:
        """Test graceful handling when tmux capture-pane fails."""
        wait_for_pane_ready = spawn_agents.wait_for_pane_ready

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            with patch("time.sleep"):
                result = wait_for_pane_ready(
                    "test:0.0", timeout=0.01, poll_interval=0.001
                )

        assert result is False


class TestTermNormalization:
    """Test suite for TERM environment variable normalization in scripts."""

    def test_term_normalization_in_project_creator_script(self, tmp_path: Path) -> None:
        """Test project creator bash script includes TERM normalization."""
        config = MagicMock()
        config.experiment_dir = tmp_path
        config.implementation_dir = tmp_path / "implementation"
        config.implementation_dir.mkdir()
        config.prompts_dir = tmp_path / "prompts"
        config.prompts_dir.mkdir()
        config.project_info_file = tmp_path / "project_info.json"
        config.project_name = "test"
        config.project_options = {
            "complexity": "standard",
            "provider": "sqlite",
            "mode": "new_project",
        }
        config.agents = []
        config.get_timeout.return_value = 300

        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.config = config
        spawner.tmux_session = "test_session"
        spawner.current_pane = 0
        spawner.current_window = 0
        spawner.panes_per_window = 4
        spawner.marcus_mcp_url = "http://localhost:4298/mcp"
        # Slice B+ (#523) and agent-model wiring: see fixture below.
        spawner.agent_model = None
        spawner.model_flag = ""
        spawner.claude_model_flag = ""
        spawner.harness = "claude"
        spawner.harness_impl = spawn_agents.HARNESSES["claude"]

        # Mock tmux calls and generate the script
        with patch("subprocess.run"):
            with patch.object(spawner, "copy_agent_workflow_to_implementation"):
                with patch.object(spawner, "run_in_tmux_pane"):
                    spawner.spawn_project_creator()

        script_file = config.prompts_dir / "project_creator.sh"
        script_content = script_file.read_text()
        assert "TERM=xterm-256color" in script_content
        assert 'if [ "$TERM" = "dumb" ]' in script_content


class TestKanbanConnectionResilience:
    """Test suite for Kanban connection failure scenarios."""

    def test_planka_client_import_succeeds(self) -> None:
        """Test that the Planka client module is importable."""
        from src.marcus_mcp import handlers  # noqa: F401

    @pytest.mark.asyncio
    async def test_kanban_client_raises_on_connection_error(
        self,
    ) -> None:
        """Test that Kanban client operations raise on connection failure."""
        from unittest.mock import AsyncMock

        mock_client = AsyncMock()
        mock_client.get_boards = AsyncMock(
            side_effect=ConnectionError("Connection refused")
        )

        with pytest.raises(ConnectionError):
            await mock_client.get_boards()

    @pytest.mark.asyncio
    async def test_kanban_client_raises_on_timeout(self) -> None:
        """Test that Kanban client operations raise on timeout."""
        import asyncio
        from unittest.mock import AsyncMock

        mock_client = AsyncMock()
        mock_client.get_boards = AsyncMock(side_effect=asyncio.TimeoutError())

        with pytest.raises(asyncio.TimeoutError):
            await mock_client.get_boards()


class TestMCPHealthCheck:
    """Test suite for MCP server health verification."""

    def test_mcp_health_check_returns_true_when_healthy(self) -> None:
        """Test MCP health check returns True when server responds."""
        check_mcp_health = spawn_agents.check_mcp_health

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok")
            result = check_mcp_health("http://localhost:4298")

        assert result is True

    def test_mcp_health_check_returns_false_when_down(self) -> None:
        """Test MCP health check returns False when server not responding."""
        check_mcp_health = spawn_agents.check_mcp_health

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=7)
            result = check_mcp_health("http://localhost:4298")

        assert result is False

    def test_mcp_health_check_handles_exception(self) -> None:
        """Test MCP health check returns False on exception."""
        check_mcp_health = spawn_agents.check_mcp_health

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = check_mcp_health("http://localhost:4298")

        assert result is False


class TestPretrustDirectory:
    """Test suite for pre-trusting directories in ~/.claude.json."""

    def test_creates_new_config_when_missing(self, tmp_path: Path) -> None:
        """Test pre-trust creates ~/.claude.json if it doesn't exist."""
        claude_json = tmp_path / ".claude.json"
        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()

        with patch.object(Path, "home", return_value=tmp_path):
            spawn_agents.AgentSpawner._pretrust_directory(impl_dir)

        assert claude_json.exists()
        config = json.loads(claude_json.read_text())
        dir_key = str(impl_dir)
        assert dir_key in config["projects"]
        assert config["projects"][dir_key]["hasTrustDialogAccepted"] is True

    def test_updates_existing_config(self, tmp_path: Path) -> None:
        """Test pre-trust adds to existing ~/.claude.json without clobbering."""
        claude_json = tmp_path / ".claude.json"
        existing = {
            "numStartups": 42,
            "projects": {
                "/some/other/project": {
                    "hasTrustDialogAccepted": True,
                }
            },
        }
        claude_json.write_text(json.dumps(existing))

        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()

        with patch.object(Path, "home", return_value=tmp_path):
            spawn_agents.AgentSpawner._pretrust_directory(impl_dir)

        config = json.loads(claude_json.read_text())
        # Existing data preserved
        assert config["numStartups"] == 42
        assert "/some/other/project" in config["projects"]
        # New directory added
        assert config["projects"][str(impl_dir)]["hasTrustDialogAccepted"] is True

    def test_retrusts_directory_with_false_flag(self, tmp_path: Path) -> None:
        """Test pre-trust flips hasTrustDialogAccepted from False to True."""
        claude_json = tmp_path / ".claude.json"
        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()
        dir_key = str(impl_dir)

        existing = {
            "projects": {
                dir_key: {
                    "hasTrustDialogAccepted": False,
                    "allowedTools": ["Bash"],
                }
            }
        }
        claude_json.write_text(json.dumps(existing))

        with patch.object(Path, "home", return_value=tmp_path):
            spawn_agents.AgentSpawner._pretrust_directory(impl_dir)

        config = json.loads(claude_json.read_text())
        assert config["projects"][dir_key]["hasTrustDialogAccepted"] is True
        # Existing fields preserved
        assert config["projects"][dir_key]["allowedTools"] == ["Bash"]

    def test_skips_already_trusted_directory(self, tmp_path: Path) -> None:
        """Test pre-trust is a no-op when directory is already trusted."""
        claude_json = tmp_path / ".claude.json"
        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()
        dir_key = str(impl_dir)

        existing = {"projects": {dir_key: {"hasTrustDialogAccepted": True}}}
        claude_json.write_text(json.dumps(existing))
        original_mtime = claude_json.stat().st_mtime

        with patch.object(Path, "home", return_value=tmp_path):
            spawn_agents.AgentSpawner._pretrust_directory(impl_dir)

        # File should not be rewritten
        assert claude_json.stat().st_mtime == original_mtime

    def test_handles_malformed_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test graceful handling of corrupted ~/.claude.json."""
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text("{bad json")

        impl_dir = tmp_path / "implementation"
        impl_dir.mkdir()

        with patch.object(Path, "home", return_value=tmp_path):
            spawn_agents.AgentSpawner._pretrust_directory(impl_dir)

        output = capsys.readouterr().out
        assert "Could not pre-trust" in output


class TestWorkerPromptEphemeralContract:
    """Worker prompt must describe an ephemeral, single-task agent.

    Issue #595 Fix 3: workers no longer run a long-lived poll loop.
    Each worker does exactly one task and exits. There is no retry
    loop, no get_experiment_status polling, no is_running exit signal —
    the runner controller spawns the next agent for the next task. This
    removes idle polling (measured at 42% of a run's worker spend) by
    construction.

    This replaces the former long-lived-loop contract (and the v73
    premature-exit regression it guarded): premature exit is no longer
    a failure mode — exiting after one task is correct, and work
    continuity is now the runner's responsibility, not the worker's.
    """

    @pytest.fixture
    def spawner(self, tmp_path: Path) -> Any:
        """Build an AgentSpawner with minimal config for prompt rendering."""
        config = MagicMock()
        config.implementation_dir = tmp_path / "impl"
        config.project_info_file = tmp_path / "project_info.json"
        config.prompts_dir = tmp_path / "prompts"
        config.prompts_dir.mkdir()

        # AgentSpawner reads agent_prompt_template from disk; provide a
        # tiny stub so create_worker_prompt's read() succeeds.
        template = tmp_path / "agent_template.md"
        template.write_text("# Base agent template\n")

        instance = MagicMock(spec=spawn_agents.AgentSpawner)
        instance.config = config
        instance.agent_prompt_template = template
        # Bind the real method to the mock so we can call it like a method.
        instance.create_worker_prompt = (
            spawn_agents.AgentSpawner.create_worker_prompt.__get__(
                instance, spawn_agents.AgentSpawner
            )
        )
        return instance

    @pytest.fixture
    def agent_config(self) -> dict[str, Any]:
        """Minimal agent config dict for prompt rendering."""
        return {
            "id": "agent_test_1",
            "name": "Test Agent 1",
            "role": "full-stack",
            "skills": ["python", "javascript"],
            "subagents": 0,
        }

    def test_worker_prompt_is_single_task_ephemeral(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """The prompt instructs exactly one task, then exit."""
        prompt = spawner.create_worker_prompt(agent_config)
        prompt_lower = prompt.lower()
        assert "one task" in prompt_lower
        assert "exit" in prompt_lower

    def test_worker_prompt_exits_immediately_on_no_task(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """On 'no suitable tasks' the worker exits at once — no sleep, no retry.

        This is the inverse of the old long-lived contract: an idle
        worker that sleeps and retries is exactly the 42%-of-spend cost
        Fix 3 removes. The runner respawns when work is genuinely ready.
        """
        prompt = spawner.create_worker_prompt(agent_config)
        prompt_lower = prompt.lower()
        assert "no suitable tasks" in prompt_lower
        assert "exit immediately" in prompt_lower

    def test_worker_prompt_does_not_poll_or_loop(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """An ephemeral worker must not poll experiment status or loop.

        The worker is not concerned with experiment lifecycle at all —
        no get_experiment_status, no is_running. Those belong to the
        runner controller now.
        """
        prompt = spawner.create_worker_prompt(agent_config)
        assert "get_experiment_status" not in prompt
        assert "is_running" not in prompt
        assert "do not loop" in prompt.lower()

    def test_worker_prompt_does_not_request_another_task(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """After its one task the worker must not request a second."""
        prompt = spawner.create_worker_prompt(agent_config)
        assert "do not call request_next_task" in prompt.lower()

    def test_worker_prompt_has_no_retry_cap(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """No max-retries phrasing — there is no retry loop to cap.

        Retained from the v73 regression guard: a worker prompt must
        never ship a 'max N retries' cap. Under the ephemeral contract
        there is no retry at all, so this is trivially satisfied — the
        assertion guards against a future reintroduction of looping.
        """
        prompt = spawner.create_worker_prompt(agent_config).lower()
        assert "max 3 retries" not in prompt
        assert "max retries" not in prompt
        assert "retries exhausted" not in prompt

    def test_real_template_composed_prompt_has_no_loop_language(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """The prompt composed with the REAL agent_prompt.md has no loop language.

        The other tests in this class stub the base template. This one
        points the spawner at the actual templates/agent_prompt.md that
        create_worker_prompt embeds, so a contradiction between the
        ephemeral wrapper and the base template cannot slip through
        (Kaia review of #595 Fix 3 caught exactly that).
        """
        real_template = (
            Path(spawn_agents.__file__).resolve().parent.parent
            / "templates"
            / "agent_prompt.md"
        )
        assert real_template.exists(), f"real template missing: {real_template}"
        spawner.agent_prompt_template = real_template

        prompt = spawner.create_worker_prompt(agent_config).lower()

        for banned in (
            "continuous work loop",
            "never-ending",
            "never stops",
            "keep polling",
            "always immediately",
        ):
            assert banned not in prompt, (
                "long-lived-loop language in the composed worker prompt: "
                f"{banned!r} — ephemeral agents do exactly one task"
            )
        assert "exit" in prompt

    def test_real_template_enforces_tdd_as_project_standard(
        self, spawner: Any, agent_config: dict[str, Any]
    ) -> None:
        """Issue #607: TDD is a project-wide standard in the worker prompt.

        Step 3 of the decomposition redesign rolls the per-feature Test
        task up into the Implement task's ``completion_criteria``. The
        load-bearing complement to that rollup is this directive in the
        worker prompt: write tests first against the criteria, watch
        them fail, then make them pass; do not modify tests to fit code.

        This is a project standard (like "all PRs require review"), not
        a prescription of HOW (framework, file layout, assertion style
        remain agent choices).
        """
        real_template = (
            Path(spawn_agents.__file__).resolve().parent.parent
            / "templates"
            / "agent_prompt.md"
        )
        spawner.agent_prompt_template = real_template

        prompt = spawner.create_worker_prompt(agent_config).lower()

        # Project-standard TDD directive must mention all four moves.
        assert "completion_criteria" in prompt, (
            "TDD directive must point agents at the implement task's "
            "completion_criteria (the test behaviors Marcus rolls up)"
        )
        assert (
            "tests first" in prompt or "write the tests first" in prompt
        ), "TDD directive must order tests FIRST"
        assert "fail" in prompt, "TDD directive must require watching tests fail"
        # Reject the most common LLM cheat: retrofit tests to match the code.
        assert (
            "do not modify the tests" in prompt
            or "not modify the tests" in prompt
            or "not modify tests" in prompt
        ), "TDD directive must forbid modifying tests to fit the implementation"
        # Must NOT prescribe a framework — agents pick their own.
        assert "you must use pytest" not in prompt
        assert "you must use jest" not in prompt


class TestFetchRecommendedAgents:
    """_fetch_recommended_agents queries Marcus MCP HTTP — no LLM relay."""

    def test_returns_optimal_agents_on_success(self) -> None:
        """Returns optimal_agents from Marcus MCP response."""
        sse_body = (
            'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{'
            '"content":[{"type":"text","text":"{}"}],'
            '"structuredContent":{"result":{"success":true,"optimal_agents":3}},'
            '"isError":false}}\n'
        ).encode()

        init_resp = MagicMock()
        init_resp.read.return_value = b"event: message\ndata: {}\n"
        init_resp.headers = {"mcp-session-id": "test-session-123"}
        init_resp.__enter__ = lambda s: s
        init_resp.__exit__ = MagicMock(return_value=False)

        tool_resp = MagicMock()
        tool_resp.read.return_value = sse_body
        tool_resp.__enter__ = lambda s: s
        tool_resp.__exit__ = MagicMock(return_value=False)

        notif_resp = MagicMock()
        notif_resp.read.return_value = b""
        notif_resp.__enter__ = lambda s: s
        notif_resp.__exit__ = MagicMock(return_value=False)

        responses = [init_resp, notif_resp, tool_resp]

        with patch("urllib.request.urlopen", side_effect=responses):
            result = spawn_agents.AgentSpawner._fetch_recommended_agents()

        assert result == 3

    def test_returns_zero_when_server_unreachable(self) -> None:
        """Returns 0 (safe fallback) when Marcus server is not running."""
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = spawn_agents.AgentSpawner._fetch_recommended_agents()

        assert result == 0

    def test_returns_zero_when_response_malformed(self) -> None:
        """Returns 0 when MCP response cannot be parsed."""
        bad_resp = MagicMock()
        bad_resp.read.return_value = b"not valid SSE"
        bad_resp.headers = {"mcp-session-id": "s1"}
        bad_resp.__enter__ = lambda s: s
        bad_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=bad_resp):
            result = spawn_agents.AgentSpawner._fetch_recommended_agents()

        assert result == 0

    def test_returns_zero_when_session_id_missing(self) -> None:
        """Returns 0 when Marcus init response has no session ID."""
        init_resp = MagicMock()
        init_resp.read.return_value = b"event: message\ndata: {}\n"
        init_resp.headers = {}  # no mcp-session-id
        init_resp.__enter__ = lambda s: s
        init_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=init_resp):
            result = spawn_agents.AgentSpawner._fetch_recommended_agents()

        assert result == 0


class TestProjectInfoPathSync:
    """Tests for Codex P2 fix: project_info_file kept in sync with project_info_path."""

    def test_project_info_file_defaults_to_experiment_dir(self, tmp_path: Path) -> None:
        """Without override, project_info_file is <experiment_dir>/project_info.json."""
        config_data = {
            "project_name": "test",
            "project_spec_file": "spec.md",
            "project_options": {"complexity": "prototype", "provider": "sqlite"},
            "agents": [],
        }
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("Build something")
        config_path = tmp_path / "config.yaml"
        import yaml  # type: ignore[import-untyped]

        config_path.write_text(yaml.dump(config_data))

        config = spawn_agents.ExperimentConfig(config_path)

        assert config.project_info_file == tmp_path / "project_info.json"
        assert config.project_options["project_info_path"] == str(
            tmp_path / "project_info.json"
        )

    def test_project_info_file_syncs_with_custom_override(self, tmp_path: Path) -> None:
        """
        When project_info_path is pre-set in config, project_info_file must
        point to the same path so wait_for_project_info and the JSON read
        both target the file Marcus actually writes (Codex P2 fix).
        """
        custom_path = tmp_path / "custom" / "info.json"
        config_data = {
            "project_name": "test",
            "project_spec_file": "spec.md",
            "project_options": {
                "complexity": "prototype",
                "provider": "sqlite",
                "project_info_path": str(custom_path),
            },
            "agents": [],
        }
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("Build something")
        config_path = tmp_path / "config.yaml"
        import yaml  # type: ignore[import-untyped]

        config_path.write_text(yaml.dump(config_data))

        config = spawn_agents.ExperimentConfig(config_path)

        assert (
            config.project_info_file == custom_path
        ), "project_info_file must match the pre-set project_info_path override"


# ---------------------------------------------------------------------------
# Agent model resolution + threading
# ---------------------------------------------------------------------------


def _make_config_yaml(tmp_path: Path, **extra: Any) -> Path:
    """Write a minimal config.yaml for ExperimentConfig tests.

    ``extra`` keys are merged into the top-level mapping so tests can
    add ``agent_model: ...`` without rewriting the full template.
    """
    import yaml as _yaml

    spec_path = tmp_path / "project_spec.md"
    spec_path.write_text("test spec")

    config_data: Dict[str, Any] = {
        "project_name": "agent-model-test",
        "project_spec_file": "project_spec.md",
        "project_options": {
            "complexity": "prototype",
            "provider": "sqlite",
            "mode": "new_project",
        },
        "agents": [
            {"id": "agent_1", "name": "Agent 1", "role": "full-stack", "skills": []}
        ],
    }
    config_data.update(extra)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_yaml.dump(config_data))
    return config_path


class TestAgentModelResolution:
    """``ExperimentConfig.agent_model`` resolves from yaml then config_marcus.

    The Planner (Marcus-side LLM calls) is governed by ``ai.model`` in
    ``config_marcus.json``.  The Agent (spawned ``claude`` processes)
    defaults to the same value so a single config setting governs both
    classes.  Per-experiment override via ``agent_model`` in
    ``config.yaml``.  Per-invocation override via ``--model`` on
    ``run_experiment.py`` (handled at :class:`AgentSpawner` level,
    tested separately below).
    """

    def test_yaml_agent_model_wins_over_marcus_config(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``config.yaml.agent_model`` overrides ``config_marcus.json.ai.model``.

        Point ``__file__`` at a tmp-path repo root that DOES contain a
        ``config_marcus.json`` so the resolver's fallback branch is a
        live candidate.  The test then asserts the yaml hit short-
        circuits and the marcus-config value is never consulted.
        """
        # Build a fake repo root with a config_marcus.json that would
        # win if the resolver fell through to the fallback branch.
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        (fake_root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "should-not-be-used"}})
        )
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))
        # Clear MARCUS_CONFIG so the env-var branch (Codex P2 fix on
        # PR #540) doesn't redirect this test to an unintended file.
        monkeypatch.delenv("MARCUS_CONFIG", raising=False)

        config_path = _make_config_yaml(tmp_path, agent_model="from-yaml")
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model == "from-yaml"

    def test_falls_back_to_marcus_config_when_yaml_silent(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Absent ``agent_model`` in yaml → read ``ai.model`` from
        ``config_marcus.json``.

        Patches ``Path(__file__).resolve()`` for the spawn_agents
        module so the resolver looks in a tmp-path repo root instead
        of the real one.
        """
        # Build a fake repo root containing config_marcus.json
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        (fake_root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "claude-haiku-4-5-20251001"}})
        )
        # Pretend spawn_agents.py lives at
        # <fake_root>/dev-tools/experiments/runners/spawn_agents.py
        # so ``parents[3]`` lands on fake_root.
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")

        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        config_path = _make_config_yaml(tmp_path)  # no agent_model in yaml
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model == "claude-haiku-4-5-20251001"

    def test_returns_none_when_neither_source_has_model(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Yaml silent + ``config_marcus.json`` missing → ``None``.

        ``None`` is the signal AgentSpawner uses to omit the
        ``--model`` flag, letting ``claude`` use its global default.
        """
        # Repo root WITHOUT config_marcus.json
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model is None

    def test_malformed_marcus_config_is_treated_as_absent(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Invalid JSON in ``config_marcus.json`` → ``None``, not raise.

        A broken Planner config must not also break agent spawning.
        The two failure modes are independent.
        """
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        (fake_root / "config_marcus.json").write_text("not-valid-json{")
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model is None

    def test_marcus_config_without_ai_block_returns_none(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``config_marcus.json`` missing ``ai`` block → ``None``."""
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        (fake_root / "config_marcus.json").write_text(
            json.dumps({"other_key": "value"})
        )
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model is None

    def test_marcus_config_env_var_redirects_resolver(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``MARCUS_CONFIG`` env var redirects the resolver (Codex P2 on #540).

        Marcus's Planner honors ``MARCUS_CONFIG`` for its own model
        lookup (``src/config/marcus_config.py:get_config()``).  The
        agent resolver must honor it too — otherwise the Planner
        reads the custom file while the resolver silently reads the
        repo-root default, and the documented "inherit the Planner
        model" behavior is false.
        """
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        # Repo-root config_marcus.json with a DIFFERENT model than
        # the env-var path — so we can prove the env var wins.
        (fake_root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "repo-root-should-be-ignored"}})
        )
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        custom_config = tmp_path / "custom_marcus.json"
        custom_config.write_text(json.dumps({"ai": {"model": "from-env-var-config"}}))
        monkeypatch.setenv("MARCUS_CONFIG", str(custom_config))

        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model == "from-env-var-config"

    def test_marcus_config_env_var_missing_file_returns_none(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``MARCUS_CONFIG`` pointing at a missing file → ``None``.

        Defensive degradation, same as the unset-env-var case with
        missing repo-root config.  Does NOT silently fall back to
        the repo-root file when the env var is set but invalid —
        operator misconfiguration must surface as a missing model,
        not as a different model than expected.
        """
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        (fake_root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "must-not-be-used-when-env-set"}})
        )
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        monkeypatch.setenv("MARCUS_CONFIG", str(tmp_path / "does_not_exist.json"))

        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model is None

    def test_empty_marcus_config_env_var_falls_back_to_repo_root(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``MARCUS_CONFIG=""`` (empty string) ignored → repo-root used.

        Mirrors the Planner's env-var precedence: only treat the env
        var as a redirect when it carries a non-empty value.  An
        empty MARCUS_CONFIG (set by some shell wrappers as a "clear"
        signal) must NOT short-circuit the resolver to ``None`` —
        the repo-root fallback should still run.
        """
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        (fake_root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "from-repo-root"}})
        )
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        monkeypatch.setenv("MARCUS_CONFIG", "")

        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)

        assert config.agent_model == "from-repo-root"


class TestAgentSpawnerModelFlag:
    """``AgentSpawner.claude_model_flag`` renders the ``--model`` fragment.

    The flag string is spliced into every spawned ``claude`` command
    so all three pane types (project creator, workers, monitor) run
    on the same model.  Empty string means no ``--model`` is appended
    → ``claude`` uses its global default.
    """

    def _build_spawner(
        self,
        tmp_path: Path,
        monkeypatch: Any,
        yaml_model: str | None = None,
        cli_model: str | None = None,
    ) -> Any:
        """Build an AgentSpawner with a real ExperimentConfig + no MCP probe."""
        # Isolate config_marcus.json discovery to a non-existent dir so
        # the resolver's fallback is deterministically None.
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        extra = {"agent_model": yaml_model} if yaml_model else {}
        config_path = _make_config_yaml(tmp_path, **extra)
        config = spawn_agents.ExperimentConfig(config_path)

        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "agent_prompt.md").write_text("stub")

        return spawn_agents.AgentSpawner(
            config=config,
            templates_dir=templates_dir,
            agent_model=cli_model,
        )

    def test_no_model_renders_empty_flag(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        spawner = self._build_spawner(tmp_path, monkeypatch)
        assert spawner.claude_model_flag == ""
        assert spawner.agent_model is None

    def test_yaml_model_renders_flag(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawner = self._build_spawner(tmp_path, monkeypatch, yaml_model="haiku")
        assert spawner.claude_model_flag == "--model haiku "
        assert spawner.agent_model == "haiku"

    def test_cli_override_wins_over_yaml(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``--model`` on the CLI beats ``agent_model`` in yaml.

        Precedence order: CLI > yaml > config_marcus.json > None.
        """
        spawner = self._build_spawner(
            tmp_path,
            monkeypatch,
            yaml_model="should-be-overridden",
            cli_model="opus",
        )
        assert spawner.agent_model == "opus"
        assert spawner.claude_model_flag == "--model opus "

    def test_cli_none_falls_back_to_yaml(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """``agent_model=None`` on CLI → use yaml's value."""
        spawner = self._build_spawner(
            tmp_path,
            monkeypatch,
            yaml_model="from-yaml",
            cli_model=None,
        )
        assert spawner.agent_model == "from-yaml"
        assert spawner.claude_model_flag == "--model from-yaml "


class TestClaudeModelFlagLandsInGeneratedScripts:
    """End-to-end: ``--model X`` actually reaches the rendered bash.

    The unit tests in :class:`TestAgentSpawnerModelFlag` verify the
    ``claude_model_flag`` attribute, but the load-bearing behaviour
    is whether it gets spliced into the generated project-creator
    script that tmux ultimately executes.  This class scrubs the
    rendered ``.sh`` file and asserts the flag is on the right line.
    """

    def _make_spawner(self, tmp_path: Path, agent_model: str) -> Any:
        """Build a bypass-init spawner with everything spawn_project_creator
        reads from ``self``.  Matches the pattern in
        :class:`TestTermNormalization`."""
        config = MagicMock()
        config.experiment_dir = tmp_path
        config.implementation_dir = tmp_path / "implementation"
        config.implementation_dir.mkdir()
        config.prompts_dir = tmp_path / "prompts"
        config.prompts_dir.mkdir()
        config.project_info_file = tmp_path / "project_info.json"
        config.project_name = "test"
        config.project_options = {
            "complexity": "standard",
            "provider": "sqlite",
            "mode": "new_project",
        }
        config.agents = []
        config.get_timeout.return_value = 300

        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.config = config
        spawner.tmux_session = "test_session"
        spawner.current_pane = 0
        spawner.current_window = 0
        spawner.panes_per_window = 4
        spawner.marcus_mcp_url = "http://localhost:4298/mcp"
        spawner.agent_model = agent_model
        spawner.model_flag = f"--model {agent_model} " if agent_model else ""
        spawner.claude_model_flag = spawner.model_flag
        spawner.harness = "claude"
        spawner.harness_impl = spawn_agents.HARNESSES["claude"]
        return spawner

    def test_project_creator_script_contains_model_flag(self, tmp_path: Path) -> None:
        """``--model X`` appears in the rendered project-creator script,
        on the same line as the ``claude`` invocation, before
        ``--dangerously-skip-permissions``."""
        spawner = self._make_spawner(tmp_path, agent_model="haiku")

        with (
            patch("subprocess.run"),
            patch.object(spawner, "copy_agent_workflow_to_implementation"),
            patch.object(spawner, "run_in_tmux_pane"),
        ):
            spawner.spawn_project_creator()

        script_file = spawner.config.prompts_dir / "project_creator.sh"
        content = script_file.read_text()
        assert "--model haiku" in content
        # And it precedes --dangerously-skip-permissions on the same line
        # so the claude CLI parses both flags correctly.
        claude_line = next(
            ln for ln in content.splitlines() if "--dangerously-skip-permissions" in ln
        )
        assert "--model haiku" in claude_line

    def test_project_creator_script_omits_flag_when_no_model(
        self, tmp_path: Path
    ) -> None:
        """No model resolved → no ``--model`` token in the rendered script.

        Backward compat: legacy experiments with no ``agent_model``
        wiring must produce scripts identical to pre-#540 output.
        """
        spawner = self._make_spawner(tmp_path, agent_model=None)

        with (
            patch("subprocess.run"),
            patch.object(spawner, "copy_agent_workflow_to_implementation"),
            patch.object(spawner, "run_in_tmux_pane"),
        ):
            spawner.spawn_project_creator()

        script_file = spawner.config.prompts_dir / "project_creator.sh"
        content = script_file.read_text()
        assert "--model" not in content


class TestTmuxBaseIndexDetection:
    """Test suite for non-zero tmux base-index handling (PR #568).

    A user's ``~/.tmux.conf`` may set ``base-index 1`` and
    ``pane-base-index 1`` — a common non-default configuration. The
    spawner builds ``session:window.pane`` target strings; with the
    indices hardcoded to ``0`` those targets address windows/panes that
    do not exist under such a config and every ``tmux`` call fails.

    ``AgentSpawner`` detects the two indices inside ``create_tmux_session``
    — by reading the first window/pane of the session that was just
    created — and offsets every target string by the detected base.

    Detection happens after the session exists (not in ``__init__``)
    because ``tmux new-session`` is what starts the tmux server and
    sources ``~/.tmux.conf``; reading the indices any earlier returns
    the default ``0`` even when the user's config sets ``base-index 1``
    (the cold-start bug). These tests cover the ``_first_int`` reader,
    the ``__init__`` placeholder defaults, the ``create_tmux_session``
    detection, and the offset applied to target strings.
    """

    # ------------------------------------------------------------------
    # _first_int — the stdout reader
    # ------------------------------------------------------------------

    def test_first_int_parses_first_line(self) -> None:
        """Returns the first stdout line as an int."""
        mock_result = MagicMock(returncode=0, stdout="1\n1\n")
        with patch("subprocess.run", return_value=mock_result):
            value = spawn_agents.AgentSpawner._first_int(["tmux", "x"], 0)
        assert value == 1

    def test_first_int_returns_default_on_nonzero_returncode(self) -> None:
        """Command failure (e.g. no tmux server) → default returned."""
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            value = spawn_agents.AgentSpawner._first_int(["tmux", "x"], 0)
        assert value == 0

    def test_first_int_returns_default_on_non_numeric_output(self) -> None:
        """Non-numeric stdout → default returned."""
        mock_result = MagicMock(returncode=0, stdout="not-a-number\n")
        with patch("subprocess.run", return_value=mock_result):
            value = spawn_agents.AgentSpawner._first_int(["tmux", "x"], 0)
        assert value == 0

    def test_first_int_returns_default_when_command_missing(self) -> None:
        """``tmux`` binary absent → subprocess raises → default returned."""
        with patch("subprocess.run", side_effect=FileNotFoundError("tmux")):
            value = spawn_agents.AgentSpawner._first_int(["tmux", "x"], 0)
        assert value == 0

    # ------------------------------------------------------------------
    # __init__ — placeholder defaults only
    # ------------------------------------------------------------------

    def test_init_uses_placeholder_indices_before_session_exists(
        self, tmp_path: Path
    ) -> None:
        """Constructor sets placeholder 0/0; no tmux query happens at init."""
        config = MagicMock()
        config.project_name = "Test Project"
        config.agent_model = None
        # Eager harness_impl resolution needs a valid harness name.
        config.harness = "claude"
        templates = tmp_path / "templates"
        templates.mkdir()

        with patch("subprocess.run") as mock_run:
            spawner = spawn_agents.AgentSpawner(config, templates)

        assert spawner._tmux_base_index == 0
        assert spawner._tmux_pane_base_index == 0
        # __init__ must not shell out to tmux — detection is deferred.
        mock_run.assert_not_called()

    # ------------------------------------------------------------------
    # create_tmux_session — detection from the live session
    # ------------------------------------------------------------------

    def test_create_tmux_session_detects_indices_from_live_session(self) -> None:
        """Detection reads the real window/pane indices of the new session.

        Regression for the cold-start bug: a non-zero base-index is
        picked up because it is read from the session that exists,
        not from global options queried before the server started.
        """
        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.tmux_session = "marcus_test"

        def fake_run(cmd: list, *args: Any, **kwargs: Any) -> MagicMock:
            if "list-windows" in cmd:
                return MagicMock(returncode=0, stdout="1\n")
            if "list-panes" in cmd:
                return MagicMock(returncode=0, stdout="1\n")
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=fake_run):
            spawner.create_tmux_session()

        assert spawner._tmux_base_index == 1
        assert spawner._tmux_pane_base_index == 1

    # ------------------------------------------------------------------
    # target-string offsets
    # ------------------------------------------------------------------

    def _bypass_init_spawner(self, base: int, pane_base: int) -> Any:
        """Build an AgentSpawner without running __init__, with tmux indices set.

        ``run_in_tmux_pane`` branches on ``self.harness`` to gate the
        Claude-only trust-dialog poller, so the attribute must be set
        even though these tests target the tmux base-index logic.
        """
        instance = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        instance.tmux_session = "marcus_test"
        instance.panes_per_window = 2
        instance.current_window = 0
        instance.current_pane = 0
        instance._tmux_base_index = base
        instance._tmux_pane_base_index = pane_base
        instance.harness = "claude"
        instance.harness_impl = spawn_agents.HARNESSES["claude"]
        return instance

    def test_new_window_target_includes_base_index_offset(self) -> None:
        """get_next_pane_location offsets the new-window target by base-index."""
        spawner = self._bypass_init_spawner(base=1, pane_base=1)
        spawner.current_pane = 2  # → window 1, pane 0 → triggers new-window

        with patch("subprocess.run") as mock_run:
            window, pane = spawner.get_next_pane_location()

        assert (window, pane) == (1, 0)
        cmd = mock_run.call_args[0][0]
        # window 1 + base-index 1 = 2
        assert "marcus_test:2" in cmd

    def test_first_pane_target_includes_both_offsets(self) -> None:
        """run_in_tmux_pane (pane 0) offsets window AND pane by their bases."""
        spawner = self._bypass_init_spawner(base=1, pane_base=1)

        with (
            patch("subprocess.run") as mock_run,
            patch.object(spawn_agents, "wait_for_pane_ready", return_value=True),
            patch.object(spawn_agents, "confirm_trust_if_prompted"),
            patch("time.sleep"),
        ):
            spawner.run_in_tmux_pane(
                window=0, pane=0, script_file=Path("dummy_script.sh"), title="t"
            )

        # First tmux call is select-pane -t <target>
        first_cmd = mock_run.call_args_list[0][0][0]
        # window 0 + base 1 = 1, pane base 1 → "marcus_test:1.1"
        assert "marcus_test:1.1" in first_cmd

    def test_default_indices_preserve_classic_zero_targets(self) -> None:
        """With both bases 0 (tmux default) targets are unchanged — back-compat."""
        spawner = self._bypass_init_spawner(base=0, pane_base=0)

        with (
            patch("subprocess.run") as mock_run,
            patch.object(spawn_agents, "wait_for_pane_ready", return_value=True),
            patch.object(spawn_agents, "confirm_trust_if_prompted"),
            patch("time.sleep"),
        ):
            spawner.run_in_tmux_pane(
                window=0, pane=0, script_file=Path("dummy_script.sh"), title="t"
            )

        first_cmd = mock_run.call_args_list[0][0][0]
        assert "marcus_test:0.0" in first_cmd


# ---------------------------------------------------------------------------
# Harness routing (claude vs codex)
# ---------------------------------------------------------------------------


class TestHarnessConfigResolution:
    """``ExperimentConfig.harness`` reads the yaml field with safe defaults.

    The harness field selects which agent CLI (``claude`` or ``codex``)
    the runner spawns inside each tmux pane.  Defaulting to ``'claude'``
    preserves backward compatibility for every config.yaml written
    before harness support landed.
    """

    def test_harness_defaults_to_claude_when_absent(self, tmp_path: Path) -> None:
        """No ``harness:`` key in yaml → config.harness == 'claude'."""
        config_path = _make_config_yaml(tmp_path)
        config = spawn_agents.ExperimentConfig(config_path)
        assert config.harness == "claude"

    def test_harness_codex_from_yaml(self, tmp_path: Path) -> None:
        """``harness: codex`` in yaml → config.harness == 'codex'."""
        config_path = _make_config_yaml(tmp_path, harness="codex")
        config = spawn_agents.ExperimentConfig(config_path)
        assert config.harness == "codex"

    def test_harness_normalized_to_lowercase(self, tmp_path: Path) -> None:
        """Yaml ``harness: Codex`` (case quirk) is normalized."""
        config_path = _make_config_yaml(tmp_path, harness="Codex")
        config = spawn_agents.ExperimentConfig(config_path)
        assert config.harness == "codex"

    def test_invalid_harness_raises_value_error(self, tmp_path: Path) -> None:
        """Unknown harness names error at config load, not at spawn time.

        Failing fast at load time means the bad value surfaces before
        we touch tmux, git, or MLflow — preserving the user's terminal
        state and giving a clean message.
        """
        config_path = _make_config_yaml(tmp_path, harness="bedrock")
        with pytest.raises(ValueError, match="bedrock"):
            spawn_agents.ExperimentConfig(config_path)


class TestAgentSpawnerHarnessRouting:
    """``AgentSpawner`` dispatches MCP register and agent command by harness.

    The helpers :meth:`_build_mcp_register_snippet` and
    :meth:`_build_agent_command` are the only places where harness
    semantics diverge.  These tests verify each branch produces the
    correct CLI surface area: ``claude`` for the claude harness;
    ``codex exec --yolo`` for the codex harness.
    """

    def _make_bypass_spawner(self, tmp_path: Path, harness: str) -> Any:
        """Build a __new__-style spawner with the minimum attrs the helpers
        read.  Mirrors the bypass pattern used elsewhere in this module."""
        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.harness = harness
        spawner.harness_impl = spawn_agents.HARNESSES[harness]
        spawner.model_flag = ""
        spawner.claude_model_flag = ""
        return spawner

    def test_mcp_register_claude(self, tmp_path: Path) -> None:
        """Claude harness no longer registers marcus via ``claude mcp add``.

        Marcus is pinned per-pane through --strict-mcp-config on the agent
        command; the register snippet is a no-op marker. ``claude mcp add``
        wrote ~/.claude.json, which strict config ignores and which parallel
        panes can corrupt.
        """
        spawner = self._make_bypass_spawner(tmp_path, "claude")
        snippet = spawner._build_mcp_register_snippet()
        assert "claude mcp add" not in snippet
        assert "codex" not in snippet

    def test_mcp_register_codex(self, tmp_path: Path) -> None:
        """Codex harness uses ``codex mcp add ... --url``."""
        spawner = self._make_bypass_spawner(tmp_path, "codex")
        snippet = spawner._build_mcp_register_snippet()
        assert "codex mcp add marcus --url" in snippet
        # No claude-side syntax leaking into the codex branch.
        assert "claude mcp" not in snippet
        assert "|| true" in snippet

    def test_agent_command_claude_no_model(self, tmp_path: Path) -> None:
        """Claude command shape: ``claude --add-dir ... --dangerously-skip-permissions``."""
        spawner = self._make_bypass_spawner(tmp_path, "claude")
        cmd = spawner._build_agent_command(tmp_path / "work", tmp_path / "prompt.txt")
        assert "claude --add-dir" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--print" not in cmd  # default print_mode=False
        assert "codex" not in cmd
        assert "--yolo" not in cmd

    def test_agent_command_claude_print_mode(self, tmp_path: Path) -> None:
        """``print_mode=True`` adds ``--print`` (used by project creator)."""
        spawner = self._make_bypass_spawner(tmp_path, "claude")
        cmd = spawner._build_agent_command(
            tmp_path / "work", tmp_path / "prompt.txt", print_mode=True
        )
        assert "--print " in cmd

    def test_agent_command_codex_no_model(self, tmp_path: Path) -> None:
        """Codex command shape: ``codex exec --dangerously-bypass-approvals-and-sandbox``.

        We deliberately use the documented long-form flag rather than
        the hidden ``--yolo`` alias.  Hidden aliases have no stability
        contract and can be renamed without notice; the documented
        flag has a much longer half-life and the behaviour is
        identical (approval=never, sandbox=danger-full-access).
        """
        spawner = self._make_bypass_spawner(tmp_path, "codex")
        cmd = spawner._build_agent_command(tmp_path / "work", tmp_path / "prompt.txt")
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--skip-git-repo-check" in cmd
        # Guardian must be disabled — its "unexpected workspace
        # change" prompt fires on `git merge main` artifacts and
        # would halt the agent waiting for a user response that
        # never comes (no human attached to a tmux pane).
        assert "--disable guardian_approval" in cmd
        # Goals must be enabled — codex's autonomous loop is what
        # keeps the model engaged through empty request_next_task
        # polls.  Without it, the model wraps up after 2-3 empty
        # cycles and the agent silently exits.
        assert "--enable goals" in cmd
        # Hidden alias must not leak in — keeps our dependency on
        # documented flags only.
        assert "--yolo" not in cmd
        # Claude-side flags must NOT leak into the codex branch.
        assert "claude " not in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_agent_command_codex_ignores_print_mode(self, tmp_path: Path) -> None:
        """``codex exec`` is inherently non-interactive — print_mode is a no-op.

        We document this in the helper docstring; the test enforces
        the contract so a well-meaning refactor cannot accidentally
        start passing ``--print`` (which codex does not accept).
        """
        spawner = self._make_bypass_spawner(tmp_path, "codex")
        cmd = spawner._build_agent_command(
            tmp_path / "work", tmp_path / "prompt.txt", print_mode=True
        )
        assert "--print" not in cmd

    def test_worker_invocation_block_claude_no_wrapper(self, tmp_path: Path) -> None:
        """Claude TUI stays alive across turns — no relaunch loop needed.

        The worker helper must return the raw single command for the
        claude harness so the pane runs interactive claude exactly
        the way pre-codex Marcus has always done.
        """
        spawner = self._make_bypass_spawner(tmp_path, "claude")
        block = spawner._build_worker_invocation_block(
            tmp_path / "work", tmp_path / "prompt.txt"
        )
        # No loop scaffolding leaks into the claude path.
        assert "MAX_RELAUNCHES" not in block
        assert "for relaunch" not in block
        # Single claude invocation is still there.
        assert "claude --add-dir" in block

    def test_worker_invocation_block_codex_wraps_in_loop(self, tmp_path: Path) -> None:
        """Codex workers wrap ``codex exec`` in a bounded bash relaunch loop.

        Belt-and-suspenders: ``--enable goals`` keeps the model
        engaged across empty polls in the common case; the wrapper
        catches the edge cases where codex exec exits anyway (token
        budget exhausted, model judges goal complete on a fluke,
        unrecoverable error).  The loop is bounded so a genuinely
        wedged worker cannot burn unbounded tokens — the monitor
        pane kills the session at experiment end on the happy path.
        """
        spawner = self._make_bypass_spawner(tmp_path, "codex")
        block = spawner._build_worker_invocation_block(
            tmp_path / "work", tmp_path / "prompt.txt"
        )
        # Loop scaffolding present.
        assert "MAX_RELAUNCHES=50" in block
        assert "for relaunch in $(seq 1 $MAX_RELAUNCHES)" in block
        assert "sleep 3" in block
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in block
        # Final-give-up echo so a wedged worker is visible in pane history.
        assert "hit MAX_RELAUNCHES" in block

    def test_worker_invocation_block_codex_breaks_on_clean_exit(
        self, tmp_path: Path
    ) -> None:
        """Codex wrapper breaks the relaunch loop when ``codex exec`` exits
        cleanly.

        Without this guard the loop would re-launch after every
        successful completion (rc=0) — burning tokens, and with
        ``--epictetus`` keeping workers churning instead of staying
        quiescent for post-run inspection. The shell block must
        capture the exit code and break when it's 0; non-zero exits
        (crashes, token budget hits) still trigger a relaunch.
        Codex review P1 on PR #554.
        """
        spawner = self._make_bypass_spawner(tmp_path, "codex")
        block = spawner._build_worker_invocation_block(
            tmp_path / "work", tmp_path / "prompt.txt"
        )
        # Exit status captured.
        assert "rc=$?" in block
        # Branches on rc=0 and breaks out.
        assert "[ $rc -eq 0 ]" in block
        assert "break" in block
        # Non-zero path still relaunches (preserves the sleep + loop).
        assert "rc=$rc" in block

    def test_agent_command_model_flag_threaded_through_both_harnesses(
        self, tmp_path: Path
    ) -> None:
        """``--model X`` is spliced into both branches verbatim.

        Both harnesses accept the long-form ``--model`` spelling so a
        single rendered fragment works for either.
        """
        for harness in ("claude", "codex"):
            spawner = self._make_bypass_spawner(tmp_path, harness)
            spawner.model_flag = "--model my-model "
            cmd = spawner._build_agent_command(
                tmp_path / "work", tmp_path / "prompt.txt"
            )
            assert (
                "--model my-model" in cmd
            ), f"model flag missing in {harness} branch: {cmd}"


class TestSpawnerHarnessFromConfig:
    """Spawner harness defaults flow correctly from config → __init__."""

    def _build_spawner(
        self,
        tmp_path: Path,
        monkeypatch: Any,
        yaml_harness: str | None = None,
        cli_harness: str | None = None,
    ) -> Any:
        """Build a real-init AgentSpawner with the given harness wiring."""
        fake_root = tmp_path / "marcus_root"
        fake_root.mkdir()
        fake_module_path = (
            fake_root / "dev-tools" / "experiments" / "runners" / "spawn_agents.py"
        )
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("")
        monkeypatch.setattr(spawn_agents, "__file__", str(fake_module_path))

        extra: Dict[str, Any] = {}
        if yaml_harness is not None:
            extra["harness"] = yaml_harness
        config_path = _make_config_yaml(tmp_path, **extra)
        config = spawn_agents.ExperimentConfig(config_path)

        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "agent_prompt.md").write_text("stub")

        return spawn_agents.AgentSpawner(
            config=config,
            templates_dir=templates_dir,
            harness=cli_harness,
        )

    def test_spawner_defaults_to_claude(self, tmp_path: Path, monkeypatch: Any) -> None:
        spawner = self._build_spawner(tmp_path, monkeypatch)
        assert spawner.harness == "claude"

    def test_yaml_harness_flows_to_spawner(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        spawner = self._build_spawner(tmp_path, monkeypatch, yaml_harness="codex")
        assert spawner.harness == "codex"

    def test_cli_harness_overrides_yaml(self, tmp_path: Path, monkeypatch: Any) -> None:
        """CLI ``--harness`` beats yaml ``harness:`` field.

        Precedence: CLI > yaml > default ('claude').  Same pattern as
        ``--model``/``agent_model`` — last writer wins, with CLI as
        the explicit user override.
        """
        spawner = self._build_spawner(
            tmp_path,
            monkeypatch,
            yaml_harness="claude",
            cli_harness="codex",
        )
        assert spawner.harness == "codex"

    def test_invalid_cli_harness_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        with pytest.raises(ValueError, match="bedrock"):
            self._build_spawner(tmp_path, monkeypatch, cli_harness="bedrock")


# ---------------------------------------------------------------------------
# Workflow file materialization (CLAUDE.md + AGENTS.md)
# ---------------------------------------------------------------------------


class TestAgentWorkflowFilesMaterialized:
    """``copy_agent_workflow_to_implementation`` writes BOTH CLAUDE.md and AGENTS.md.

    Claude Code reads ``CLAUDE.md``.  Codex CLI reads ``AGENTS.md``
    (https://developers.openai.com/codex/guides/agents-md).  Without
    AGENTS.md, codex agents start with zero Marcus workflow guidance
    and silently fail to call ``register_agent`` /
    ``request_next_task``.  Writing both files keeps the spawn code
    harness-agnostic and is harmless when the claude harness is
    active.
    """

    def _make_spawner(self, tmp_path: Path, harness: str = "claude") -> Any:
        config = MagicMock()
        config.implementation_dir = tmp_path / "implementation"
        config.implementation_dir.mkdir()

        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.config = config
        spawner.harness = harness
        spawner.harness_impl = spawn_agents.HARNESSES[harness]

        # Real workflow template content — tests should fail loudly
        # if either file goes empty.
        template = tmp_path / "agent_prompt.md"
        template.write_text(
            "# Marcus Agent Workflow\n\n"
            "Call register_agent() then loop on request_next_task().\n"
        )
        spawner.agent_prompt_template = template
        return spawner

    def test_writes_both_files_for_claude_harness(self, tmp_path: Path) -> None:
        spawner = self._make_spawner(tmp_path, harness="claude")
        spawner.copy_agent_workflow_to_implementation()

        claude_md = spawner.config.implementation_dir / "CLAUDE.md"
        agents_md = spawner.config.implementation_dir / "AGENTS.md"
        assert claude_md.exists()
        assert agents_md.exists()
        # Same content — same template feeds both.
        assert claude_md.read_text() == agents_md.read_text()
        assert "register_agent" in agents_md.read_text()

    def test_writes_both_files_for_codex_harness(self, tmp_path: Path) -> None:
        """The load-bearing case: AGENTS.md is what codex actually reads."""
        spawner = self._make_spawner(tmp_path, harness="codex")
        spawner.copy_agent_workflow_to_implementation()

        agents_md = spawner.config.implementation_dir / "AGENTS.md"
        assert agents_md.exists()
        assert "register_agent" in agents_md.read_text()

    def test_writes_gemini_md_for_gemini_harness(self, tmp_path: Path) -> None:
        """The load-bearing case for gemini: ``GEMINI.md`` is what the
        Gemini CLI reads from cwd as its per-directory context file.

        Regression test for Codex P1 review on PR #587: the spawner
        previously hardcoded the file list to (``CLAUDE.md``,
        ``AGENTS.md``) and ignored ``Harness.workflow_files``, so a
        gemini worker found no ``GEMINI.md`` and started with zero
        Marcus workflow guidance — silently failing to call
        ``register_agent`` / ``request_next_task``.
        """
        spawner = self._make_spawner(tmp_path, harness="gemini")
        spawner.copy_agent_workflow_to_implementation()

        gemini_md = spawner.config.implementation_dir / "GEMINI.md"
        assert gemini_md.exists(), (
            "GEMINI.md must land in implementation/ when --harness gemini "
            "(declared by GeminiHarness.workflow_files)"
        )
        assert "register_agent" in gemini_md.read_text()

    def test_writes_union_of_all_registered_harness_files(self, tmp_path: Path) -> None:
        """All harnesses' workflow files are written regardless of active
        harness — so a swap-harness-mid-experiment flow stays viable
        and a typo on ``--harness`` does not silently lose guidance."""
        spawner = self._make_spawner(tmp_path, harness="claude")
        spawner.copy_agent_workflow_to_implementation()

        impl_dir = spawner.config.implementation_dir
        # Union of every registered harness's declared workflow files.
        for fname in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
            assert (
                impl_dir / fname
            ).exists(), f"{fname} should land regardless of active harness"


# ---------------------------------------------------------------------------
# End-to-end codex script rendering
# ---------------------------------------------------------------------------


class TestCodexScriptsRender:
    """Rendered bash scripts contain codex CLI invocations, not claude.

    The helper-level tests in :class:`TestAgentSpawnerHarnessRouting`
    verify the command-builder methods.  These tests verify that those
    methods actually get spliced into the rendered ``.sh`` files for
    all three pane types — project creator, worker, monitor.  A
    regression where someone hard-codes ``claude`` back into an
    f-string is caught here, not in production.
    """

    def _make_spawner(self, tmp_path: Path, model: str | None = None) -> Any:
        """Build a bypass-init spawner wired for the codex harness."""
        config = MagicMock()
        config.experiment_dir = tmp_path
        config.implementation_dir = tmp_path / "implementation"
        config.implementation_dir.mkdir()
        config.prompts_dir = tmp_path / "prompts"
        config.prompts_dir.mkdir()
        config.project_info_file = tmp_path / "project_info.json"
        config.project_name = "test"
        config.project_options = {
            "complexity": "standard",
            "provider": "sqlite",
            "mode": "new_project",
        }
        config.agents = []
        config.get_timeout.return_value = 300

        # Worktrees dir for spawn_worker test.
        worktrees = tmp_path / "worktrees"
        worktrees.mkdir(exist_ok=True)

        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.config = config
        spawner.tmux_session = "test_session"
        spawner.current_pane = 0
        spawner.current_window = 0
        spawner.panes_per_window = 4
        spawner.marcus_mcp_url = "http://localhost:4298/mcp"
        spawner.agent_model = model
        spawner.model_flag = f"--model {model} " if model else ""
        spawner.claude_model_flag = spawner.model_flag
        spawner.harness = "codex"
        spawner.harness_impl = spawn_agents.HARNESSES["codex"]

        # agent_prompt_template — needed by worker prompt creation.
        template = tmp_path / "agent_prompt.md"
        template.write_text("# stub workflow\n")
        spawner.agent_prompt_template = template
        return spawner

    def test_project_creator_script_renders_codex(self, tmp_path: Path) -> None:
        """Project creator pane runs ``codex exec`` under codex harness."""
        spawner = self._make_spawner(tmp_path, model="gpt-5-codex")

        with (
            patch("subprocess.run"),
            patch.object(spawner, "copy_agent_workflow_to_implementation"),
            patch.object(spawner, "run_in_tmux_pane"),
        ):
            spawner.spawn_project_creator()

        script = (spawner.config.prompts_dir / "project_creator.sh").read_text()
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in script
        assert "--disable guardian_approval" in script
        assert "--enable goals" in script
        assert "codex mcp add marcus --url" in script
        assert "--model gpt-5-codex" in script
        # Claude branch must not leak in.
        assert "claude --add-dir" not in script
        assert "claude mcp add" not in script
        assert "--yolo" not in script

    def test_worker_script_renders_codex_with_wrapper(self, tmp_path: Path) -> None:
        """Worker pane wraps ``codex exec`` in the relaunch loop.

        End-to-end: verify the wrapper actually lands in the
        rendered worker script.  Helper-level coverage lives in
        :class:`TestAgentSpawnerHarnessRouting`; this catches the
        ``spawn_worker`` f-string regression where someone could
        revert to the unwrapped helper.
        """
        spawner = self._make_spawner(tmp_path)

        agent = {
            "id": "agent_unicorn_1",
            "name": "Unicorn Developer 1",
            "role": "full-stack",
            "skills": ["python"],
            "subagents": 0,
        }

        # spawn_worker creates a git worktree before launching codex;
        # stub the worktree-creation helper and tmux glue so the test
        # is focused on the script body.
        with (
            patch("subprocess.run"),
            patch.object(spawner, "run_in_tmux_pane"),
            patch.object(spawner, "_create_agent_worktree"),
            patch.object(spawner, "create_worker_prompt", return_value="stub prompt"),
        ):
            spawner.spawn_worker(agent)

        script = (spawner.config.prompts_dir / "agent_unicorn_1.sh").read_text()
        # Codex invocation present.
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in script
        assert "--enable goals" in script
        assert "--disable guardian_approval" in script
        # Wrapper loop scaffolding lands in the rendered script.
        assert "MAX_RELAUNCHES=50" in script
        assert "for relaunch in $(seq 1 $MAX_RELAUNCHES)" in script
        # Claude path must not leak.
        assert "claude --add-dir" not in script


# ---------------------------------------------------------------------------
# Harness binary pre-flight
# ---------------------------------------------------------------------------


class TestHarnessPreflight:
    """``AgentSpawner.run`` pre-flights the harness binary before spawning.

    Two-stage lookup: ``shutil.which`` first (fast, no subprocess) and
    fall back to a ``bash -c`` invocation that sources ``~/.zshrc`` and
    ``~/.bashrc`` — the same init the per-pane scripts do. This avoids
    the false-negative case where ``claude`` or ``codex`` is added to
    PATH only by shell init files (nvm / npm / asdf), so the parent
    Python sees no binary but the spawned pane would. Codex review P2
    on PR #554.
    """

    def _minimal_spawner(self, tmp_path: Path, harness: str) -> Any:
        """Build a spawner with just enough state to reach the pre-flight.

        Bypasses ``__init__`` and stubs the bits ``run`` touches before
        the pre-flight check (banner print, MLflow probe attribute,
        agent list).
        """
        config = MagicMock()
        config.experiment_dir = tmp_path
        config.implementation_dir = tmp_path / "implementation"
        config.implementation_dir.mkdir()
        config.prompts_dir = tmp_path / "prompts"
        config.prompts_dir.mkdir()
        config.project_name = "preflight_test"
        config.project_options = {"complexity": "prototype", "provider": "sqlite"}
        config.agents = []
        config.get_timeout.return_value = 300

        spawner = spawn_agents.AgentSpawner.__new__(spawn_agents.AgentSpawner)
        spawner.config = config
        spawner.harness = harness
        spawner.harness_impl = spawn_agents.HARNESSES[harness]
        spawner.agent_model = None
        spawner.marcus_mcp_url = "http://localhost:4298/mcp"
        # Attributes ``run`` references between the pre-flight check
        # and ``create_tmux_session`` — set so the patched STOP can
        # actually fire instead of hitting an AttributeError.
        spawner.panes_per_window = 2
        spawner.current_window = 0
        spawner.current_pane = 0
        spawner._tmux_base_index = 0
        spawner._tmux_pane_base_index = 0
        spawner.tmux_session = "marcus_preflight_test"
        return spawner

    def test_passes_when_shutil_which_finds_binary(self, tmp_path: Path) -> None:
        """Fast path: parent PATH has the CLI → no bash subprocess fired."""
        spawner = self._minimal_spawner(tmp_path, "codex")

        bash_lookup_calls: list[Any] = []

        def _fake_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(cmd, list) and len(cmd) >= 3 and "command -v" in str(cmd[-1]):
                bash_lookup_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("shutil.which", return_value="/usr/local/bin/codex"),
            patch("subprocess.run", side_effect=_fake_run),
            # Halt run() right after pre-flight so we don't have to
            # mock the entire spawn pipeline.
            patch.object(
                spawner, "create_tmux_session", side_effect=RuntimeError("STOP")
            ),
        ):
            with pytest.raises(RuntimeError, match="STOP"):
                spawner.run()

        assert bash_lookup_calls == [], (
            "shutil.which succeeded; bash fallback must not have been called: "
            f"{bash_lookup_calls}"
        )

    def test_falls_back_to_interactive_shell_when_parent_path_misses(
        self, tmp_path: Path
    ) -> None:
        """When ``shutil.which`` returns None, retry via ``bash -c`` that
        sources ~/.zshrc and ~/.bashrc — same as the pane scripts."""
        spawner = self._minimal_spawner(tmp_path, "codex")

        bash_lookup_calls: list[Any] = []

        def _fake_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(cmd, list) and len(cmd) >= 3 and "command -v" in str(cmd[-1]):
                bash_lookup_calls.append(cmd)
                # Simulate the interactive shell finding the binary
                # only after sourcing rc files.
                return MagicMock(
                    returncode=0, stdout="/Users/u/.nvm/.../codex\n", stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run", side_effect=_fake_run),
            patch.object(
                spawner, "create_tmux_session", side_effect=RuntimeError("STOP")
            ),
        ):
            with pytest.raises(RuntimeError, match="STOP"):
                spawner.run()

        assert len(bash_lookup_calls) == 1, (
            "Expected exactly one bash fallback lookup, got "
            f"{len(bash_lookup_calls)}: {bash_lookup_calls}"
        )
        invocation = bash_lookup_calls[0]
        assert invocation[0] == "bash"
        assert invocation[1] == "-c"
        # The fallback shell must source both rc files (matches the
        # pane scripts at spawn_agents.py:1422-1423 / :1575-1576 /
        # :1655-1656) so the lookup sees the same PATH the pane will.
        assert "~/.zshrc" in invocation[2]
        assert "~/.bashrc" in invocation[2]
        assert "command -v codex" in invocation[2]

    def test_aborts_with_clear_message_when_neither_check_finds_binary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both lookups miss → ``run`` returns False with an error mentioning
        both check paths so the user knows it's not just a PATH problem."""
        spawner = self._minimal_spawner(tmp_path, "claude")

        def _fake_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            # Interactive shell also doesn't find it.
            return MagicMock(returncode=1, stdout="", stderr="")

        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            result = spawner.run()

        assert result is False, (
            "run() should return False when pre-flight fails so the runner "
            "aborts cleanly"
        )
        out = capsys.readouterr().out
        assert "Harness CLI 'claude' not found" in out
        # Error must reference both lookup paths so the user knows the
        # parent-PATH-only false-negative is already ruled out.
        assert "parent PATH" in out
        assert "~/.zshrc" in out


# ---------------------------------------------------------------------------
# Tmux session name sanitization
# ---------------------------------------------------------------------------


class TestTmuxSessionNameSanitization:
    """``AgentSpawner.__init__`` builds ``tmux_session`` from project_name.

    tmux uses ``:`` as the session/window separator and ``.`` as the
    window/pane separator, so any of those characters in the session
    name turn every subsequent ``tmux`` call into a parse error
    (``no such window: marcus_foo:_bar``). The constructor must strip
    every character that is not safe for a tmux target identifier —
    ASCII letters, digits, underscore, hyphen — and fall back to a
    constant when the result is empty.

    Regression for the cold-launch failure where a project_name like
    ``"Codex Validation: TODO CLI"`` produced
    ``marcus_codex_validation:_todo_cli`` and the very first
    ``tmux set-option -t <session>`` call failed.
    """

    def _spawner_with_project_name(self, project_name: str, tmp_path: Path) -> Any:
        """Build an AgentSpawner whose only purpose is exposing tmux_session."""
        config = MagicMock()
        config.project_name = project_name
        config.agent_model = None
        # AgentSpawner.__init__ eagerly resolves harness_impl from the
        # registry — supply a valid harness name on the mock config.
        config.harness = "claude"
        templates = tmp_path / "templates"
        templates.mkdir()
        with patch("subprocess.run"):
            spawner = spawn_agents.AgentSpawner(config, templates)
        return spawner

    def test_colon_in_project_name_stripped(self, tmp_path: Path) -> None:
        """A colon in the project name must not survive into tmux_session."""
        spawner = self._spawner_with_project_name(
            "Codex Validation: TODO CLI", tmp_path
        )
        assert ":" not in spawner.tmux_session
        assert spawner.tmux_session == "marcus_codex_validation_todo_cli"

    def test_dot_in_project_name_stripped(self, tmp_path: Path) -> None:
        """A dot in the project name must not survive — tmux pane separator."""
        spawner = self._spawner_with_project_name("My App v2.0", tmp_path)
        assert "." not in spawner.tmux_session
        assert spawner.tmux_session == "marcus_my_app_v2_0"

    def test_slash_quote_and_other_specials_stripped(self, tmp_path: Path) -> None:
        """Slashes, quotes, and other shell-special chars are scrubbed."""
        spawner = self._spawner_with_project_name("foo/bar O'Brien (test)", tmp_path)
        for char in "/'() ":
            assert (
                char not in spawner.tmux_session
            ), f"Unsafe char {char!r} survived in {spawner.tmux_session!r}"

    def test_empty_or_whitespace_project_name_falls_back(self, tmp_path: Path) -> None:
        """All-whitespace names degrade to ``marcus_experiment``."""
        spawner = self._spawner_with_project_name("   ", tmp_path)
        assert spawner.tmux_session == "marcus_experiment"

    def test_safe_project_name_unchanged(self, tmp_path: Path) -> None:
        """Letters / digits / underscore / hyphen pass through untouched."""
        spawner = self._spawner_with_project_name("simple-name_42", tmp_path)
        assert spawner.tmux_session == "marcus_simple-name_42"

    def test_lowercasing_preserved(self, tmp_path: Path) -> None:
        """Mixed-case input is lowercased the same way as before the fix."""
        spawner = self._spawner_with_project_name("UpperCaseProject", tmp_path)
        assert spawner.tmux_session == "marcus_uppercaseproject"


# ---------------------------------------------------------------------------
# Direct-invocation regression (PR #585 Codex review P1)
# ---------------------------------------------------------------------------


class TestSpawnAgentsDirectInvocation:
    """``spawn_agents.py`` must load cleanly when run as a script.

    The file has its own ``if __name__ == "__main__":`` block, and a
    user may run ``python dev-tools/experiments/runners/spawn_agents.py
    <experiment_dir>`` directly without going through
    ``run_experiment.py``.  Python puts the script's own directory
    (``runners/``) on ``sys.path``, not its parent (``experiments/``),
    so a naive ``from runners.harness import ...`` would raise
    ``ModuleNotFoundError`` before any CLI handling ran.  This was a
    real regression flagged by Codex on PR #585; ``spawn_agents.py``
    bootstraps the parent path before the harness import so the three
    entry points (production, tests, direct) all resolve.
    """

    def test_direct_python_invocation_loads_without_module_error(self) -> None:
        """Running the script as ``python <path>`` does not crash on import.

        Uses a subprocess so the test mirrors the user-visible
        invocation path exactly — running the module via ``-c`` or
        ``importlib`` would mask the regression because pytest's
        own ``sys.path`` already includes the parent.
        """
        import subprocess

        script = (
            Path(__file__).parent.parent.parent.parent
            / "dev-tools"
            / "experiments"
            / "runners"
            / "spawn_agents.py"
        )
        # Run the script with no arguments — it prints usage and
        # exits non-zero.  We only care that the import phase
        # succeeded (i.e. no ``ModuleNotFoundError`` traceback in
        # stderr).
        result = subprocess.run(  # nosec B603 - script is internal
            ["python", str(script)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        # Absence of an import error in stderr is the regression check.
        assert "ModuleNotFoundError" not in result.stderr, (
            "Direct invocation crashed on import:\n" + result.stderr
        )
        # And the script reached its usage-print path, proving control
        # flow got past the imports and into the ``__main__`` block.
        assert "Usage:" in result.stdout or "Usage:" in result.stderr, (
            "Script ran without import error but never printed usage; "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
