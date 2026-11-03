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
    CHORE_DONE_MARKER,
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

    def test_labels_the_run_path_as_marcus(self, tmp_path: Path) -> None:
        """The cost store's runs.path says this run came from the launcher."""
        options = build_create_project_options({}, tmp_path, tmp_path / "i.json")
        assert options["path"] == "marcus"

    def test_keeps_an_explicit_run_path(self, tmp_path: Path) -> None:
        """A config that names its own entry point (posidonius) is not overridden."""
        options = build_create_project_options(
            {"path": "posidonius"}, tmp_path, tmp_path / "i.json"
        )
        assert options["path"] == "posidonius"


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
        assert CHORE_DONE_MARKER in prompt
        assert "ALREADY have a task" in prompt
        # The header points at the contract instead of restating it.
        assert "CLAUDE.md" in prompt
        assert "ADVISORY" not in prompt

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

    def test_script_resets_the_branch_only_after_a_finished_or_handed_back_chore(
        self, tmp_path: Path
    ) -> None:
        """The #730 hazard: a handed-back task's commits must not ride into main.

        When the worker left the chore-done marker, the next chore starts
        from main; when it did not (the chore ended mid-task), the branch is
        kept so the worker resumes (Codex P1 on PR #779).
        """
        script = self._script(tmp_path)
        wt = tmp_path / "worktrees" / "agent_unicorn_1"
        assert f"if [ -f {wt / CHORE_DONE_MARKER} ]; then" in script
        assert "git checkout -q -B marcus/agent_unicorn_1 main" in script
        reset_at = script.index("git checkout -q -B marcus/agent_unicorn_1 main")
        check_at = script.index(f"if [ -f {wt / CHORE_DONE_MARKER} ]; then")
        chore_at = script.index("claude --add-dir /x")
        assert check_at < reset_at < chore_at
        assert f"rm -f {wt / SESSION_DONE_MARKER} {wt / CHORE_DONE_MARKER}" in script

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


def _host(tmp_path: Path) -> Mock:
    host = Mock()
    host.harness = "claude"
    host.harness_impl = Mock(
        binary="claude", needs_pretrust_directory=False, workflow_files=("CLAUDE.md",)
    )
    host.harness_impl.install_hint = Mock(return_value=[])
    host.marcus_mcp_url = "http://127.0.0.1:4298/mcp"
    host.tmux_session = "marcus_tiny"
    host.build_mcp_register_snippet = Mock(return_value="register-snippet")
    host.build_agent_command = Mock(return_value="claude --print < prompt")
    counter = {"n": 0}

    def _window(script: Path, title: str) -> str:
        counter["n"] += 1
        return f"%{counter['n']}"

    host.start_window = Mock(side_effect=_window)
    return host


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


class TestGitSetup:
    """Worktrees need a commit to branch from; the launcher guarantees one."""

    def _launcher(self, tmp_path: Path) -> SessionLauncher:
        return SessionLauncher(
            _config(tmp_path),
            TEMPLATE.parent,
            sessions=1,
            host=_host(tmp_path),
            mcp_factory=lambda t: Mock(),
            sleep=lambda s: None,
        )

    def test_init_git_commits_when_init_left_an_unborn_head(
        self, tmp_path: Path
    ) -> None:
        """--init ran git init with no commit; worktree add would fail with 'not a valid object name'."""
        import subprocess as sp

        launcher = self._launcher(tmp_path)
        repo = launcher.config.implementation_dir
        sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        assert (
            sp.run(
                ["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True
            ).returncode
            != 0
        )

        with patch.dict(
            "os.environ",
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        ):
            launcher.init_git()

        assert (
            sp.run(
                ["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True
            ).returncode
            == 0
        )

    def test_init_git_leaves_an_existing_history_alone(self, tmp_path: Path) -> None:
        """A repo with commits is not touched."""
        import subprocess as sp

        launcher = self._launcher(tmp_path)
        repo = launcher.config.implementation_dir
        sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        sp.run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "first",
            ],
            cwd=repo,
            check=True,
        )
        before = sp.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout

        launcher.init_git()

        after = sp.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert before == after

    def test_commit_scaffold_puts_created_files_on_main(self, tmp_path: Path) -> None:
        """What create_project wrote is committed so the first worktree starts from it."""
        import subprocess as sp

        launcher = self._launcher(tmp_path)
        repo = launcher.config.implementation_dir
        sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        sp.run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "first",
            ],
            cwd=repo,
            check=True,
        )
        (repo / "docs").mkdir()
        (repo / "docs" / "design.md").write_text("design")

        with patch.dict(
            "os.environ",
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        ):
            launcher.commit_scaffold()
            launcher.commit_scaffold()  # nothing new: must not raise

        tracked = sp.run(
            ["git", "ls-files"], cwd=repo, capture_output=True, text=True
        ).stdout.split()
        assert "docs/design.md" in tracked

    def test_wait_for_scaffold_settles_once_status_is_stable(
        self, tmp_path: Path
    ) -> None:
        """Marcus writes and commits the scaffold in the background; the launcher waits for quiet."""
        import subprocess as sp

        launcher = self._launcher(tmp_path)
        repo = launcher.config.implementation_dir
        sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        sp.run(
            [
                "git",
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "first",
            ],
            cwd=repo,
            check=True,
        )

        assert launcher.wait_for_scaffold(timeout_seconds=30) is True

    def test_wait_for_scaffold_gives_up_on_an_unborn_repo(self, tmp_path: Path) -> None:
        """No commit ever arrives: the wait ends at the timeout instead of hanging."""
        import subprocess as sp

        launcher = self._launcher(tmp_path)
        repo = launcher.config.implementation_dir
        sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        clock = iter(range(0, 100000, 1))
        with patch(
            "runners.session_launcher.time.monotonic",
            side_effect=lambda: float(next(clock)) * 10,
        ):
            assert launcher.wait_for_scaffold(timeout_seconds=30) is False


class TestLauncherRun:
    """The run: preflight, git, contract files, project, sessions, wait, teardown."""

    def _launcher(
        self, tmp_path: Path, create_result: Any, **kwargs: Any
    ) -> tuple[SessionLauncher, Mock, Mock]:
        config = _config(tmp_path)
        host = _host(tmp_path)
        mcp = _mcp(create_result)
        launcher = SessionLauncher(
            config,
            TEMPLATE.parent,
            sessions=kwargs.pop("sessions", 2),
            host=host,
            mcp_factory=lambda timeout: mcp,
            sleep=lambda s: None,
            **kwargs,
        )
        return launcher, host, mcp

    def test_creates_the_project_itself_with_project_root(self, tmp_path: Path) -> None:
        """No creator agent: create_project is called over MCP with the implementation dir as root."""
        launcher, host, mcp = self._launcher(
            tmp_path, {"success": True, "project_id": "p1", "tasks_created": 3}
        )
        host.count_live_panes = Mock(return_value=0)

        with (
            patch(
                "runners.session_launcher.shutil.which", return_value="/usr/bin/claude"
            ),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ),
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
        launcher, host, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            wait=False,
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ),
        ):
            launcher.run()

        assert launcher.config.project_info_file.exists()
        assert '"project_id": "p1"' in launcher.config.project_info_file.read_text()

    def test_starts_n_sessions_in_their_worktrees(self, tmp_path: Path) -> None:
        """N sessions, each with a worktree at worktrees/<agent_id>, a prompt and a script."""
        launcher, host, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            sessions=3,
            wait=False,
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ),
        ):
            launcher.run()

        worktrees = [c.args[2] for c in host.create_worktree.call_args_list]
        assert [w.name for w in worktrees] == [
            "agent_unicorn_1",
            "agent_unicorn_2",
            "session_3",
        ]
        assert all(
            w.parent == launcher.config.experiment_dir / "worktrees" for w in worktrees
        )
        assert host.start_window.call_count == 3
        assert (launcher.config.prompts_dir / "session_3.sh").exists()
        assert (
            "ONE CHORE" in (launcher.config.prompts_dir / "session_3.txt").read_text()
        )
        # chore command is the single-shot form
        assert host.build_agent_command.call_args.kwargs["print_mode"] is True

    def test_instruction_files_carry_the_canonical_contract(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md, AGENTS.md and GEMINI.md are the one worker contract.

        The per-chore prompt is only a header that points at these files, so
        what a worker reads in its cwd is exactly ``prompts/Agent_prompt.md``.
        """
        contract = tmp_path / "Agent_prompt.md"
        contract.write_text("WORKER_SYSTEM_PROMPT: |\n  the one contract\n")
        launcher, host, mcp = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            contract_path=contract,
        )
        host.count_live_panes = Mock(return_value=0)
        host.harness_impl.workflow_files = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

        written = launcher.write_instruction_files()

        names = sorted(p.name for p in written)
        assert names == ["AGENTS.md", "CLAUDE.md", "GEMINI.md"]
        for path in written:
            assert path.read_text() == contract.read_text()

    def test_default_contract_is_the_repo_prompt(self, tmp_path: Path) -> None:
        """Without an override the launcher writes prompts/Agent_prompt.md."""
        from runners.session_launcher import CANONICAL_PROMPT

        launcher, _, _ = self._launcher(tmp_path, {"success": True})
        assert launcher.contract_path == CANONICAL_PROMPT
        assert CANONICAL_PROMPT.name == "Agent_prompt.md"
        assert CANONICAL_PROMPT.is_file()

    def test_waits_for_all_sessions_then_tears_down(self, tmp_path: Path) -> None:
        """No restarts: the launcher polls liveness until zero, then kills tmux."""
        launcher, host, _ = self._launcher(
            tmp_path, {"success": True, "project_id": "p1", "tasks_created": 3}
        )
        host.count_live_panes = Mock(side_effect=[2, 1, 0])

        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ) as run,
        ):
            assert launcher.run() is True

        assert host.count_live_panes.call_count == 3
        assert host.start_window.call_count == 2  # never more than N
        host.kill_session.assert_called_once()

    def test_epictetus_keeps_tmux_alive(self, tmp_path: Path) -> None:
        """Inspection mode leaves the session for the operator."""
        launcher, host, _ = self._launcher(
            tmp_path,
            {"success": True, "project_id": "p1", "tasks_created": 3},
            epictetus=True,
        )
        host.count_live_panes = Mock(return_value=0)
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ) as run,
        ):
            launcher.run()
        host.kill_session.assert_not_called()

    def test_failed_project_creation_starts_no_sessions(self, tmp_path: Path) -> None:
        """A rejected create_project (for example project_root_required) ends the run."""
        launcher, host, _ = self._launcher(
            tmp_path,
            {"success": False, "error": "nope", "error_code": "project_root_required"},
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ),
        ):
            assert launcher.run() is False
        host.start_window.assert_not_called()

    def test_preflight_failures_stop_before_any_side_effect(
        self, tmp_path: Path
    ) -> None:
        """No harness binary or no Marcus: nothing is created."""
        launcher, host, mcp = self._launcher(
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
        host.create_tmux_session.assert_not_called()


class TestRunnerLog:
    """The launcher mirrors its progress lines to ``logs/runner.log``."""

    def test_say_prints_and_appends_to_runner_log(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A progress line reaches both stdout and the log file."""
        launcher = SessionLauncher(
            _config(tmp_path),
            TEMPLATE.parent,
            sessions=1,
            host=_host(tmp_path),
            mcp_factory=lambda t: Mock(),
        )

        launcher.say("hello operator")

        assert "hello operator" in capsys.readouterr().out
        log = Path(launcher.config.experiment_dir) / "logs" / "runner.log"
        assert "hello operator" in log.read_text()

    def test_run_writes_the_log_and_names_it(self, tmp_path: Path) -> None:
        """A full run leaves a runner.log that says how to follow it."""
        config = _config(tmp_path)
        host = _host(tmp_path)
        host.count_live_panes = Mock(return_value=0)
        launcher = SessionLauncher(
            config,
            TEMPLATE.parent,
            sessions=1,
            host=host,
            mcp_factory=lambda t: _mcp(
                {"success": True, "project_id": "p1", "tasks_created": 1}
            ),
            sleep=lambda s: None,
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ),
        ):
            assert launcher.run() is True
        text = (Path(config.experiment_dir) / "logs" / "runner.log").read_text()
        assert "started" in text
        assert "tail -f" in text
        assert "gone home" in text


class TestCreateProjectTimeout:
    """The MCP timeout for create_project honours the config's timeouts block."""

    def test_uses_config_timeout_when_present(self, tmp_path: Path) -> None:
        """``timeouts.project_creation`` is the client timeout."""
        config = _config(tmp_path)
        config.get_timeout = lambda key, default: (
            1234 if key == "project_creation" else default
        )
        seen: List[float] = []

        def factory(timeout: float) -> Mock:
            seen.append(timeout)
            return _mcp({"success": True, "project_id": "p1", "tasks_created": 1})

        launcher = SessionLauncher(
            config,
            TEMPLATE.parent,
            sessions=1,
            host=_host(tmp_path),
            mcp_factory=factory,
        )
        assert launcher.create_project() is True
        assert seen[0] == 1234.0

    def test_falls_back_to_default_without_get_timeout(self, tmp_path: Path) -> None:
        """A config stand-in without timeouts gets the built-in default."""
        from runners.session_launcher import CREATE_PROJECT_TIMEOUT_SECONDS

        seen: List[float] = []

        def factory(timeout: float) -> Mock:
            seen.append(timeout)
            return _mcp({"success": True, "project_id": "p1", "tasks_created": 1})

        launcher = SessionLauncher(
            _config(tmp_path),
            TEMPLATE.parent,
            sessions=1,
            host=_host(tmp_path),
            mcp_factory=factory,
        )
        assert launcher.create_project() is True
        assert seen[0] == CREATE_PROJECT_TIMEOUT_SECONDS


class TestUnfinishedReport:
    """The end-of-run statement of what did and did not get built."""

    def test_clean_run_says_all_complete(self) -> None:
        """No unfinished tasks: one line, with the count."""
        from runners.session_launcher import format_unfinished_report

        lines = format_unfinished_report([], total=9)
        assert len(lines) == 1
        assert "9/9" in lines[0]
        assert "complete" in lines[0].lower()

    def test_unfinished_work_is_named_with_reasons(self) -> None:
        """Every unfinished task is named with its status and reason."""
        from runners.session_launcher import format_unfinished_report

        text = "\n".join(
            format_unfinished_report(
                [
                    {
                        "id": "abc123",
                        "name": "Implement CLI Entry Point",
                        "status": "blocked",
                        "reason": "needs a paid API key",
                    },
                    {
                        "id": "def456",
                        "name": "Write README",
                        "status": "todo",
                        "reason": "never claimed by an agent",
                    },
                ],
                total=10,
            )
        )
        assert "8/10" in text
        assert "2 task(s) did NOT complete" in text
        assert "Implement CLI Entry Point" in text and "needs a paid API key" in text
        assert "Write README" in text and "never claimed by an agent" in text
        assert "missing" in text.lower()

    def test_absent_list_says_it_could_not_verify(self) -> None:
        """No trustworthy list must never read as success."""
        from runners.session_launcher import format_unfinished_report

        text = "\n".join(format_unfinished_report(None, total=9)).lower()
        assert "could not" in text
        assert "complete" not in text.split("could not")[0]

    def test_zero_total_does_not_claim_all_complete(self) -> None:
        """A board that reports no tasks is not a finished board."""
        from runners.session_launcher import format_unfinished_report

        text = "\n".join(format_unfinished_report([], total=0)).lower()
        assert "all 0/0" not in text
        assert "could not" in text


class TestFinalReport:
    """The launcher reads the board once at the end and names the gaps."""

    def _run(self, tmp_path: Path, project: Dict[str, Any]) -> str:
        config = _config(tmp_path)
        host = _host(tmp_path)
        host.count_live_panes = Mock(return_value=0)
        mcp = _mcp({"success": True, "project_id": "p1", "tasks_created": 3})

        def _call(name: str, arguments: Any = None) -> Any:
            if name == "create_project":
                return {"success": True, "project_id": "p1", "tasks_created": 3}
            if name == "get_project_status":
                return {"success": True, "project": project}
            return {}

        mcp.call_tool = Mock(side_effect=_call)
        launcher = SessionLauncher(
            config,
            TEMPLATE.parent,
            sessions=1,
            host=host,
            mcp_factory=lambda t: mcp,
            sleep=lambda s: None,
        )
        with (
            patch("runners.session_launcher.shutil.which", return_value="/x"),
            patch("runners.session_launcher.check_mcp_health", return_value=True),
            patch(
                "runners.session_launcher.subprocess.run",
                return_value=Mock(returncode=0, stdout=""),
            ),
        ):
            assert launcher.run() is True
        return (Path(config.experiment_dir) / "logs" / "runner.log").read_text()

    def test_names_unfinished_tasks_from_project_status(self, tmp_path: Path) -> None:
        """The RESULT lines come from get_project_status's unfinished_tasks."""
        log = self._run(
            tmp_path,
            {
                "total_tasks": 3,
                "completed": 2,
                "unfinished_tasks": [
                    {
                        "id": "deadbeef",
                        "name": "Write README",
                        "status": "todo",
                        "reason": "never claimed by an agent",
                    }
                ],
            },
        )
        assert "RESULT: 2/3 tasks complete" in log
        assert "Write README" in log
        assert "never claimed by an agent" in log

    def test_says_could_not_verify_without_the_list(self, tmp_path: Path) -> None:
        """A status without the list is reported as unverified, not as done."""
        log = self._run(tmp_path, {"total_tasks": 3, "completed": 3})
        assert "could not verify" in log.lower()
        assert "RESULT: all" not in log
