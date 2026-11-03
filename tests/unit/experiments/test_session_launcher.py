"""
Unit tests for the session launcher (epic #706 Milestone A, launcher design piece 1).

The launcher starts N persistent worker sessions and gets out of the way:
it creates the project itself over MCP, gives each session a worktree at
``worktrees/<agent_id>`` (the path shape Cato reads), runs the harness one
chore at a time until the worker leaves the drained marker, never restarts
a dead worker, waits for all of them to go home, and tears tmux down.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, Mock, patch

import pytest
from runners.session_launcher import (
    DEFAULT_SESSION_ROLE,
    SESSION_DONE_MARKER,
    SessionLauncher,
    build_create_project_options,
    build_session_script,
    plan_sessions,
    render_session_prompt,
)

pytestmark = pytest.mark.unit

TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "dev-tools"
    / "experiments"
    / "templates"
    / "session_prompt.md"
)


def _agents(n: int) -> List[Dict[str, Any]]:
    return [
        {
            "id": f"agent_unicorn_{i}",
            "name": f"Unicorn {i}",
            "role": "full-stack",
            "skills": ["python"],
            "subagents": 2,
        }
        for i in range(1, n + 1)
    ]


class TestPlanSessions:
    """Identities come from config first, then are generated."""

    def test_uses_config_agents_in_order_without_subagents(self) -> None:
        """The first N config agents are used as written; subagents are dropped."""
        planned = plan_sessions(_agents(3), 2)
        assert [p["id"] for p in planned] == ["agent_unicorn_1", "agent_unicorn_2"]
        assert all("subagents" not in p for p in planned)

    def test_pads_with_generated_sessions(self) -> None:
        """Fewer config agents than sessions: the rest are session_<i>."""
        planned = plan_sessions(_agents(1), 3)
        assert [p["id"] for p in planned] == [
            "agent_unicorn_1",
            "session_2",
            "session_3",
        ]
        assert planned[2]["role"] == DEFAULT_SESSION_ROLE
        assert planned[2]["skills"]

    def test_empty_config_still_plans(self) -> None:
        """No agents in config at all is fine."""
        assert [p["id"] for p in plan_sessions([], 2)] == ["session_1", "session_2"]

    def test_zero_sessions_is_an_error(self) -> None:
        """A run needs at least one worker."""
        with pytest.raises(ValueError):
            plan_sessions(_agents(1), 0)


class TestCreateProjectOptions:
    """The launcher owns project_root and the info-file path."""

    def test_sets_root_and_info_path_and_keeps_provider(self, tmp_path: Path) -> None:
        """project_root is the implementation dir; config options pass through."""
        options = build_create_project_options(
            {"provider": "sqlite", "complexity": "prototype"},
            tmp_path / "implementation",
            tmp_path / "project_info.json",
        )
        assert options["project_root"] == str(tmp_path / "implementation")
        assert options["project_info_path"] == str(tmp_path / "project_info.json")
        assert options["complexity"] == "prototype"
        assert options["provider"] == "sqlite"

    def test_defaults_provider_to_sqlite(self, tmp_path: Path) -> None:
        """No provider in config means sqlite."""
        assert (
            build_create_project_options({}, tmp_path, tmp_path / "i.json")["provider"]
            == "sqlite"
        )


class TestSessionPrompt:
    """The contract a worker is handed on every chore."""

    def test_prompt_carries_identity_and_the_session_rules(
        self, tmp_path: Path
    ) -> None:
        """Identity, one chore per invocation, drained marker, idempotent register."""
        agent = plan_sessions(_agents(1), 1)[0]
        prompt = render_session_prompt(
            agent, tmp_path / "wt", "marcus/agent_unicorn_1", TEMPLATE.read_text()
        )

        assert "agent_unicorn_1" in prompt and "Unicorn 1" in prompt
        assert str(tmp_path / "wt") in prompt
        assert "ONE CHORE" in prompt
        assert "register_agent" in prompt
        assert "BOARD_DRAINED" in prompt
        assert SESSION_DONE_MARKER in prompt
        assert "retry_after_seconds" in prompt
        assert "lease_epoch" in prompt
        assert "smoke_gate_unavailable" in prompt

    def test_prompt_never_asks_the_worker_to_loop_or_self_declare_completion(
        self, tmp_path: Path
    ) -> None:
        """The loop is outside the worker; only BOARD_DRAINED ends the session."""
        agent = plan_sessions([], 1)[0]
        text = " ".join(
            render_session_prompt(
                agent, tmp_path, "marcus/session_1", TEMPLATE.read_text()
            )
            .lower()
            .split()
        )
        assert "do not loop inside this invocation" in text
        assert "only board_drained ends your session" in text
        assert "total_tasks == completed" not in text


class TestSessionScript:
    """The shell loop around single-shot harness invocations."""

    def _script(self, tmp_path: Path, **overrides: Any) -> str:
        agent = plan_sessions(_agents(1), 1)[0]
        kwargs: Dict[str, Any] = dict(
            agent=agent,
            workspace=tmp_path / "worktrees" / "agent_unicorn_1",
            branch="marcus/agent_unicorn_1",
            project_info_file=tmp_path / "project_info.json",
            marcus_mcp_url="http://127.0.0.1:4298/mcp",
            register_snippet="claude mcp add --transport http marcus $MARCUS_MCP_URL || true",
            chore_command="claude --add-dir /x --dangerously-skip-permissions --print < /p.txt",
            max_chores=7,
        )
        kwargs.update(overrides)
        return build_session_script(**kwargs)

    def test_script_keeps_the_worktree_path_shape(self, tmp_path: Path) -> None:
        """Cato reads worktrees/<agent_id>; the script works in exactly that directory."""
        script = self._script(tmp_path)
        assert f"cd {tmp_path / 'worktrees' / 'agent_unicorn_1'}" in script

    def test_script_loops_one_chore_at_a_time_until_the_marker(
        self, tmp_path: Path
    ) -> None:
        """Each iteration syncs main, runs the harness once, then checks the marker."""
        script = self._script(tmp_path)
        assert "while true; do" in script
        assert "git merge main --no-edit" in script
        assert "--print" in script
        assert (
            f"if [ -f {tmp_path / 'worktrees' / 'agent_unicorn_1' / SESSION_DONE_MARKER} ]"
            in script
        )
        assert "board drained, going home" in script

    def test_script_has_a_chore_cap_and_no_restart_logic(self, tmp_path: Path) -> None:
        """A misbehaving prompt stops at the cap; nothing resurrects a dead worker."""
        script = self._script(tmp_path, max_chores=7)
        assert "-ge 7" in script
        assert "relaunch" not in script.lower()

    def test_script_sets_git_identity_and_clears_a_stale_marker(
        self, tmp_path: Path
    ) -> None:
        """Commits are attributed to the session; a marker from a previous run is removed first."""
        script = self._script(tmp_path)
        assert 'GIT_AUTHOR_EMAIL="agent_unicorn_1@marcus.ai"' in script
        assert (
            f"rm -f {tmp_path / 'worktrees' / 'agent_unicorn_1' / SESSION_DONE_MARKER}"
            in script
        )


def _config(tmp_path: Path, agents: int = 2) -> SimpleNamespace:
    experiment = tmp_path / "exp"
    (experiment / "implementation").mkdir(parents=True)
    (experiment / "prompts").mkdir()
    spec = experiment / "project_spec.md"
    spec.write_text("Build a tiny app that prints 2.")
    return SimpleNamespace(
        project_name="tiny",
        project_spec_file=spec,
        project_options={"provider": "sqlite", "complexity": "prototype"},
        agents=_agents(agents),
        experiment_dir=experiment,
        implementation_dir=experiment / "implementation",
        prompts_dir=experiment / "prompts",
        project_info_file=experiment / "project_info.json",
    )


def _spawner(tmp_path: Path) -> Mock:
    spawner = Mock()
    spawner.harness = "claude"
    spawner.harness_impl = Mock(
        binary="claude", needs_pretrust_directory=False, workflow_files=("CLAUDE.md",)
    )
    spawner.harness_impl.install_hint = Mock(return_value=[])
    spawner.marcus_mcp_url = "http://127.0.0.1:4298/mcp"
    spawner.tmux_session = "marcus_tiny"
    spawner._build_mcp_register_snippet = Mock(return_value="register-snippet")
    spawner._build_agent_command = Mock(return_value="claude --print < prompt")
    counter = {"n": 0}

    def _window(script: Path, title: str) -> str:
        counter["n"] += 1
        return f"%{counter['n']}"

    spawner.run_worker_in_new_window = Mock(side_effect=_window)
    return spawner


def _mcp(create_result: Any) -> Mock:
    mcp = Mock()
    mcp.connect = Mock(return_value=True)

    def _call(name: str, arguments: Any = None) -> Any:
        if name == "create_project":
            return create_result
        if name == "get_project_status":
            return {"project": {"total_tasks": 3, "completed": 3}}
        return {}

    mcp.call_tool = Mock(side_effect=_call)
    return mcp


class TestLauncherRun:
    """The run: preflight, git, contract files, project, sessions, wait, teardown."""

    def _launcher(
        self, tmp_path: Path, create_result: Any, **kwargs: Any
    ) -> tuple[SessionLauncher, Mock, Mock]:
        config = _config(tmp_path)
        spawner = _spawner(tmp_path)
        mcp = _mcp(create_result)
        launcher = SessionLauncher(
            config,
            TEMPLATE.parent,
            sessions=kwargs.pop("sessions", 2),
            spawner=spawner,
            mcp_factory=lambda timeout: mcp,
            sleep=lambda s: None,
            **kwargs,
        )
        return launcher, spawner, mcp

    def test_creates_the_project_itself_with_project_root(self, tmp_path: Path) -> None:
        """No creator agent: create_project is called over MCP with the implementation dir as root."""
        launcher, spawner, mcp = self._launcher(
            tmp_path, {"success": True, "project_id": "p1", "tasks_created": 3}
        )
        spawner._count_live_worker_panes = Mock(return_value=0)

        with (
            patch(
                "runners.session_launcher.shutil.which", return_value="/usr/bin/claude"
            ),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch("runners.session_launcher.subprocess.run"),
        ):
            assert launcher.run() is True

        create_call = next(
            c for c in mcp.call_tool.call_args_list if c.args[0] == "create_project"
        )
        options = create_call.args[1]["options"]
        assert options["project_root"] == str(launcher.config.implementation_dir)
        assert options["project_info_path"] == str(launcher.config.project_info_file)
        assert create_call.args[1]["project_name"] == "tiny"
        assert "prints 2" in create_call.args[1]["description"]
        assert not any(
            c.args[0] == "start_experiment" for c in mcp.call_tool.call_args_list
        )

    def test_writes_project_info_when_marcus_did_not(self, tmp_path: Path) -> None:
        """Workers wait on project_info.json; the launcher guarantees it exists."""
        launcher, spawner, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            wait=False,
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch("runners.session_launcher.subprocess.run"),
        ):
            launcher.run()

        assert launcher.config.project_info_file.exists()
        assert '"project_id": "p1"' in launcher.config.project_info_file.read_text()

    def test_starts_n_sessions_in_their_worktrees(self, tmp_path: Path) -> None:
        """N sessions, each with a worktree at worktrees/<agent_id>, a prompt and a script."""
        launcher, spawner, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            sessions=3,
            wait=False,
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch("runners.session_launcher.subprocess.run"),
        ):
            launcher.run()

        worktrees = [c.args[3] for c in spawner._create_agent_worktree.call_args_list]
        assert [w.name for w in worktrees] == [
            "agent_unicorn_1",
            "agent_unicorn_2",
            "session_3",
        ]
        assert all(
            w.parent == launcher.config.experiment_dir / "worktrees" for w in worktrees
        )
        assert spawner.run_worker_in_new_window.call_count == 3
        assert (launcher.config.prompts_dir / "session_3.sh").exists()
        assert (
            "ONE CHORE" in (launcher.config.prompts_dir / "session_3.txt").read_text()
        )
        # chore command is the single-shot form
        assert spawner._build_agent_command.call_args.kwargs["print_mode"] is True

    def test_instruction_files_carry_the_session_contract(self, tmp_path: Path) -> None:
        """CLAUDE.md (and the other harness files) say what the prompt says."""
        launcher, _, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            wait=False,
        )
        written = launcher.write_instruction_files()

        names = {p.name for p in written}
        assert {"CLAUDE.md", "AGENTS.md", "GEMINI.md"} <= names
        assert (
            "ONE CHORE"
            in (launcher.config.implementation_dir / "AGENTS.md").read_text()
        )

    def test_waits_for_all_sessions_then_tears_down(self, tmp_path: Path) -> None:
        """No restarts: the launcher polls liveness until zero, then kills tmux."""
        launcher, spawner, _ = self._launcher(
            tmp_path, {"success": True, "project_id": "p1", "tasks_created": 3}
        )
        spawner._count_live_worker_panes = Mock(side_effect=[2, 1, 0])

        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch("runners.session_launcher.subprocess.run") as run,
        ):
            assert launcher.run() is True

        assert spawner._count_live_worker_panes.call_count == 3
        assert spawner.run_worker_in_new_window.call_count == 2  # never more than N
        kill = [
            c
            for c in run.call_args_list
            if c.args and c.args[0][:2] == ["tmux", "kill-session"]
        ]
        assert len(kill) == 1

    def test_epictetus_keeps_tmux_alive(self, tmp_path: Path) -> None:
        """Inspection mode leaves the session for the operator."""
        launcher, spawner, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            epictetus=True,
        )
        spawner._count_live_worker_panes = Mock(return_value=0)
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch("runners.session_launcher.subprocess.run") as run,
        ):
            launcher.run()
        assert not [
            c
            for c in run.call_args_list
            if c.args and c.args[0][:2] == ["tmux", "kill-session"]
        ]

    def test_failed_project_creation_starts_no_sessions(self, tmp_path: Path) -> None:
        """A rejected create_project (for example project_root_required) ends the run."""
        launcher, spawner, _ = self._launcher(
            tmp_path,
            {"success": False, "error": "nope", "error_code": "project_root_required"},
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch("runners.session_launcher.subprocess.run"),
        ):
            assert launcher.run() is False
        spawner.run_worker_in_new_window.assert_not_called()

    def test_preflight_failures_stop_before_any_side_effect(
        self, tmp_path: Path
    ) -> None:
        """No harness binary or no Marcus: nothing is created."""
        launcher, spawner, mcp = self._launcher(
            tmp_path, {"success": True, "project_id": "p1", "tasks_created": 3}
        )
        with patch("runners.session_launcher.shutil.which", return_value=None):
            assert launcher.run() is False
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=False),
        ):
            assert launcher.run() is False
        mcp.call_tool.assert_not_called()
        spawner.create_tmux_session.assert_not_called()
