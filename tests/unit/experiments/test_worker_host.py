"""
Unit tests for ``runners/worker_host.py`` (epic #706 Milestone A).

The worker host is what survived the deletion of the spawn-per-task runner:
the experiment config, the tmux session, one window per session, the
per-session git worktree, the harness command, and the Claude trust-file
seeding. It starts nothing on its own and has no loop; the session
launcher drives it.
"""

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml
from runners import worker_host

pytestmark = pytest.mark.unit


def _write_config(tmp_path: Path, **extra: Any) -> Path:
    """Write a minimal config.yaml and return its path."""
    (tmp_path / "project_spec.md").write_text("test spec")
    data: Dict[str, Any] = {
        "project_name": "host-test",
        "project_spec_file": "project_spec.md",
        "project_options": {"complexity": "prototype", "provider": "sqlite"},
        "agents": [
            {"id": "agent_1", "name": "Agent 1", "role": "full-stack", "skills": []},
            {"id": "agent_2", "name": "Agent 2", "role": "full-stack", "skills": []},
        ],
    }
    data.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    return path


def _isolate_marcus_config(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point the module's ``__file__`` at a fake repo root under tmp_path.

    ``ExperimentConfig`` finds ``config_marcus.json`` four parents above the
    module file, so the fake must sit at the same depth.
    """
    fake_root = tmp_path / "marcus_root"
    fake_module = fake_root / "dev-tools" / "experiments" / "runners" / "worker_host.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("")
    monkeypatch.setattr(worker_host, "__file__", str(fake_module))
    monkeypatch.delenv("MARCUS_CONFIG", raising=False)
    return fake_root


class TestWaitForPaneReady:
    """Polling a pane for a shell prompt before sending keys."""

    def test_returns_true_when_shell_prompt_detected(self) -> None:
        """A trailing prompt character means the shell is ready."""
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="user@host ~ $")
            assert worker_host.wait_for_pane_ready("t:0.0", timeout=2.0) is True

    def test_returns_true_on_content_stabilization(self) -> None:
        """Unchanging content for two polls also counts as ready."""
        calls = {"n": 0}

        def capture(*args: Any, **kwargs: Any) -> MagicMock:
            calls["n"] += 1
            if calls["n"] <= 1:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="Loading shell...")

        with patch("subprocess.run", side_effect=capture), patch("time.sleep"):
            assert (
                worker_host.wait_for_pane_ready(
                    "t:0.0", timeout=5.0, poll_interval=0.01
                )
                is True
            )

    def test_returns_false_on_timeout(self) -> None:
        """Content that never stabilises times out."""
        calls = {"n": 0}

        def capture(*args: Any, **kwargs: Any) -> MagicMock:
            calls["n"] += 1
            return MagicMock(returncode=0, stdout=f"changing {calls['n']}")

        with patch("subprocess.run", side_effect=capture), patch("time.sleep"):
            assert (
                worker_host.wait_for_pane_ready(
                    "t:0.0", timeout=0.01, poll_interval=0.001
                )
                is False
            )

    def test_handles_tmux_capture_failure(self) -> None:
        """A failing capture-pane is not ready and does not raise."""
        with patch("subprocess.run") as run, patch("time.sleep"):
            run.return_value = MagicMock(returncode=1, stdout="")
            assert (
                worker_host.wait_for_pane_ready(
                    "t:0.0", timeout=0.01, poll_interval=0.001
                )
                is False
            )


class TestMCPHealthCheck:
    """The pre-flight probe of the Marcus endpoint."""

    def test_returns_true_when_healthy(self) -> None:
        """curl exit 0 means reachable."""
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="200")
            assert worker_host.check_mcp_health("http://localhost:4298") is True

    def test_returns_false_when_down(self) -> None:
        """A non-zero curl exit means unreachable."""
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=7)
            assert worker_host.check_mcp_health("http://localhost:4298") is False

    def test_returns_false_when_curl_missing(self) -> None:
        """No curl binary is reported as unreachable, not as a crash."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert worker_host.check_mcp_health("http://localhost:4298") is False


class TestPretrustDirectory:
    """Seeding ``~/.claude.json`` so Claude skips the trust dialog."""

    def test_creates_config_when_missing(self, tmp_path: Path) -> None:
        """A missing file is created with the directory trusted."""
        impl = tmp_path / "implementation"
        impl.mkdir()
        with patch.object(Path, "home", return_value=tmp_path):
            worker_host.WorkerHost.pretrust_directory(impl)
        config = json.loads((tmp_path / ".claude.json").read_text())
        assert config["projects"][str(impl)]["hasTrustDialogAccepted"] is True

    def test_preserves_existing_entries(self, tmp_path: Path) -> None:
        """Other projects and top-level keys survive the write."""
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
            json.dumps(
                {
                    "numStartups": 42,
                    "projects": {"/other": {"hasTrustDialogAccepted": True}},
                }
            )
        )
        impl = tmp_path / "implementation"
        impl.mkdir()
        with patch.object(Path, "home", return_value=tmp_path):
            worker_host.WorkerHost.pretrust_directory(impl)
        config = json.loads(claude_json.read_text())
        assert config["numStartups"] == 42
        assert "/other" in config["projects"]
        assert config["projects"][str(impl)]["hasTrustDialogAccepted"] is True

    def test_flips_false_flag_and_keeps_fields(self, tmp_path: Path) -> None:
        """An untrusted entry is trusted without losing its other fields."""
        impl = tmp_path / "implementation"
        impl.mkdir()
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
            json.dumps(
                {
                    "projects": {
                        str(impl): {
                            "hasTrustDialogAccepted": False,
                            "allowedTools": ["Bash"],
                        }
                    }
                }
            )
        )
        with patch.object(Path, "home", return_value=tmp_path):
            worker_host.WorkerHost.pretrust_directory(impl)
        entry = json.loads(claude_json.read_text())["projects"][str(impl)]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["allowedTools"] == ["Bash"]

    def test_already_trusted_is_not_rewritten(self, tmp_path: Path) -> None:
        """A trusted directory leaves the file untouched."""
        impl = tmp_path / "implementation"
        impl.mkdir()
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
            json.dumps({"projects": {str(impl): {"hasTrustDialogAccepted": True}}})
        )
        before = claude_json.stat().st_mtime
        with patch.object(Path, "home", return_value=tmp_path):
            worker_host.WorkerHost.pretrust_directory(impl)
        assert claude_json.stat().st_mtime == before

    def test_malformed_json_is_reported_not_raised(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corrupt trust file prints a warning instead of crashing the run."""
        (tmp_path / ".claude.json").write_text("{bad json")
        impl = tmp_path / "implementation"
        impl.mkdir()
        with patch.object(Path, "home", return_value=tmp_path):
            worker_host.WorkerHost.pretrust_directory(impl)
        assert "Could not pre-trust" in capsys.readouterr().out


class TestExperimentConfig:
    """``config.yaml`` as the launcher reads it."""

    def test_project_info_file_defaults_to_experiment_dir(self, tmp_path: Path) -> None:
        """Without an override the info file sits beside config.yaml."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        assert config.project_info_file == tmp_path / "project_info.json"
        assert config.project_options["project_info_path"] == str(
            tmp_path / "project_info.json"
        )

    def test_project_info_file_follows_custom_override(self, tmp_path: Path) -> None:
        """A pre-set project_info_path is the file the launcher waits for."""
        custom = tmp_path / "custom" / "info.json"
        config = worker_host.ExperimentConfig(
            _write_config(
                tmp_path,
                project_options={
                    "provider": "sqlite",
                    "project_info_path": str(custom),
                },
            )
        )
        assert config.project_info_file == custom

    def test_project_root_defaults_to_implementation_dir(self, tmp_path: Path) -> None:
        """project_root is filled in so create_project can run the gate."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        assert config.project_options["project_root"] == str(
            tmp_path / "implementation"
        )

    def test_sessions_default_to_agent_count(self, tmp_path: Path) -> None:
        """No ``sessions`` key: one session per listed agent."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        assert config.sessions == 2

    def test_sessions_key_wins_over_agent_count(self, tmp_path: Path) -> None:
        """An explicit ``sessions`` key is the session count."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path, sessions=3))
        assert config.sessions == 3

    def test_sessions_default_is_one_when_no_agents(self, tmp_path: Path) -> None:
        """An empty agents list still yields a runnable count."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path, agents=[]))
        assert config.sessions == 1

    def test_sessions_below_one_is_rejected(self, tmp_path: Path) -> None:
        """``sessions: 0`` is a config error, caught at load time."""
        with pytest.raises(ValueError, match="sessions"):
            worker_host.ExperimentConfig(_write_config(tmp_path, sessions=0))

    def test_harness_defaults_to_claude(self, tmp_path: Path) -> None:
        """No harness key means claude."""
        assert worker_host.ExperimentConfig(_write_config(tmp_path)).harness == "claude"

    def test_harness_is_normalised(self, tmp_path: Path) -> None:
        """Case quirks in the yaml are tolerated."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path, harness="Codex"))
        assert config.harness == "codex"

    def test_unknown_harness_fails_at_load(self, tmp_path: Path) -> None:
        """A bad harness name surfaces before tmux or git are touched."""
        with pytest.raises(ValueError, match="bedrock"):
            worker_host.ExperimentConfig(_write_config(tmp_path, harness="bedrock"))

    def test_spawn_loop_keys_are_gone(self, tmp_path: Path) -> None:
        """The stall watchdog and thrash detector settings no longer exist."""
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        for name in ("stall_timeout_minutes", "spawn_thrash_polls", "cpm_override"):
            assert not hasattr(config, name), name


class TestAgentModelResolution:
    """``agent_model``: yaml, then the Planner's config_marcus.json, then None."""

    def test_yaml_wins_over_marcus_config(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A yaml value short-circuits the fallback."""
        root = _isolate_marcus_config(tmp_path, monkeypatch)
        (root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "should-not-be-used"}})
        )
        config = worker_host.ExperimentConfig(
            _write_config(tmp_path, agent_model="from-yaml")
        )
        assert config.agent_model == "from-yaml"

    def test_falls_back_to_marcus_config(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Yaml silent: the Planner's model is reused."""
        root = _isolate_marcus_config(tmp_path, monkeypatch)
        (root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "claude-haiku-4-5-20251001"}})
        )
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        assert config.agent_model == "claude-haiku-4-5-20251001"

    def test_none_when_neither_source_has_a_model(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """No yaml value and no config file means no --model flag."""
        _isolate_marcus_config(tmp_path, monkeypatch)
        assert worker_host.ExperimentConfig(_write_config(tmp_path)).agent_model is None

    def test_malformed_marcus_config_is_absent(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A broken Planner config must not break the launcher."""
        root = _isolate_marcus_config(tmp_path, monkeypatch)
        (root / "config_marcus.json").write_text("not-valid-json{")
        assert worker_host.ExperimentConfig(_write_config(tmp_path)).agent_model is None

    def test_env_var_redirects_resolver(self, tmp_path: Path, monkeypatch: Any) -> None:
        """MARCUS_CONFIG is honoured, matching the Planner's own lookup."""
        root = _isolate_marcus_config(tmp_path, monkeypatch)
        (root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "repo-root-should-be-ignored"}})
        )
        custom = tmp_path / "custom_marcus.json"
        custom.write_text(json.dumps({"ai": {"model": "from-env-var-config"}}))
        monkeypatch.setenv("MARCUS_CONFIG", str(custom))
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        assert config.agent_model == "from-env-var-config"

    def test_env_var_missing_file_is_none(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A set but wrong MARCUS_CONFIG does not fall back to the repo file."""
        root = _isolate_marcus_config(tmp_path, monkeypatch)
        (root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "must-not-be-used-when-env-set"}})
        )
        monkeypatch.setenv("MARCUS_CONFIG", str(tmp_path / "missing.json"))
        assert worker_host.ExperimentConfig(_write_config(tmp_path)).agent_model is None

    def test_empty_env_var_falls_back_to_repo_root(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """An empty MARCUS_CONFIG is treated as unset."""
        root = _isolate_marcus_config(tmp_path, monkeypatch)
        (root / "config_marcus.json").write_text(
            json.dumps({"ai": {"model": "from-repo-root"}})
        )
        monkeypatch.setenv("MARCUS_CONFIG", "")
        config = worker_host.ExperimentConfig(_write_config(tmp_path))
        assert config.agent_model == "from-repo-root"


def _host(tmp_path: Path, monkeypatch: Any, **kwargs: Any) -> worker_host.WorkerHost:
    """Build a real host from a real config with no tmux calls."""
    _isolate_marcus_config(tmp_path, monkeypatch)
    extra = {k: v for k, v in kwargs.items() if k in {"agent_model", "harness"}}
    config = worker_host.ExperimentConfig(_write_config(tmp_path, **extra))
    with patch("subprocess.run") as run:
        host = worker_host.WorkerHost(
            config,
            agent_model=kwargs.get("cli_model"),
            harness=kwargs.get("cli_harness"),
        )
    run.assert_not_called()
    return host


class TestWorkerHostConstruction:
    """Model flag, harness precedence, and the tmux session name."""

    def test_no_model_renders_empty_flag(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """No model anywhere: no --model fragment."""
        host = _host(tmp_path, monkeypatch)
        assert host.model_flag == ""
        assert host.agent_model is None

    def test_cli_model_wins_over_yaml(self, tmp_path: Path, monkeypatch: Any) -> None:
        """--model beats agent_model in the yaml."""
        host = _host(tmp_path, monkeypatch, agent_model="from-yaml", cli_model="opus")
        assert host.agent_model == "opus"
        assert host.model_flag == "--model opus "

    def test_yaml_model_used_when_cli_silent(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """No CLI override: the yaml model is rendered."""
        host = _host(tmp_path, monkeypatch, agent_model="haiku")
        assert host.model_flag == "--model haiku "

    def test_cli_harness_overrides_yaml(self, tmp_path: Path, monkeypatch: Any) -> None:
        """--harness beats the yaml harness."""
        host = _host(tmp_path, monkeypatch, harness="claude", cli_harness="codex")
        assert host.harness == "codex"
        assert host.harness_impl is worker_host.HARNESSES["codex"]

    def test_invalid_cli_harness_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        """An unknown --harness fails at construction."""
        with pytest.raises(ValueError, match="bedrock"):
            _host(tmp_path, monkeypatch, cli_harness="bedrock")

    @pytest.mark.parametrize(
        ("project_name", "expected"),
        [
            ("Codex Validation: TODO CLI", "marcus_codex_validation_todo_cli"),
            ("My App v2.0", "marcus_my_app_v2_0"),
            ("simple-name_42", "marcus_simple-name_42"),
            ("UpperCaseProject", "marcus_uppercaseproject"),
            ("   ", "marcus_experiment"),
        ],
    )
    def test_tmux_session_name_is_safe(
        self, tmp_path: Path, project_name: str, expected: str
    ) -> None:
        """tmux separators and shell specials never reach the session name."""
        config = MagicMock(
            project_name=project_name, agent_model=None, harness="claude"
        )
        host = worker_host.WorkerHost(config)
        assert host.tmux_session == expected

    def test_marcus_url_comes_from_environment(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """MARCUS_URL is resolved once so tmux panes cannot lose it."""
        monkeypatch.setenv("MARCUS_URL", "http://127.0.0.1:9999/mcp")
        host = _host(tmp_path, monkeypatch)
        assert host.marcus_mcp_url == "http://127.0.0.1:9999/mcp"

    def test_marcus_url_defaults_to_the_standard_port(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Without MARCUS_URL the host uses the local default endpoint."""
        monkeypatch.delenv("MARCUS_URL", raising=False)
        host = _host(tmp_path, monkeypatch)
        assert host.marcus_mcp_url == worker_host.DEFAULT_MARCUS_MCP_URL
        assert host.marcus_mcp_url.endswith(":4298/mcp")

    def test_host_has_no_spawn_loop(self, tmp_path: Path, monkeypatch: Any) -> None:
        """The host starts nothing by itself: no run, no spawn, no teardown."""
        host = _host(tmp_path, monkeypatch)
        for name in ("run", "spawn_worker", "spawn_project_creator", "_teardown"):
            assert not hasattr(host, name), name


class TestHarnessRouting:
    """The host only translates; the harness owns the command shape."""

    def _bare(self, harness: str) -> worker_host.WorkerHost:
        host = worker_host.WorkerHost.__new__(worker_host.WorkerHost)
        host.harness = harness
        host.harness_impl = worker_host.HARNESSES[harness]
        host.model_flag = ""
        return host

    def test_register_snippet_claude(self) -> None:
        """Claude pins Marcus per command; the snippet is a marker."""
        snippet = self._bare("claude").build_mcp_register_snippet()
        assert "claude mcp add" not in snippet
        assert "codex" not in snippet

    def test_register_snippet_codex(self) -> None:
        """Codex registers Marcus idempotently."""
        snippet = self._bare("codex").build_mcp_register_snippet()
        assert "codex mcp add marcus --url" in snippet
        assert "|| true" in snippet

    def test_agent_command_claude_print_mode(self, tmp_path: Path) -> None:
        """print_mode adds --print for the one-chore invocation."""
        cmd = self._bare("claude").build_agent_command(
            tmp_path / "w", tmp_path / "p.txt", print_mode=True
        )
        assert "claude --add-dir" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--print " in cmd

    def test_agent_command_codex_ignores_print_mode(self, tmp_path: Path) -> None:
        """codex exec is already non-interactive."""
        cmd = self._bare("codex").build_agent_command(
            tmp_path / "w", tmp_path / "p.txt", print_mode=True
        )
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--print" not in cmd

    def test_model_flag_is_threaded_through(self, tmp_path: Path) -> None:
        """The rendered --model fragment reaches every harness command."""
        for harness in ("claude", "codex", "gemini"):
            host = self._bare(harness)
            host.model_flag = "--model my-model "
            cmd = host.build_agent_command(tmp_path / "w", tmp_path / "p.txt")
            assert "--model my-model" in cmd, harness


class TestTmuxAndGit:
    """Windows, liveness counting, teardown, and the per-session worktree."""

    def _bare(self) -> worker_host.WorkerHost:
        host = worker_host.WorkerHost.__new__(worker_host.WorkerHost)
        host.tmux_session = "marcus_test"
        host.harness = "claude"
        host.harness_impl = worker_host.HARNESSES["claude"]
        host.config = MagicMock(implementation_dir=Path("/repo"))
        return host

    def test_create_tmux_session_replaces_a_stale_one(self) -> None:
        """An old session with the same name is killed before the new one."""
        host = self._bare()
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="")
            host.create_tmux_session()
        commands = [c[0][0] for c in run.call_args_list]
        assert commands[0][:2] == ["tmux", "kill-session"]
        assert commands[1][:3] == ["tmux", "new-session", "-d"]

    def test_start_window_runs_script_with_exec_and_returns_pane_id(
        self, tmp_path: Path
    ) -> None:
        """Each session gets a detached window; exec closes it when the script ends."""
        host = self._bare()

        def run(cmd: Any, *args: Any, **kwargs: Any) -> MagicMock:
            if "new-window" in cmd:
                return MagicMock(returncode=0, stdout="%7\n")
            if "display-message" in cmd:
                return MagicMock(returncode=0, stdout="%7\n")
            return MagicMock(returncode=0, stdout="")

        with (
            patch("subprocess.run", side_effect=run) as mocked,
            patch.object(worker_host, "wait_for_pane_ready", return_value=True),
            patch.object(worker_host, "confirm_trust_if_prompted"),
            patch("time.sleep"),
        ):
            pane = host.start_window(tmp_path / "s.sh", "Unicorn 1")
        assert pane == "%7"
        commands = [c[0][0] for c in mocked.call_args_list]
        new_window = next(c for c in commands if "new-window" in c)
        assert "-d" in new_window
        send_keys = next(c for c in commands if "send-keys" in c)
        assert send_keys[-2].startswith("exec bash ")

    def test_count_live_panes_counts_only_ours(self) -> None:
        """Panes that vanished are sessions that went home."""
        host = self._bare()
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="%1\n%3\n%9\n")
            assert host.count_live_panes(["%1", "%2", "%3"]) == 2

    def test_kill_session_targets_the_session(self) -> None:
        """Teardown kills exactly this run's tmux session."""
        host = self._bare()
        with patch("subprocess.run") as run:
            host.kill_session()
        assert run.call_args[0][0] == ["tmux", "kill-session", "-t", "marcus_test"]

    def test_create_worktree_adds_branch_from_main(self, tmp_path: Path) -> None:
        """A fresh worktree on a new branch, with stale state pruned first."""
        host = self._bare()
        workspace = tmp_path / "worktrees" / "agent_1"
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            host.create_worktree("agent_1", "marcus/agent_1", workspace)
        commands = [c[0][0] for c in run.call_args_list]
        assert ["git", "worktree", "prune"] in commands
        assert ["git", "branch", "-D", "marcus/agent_1"] in commands
        assert commands[-1] == [
            "git",
            "worktree",
            "add",
            str(workspace),
            "-b",
            "marcus/agent_1",
        ]
        assert all(c[1]["cwd"] == Path("/repo") for c in run.call_args_list)

    def test_create_worktree_removes_an_existing_workspace_first(
        self, tmp_path: Path
    ) -> None:
        """Re-running a session id does not fail on the old directory."""
        host = self._bare()
        workspace = tmp_path / "wt"
        workspace.mkdir()
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            host.create_worktree("a", "marcus/a", workspace)
        commands = [c[0][0] for c in run.call_args_list]
        assert commands[0][:4] == ["git", "worktree", "remove", "--force"]


class TestConfirmTrustIfPrompted:
    """Auto-confirming Claude's trust and permission dialogs in a pane."""

    def _run(self, screens: list[str]) -> list[list[str]]:
        """Drive the poller through ``screens`` and return the tmux commands sent."""
        sent: list[list[str]] = []
        frames = iter(screens)

        def run(cmd: Any, *args: Any, **kwargs: Any) -> MagicMock:
            if "capture-pane" in cmd:
                return MagicMock(returncode=0, stdout=next(frames, screens[-1]))
            sent.append(list(cmd))
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=run), patch("time.sleep"):
            self.result = worker_host.confirm_trust_if_prompted(
                "%1", timeout=0.05, poll_interval=0.001
            )
        return sent

    def test_trust_prompt_is_confirmed_with_enter(self) -> None:
        """The folder-trust screen gets a single Enter."""
        sent = self._run(["Do you trust this folder? Press Enter to confirm"])
        assert self.result is True
        assert sent == [["tmux", "send-keys", "-t", "%1", "Enter"]]

    def test_permission_dialog_is_accepted(self) -> None:
        """The skip-permissions dialog gets a down-arrow then Enter."""
        sent = self._run(
            ["--dangerously-skip-permissions\n  No, exit\n  Yes, I accept"]
        )
        assert self.result is True
        assert sent[0][-2:] == ["-l", "\x1b[B"]
        assert sent[1][-1] == "Enter"

    def test_normal_startup_exits_early_without_sending_keys(self) -> None:
        """Plenty of ordinary output and no trust words: nothing is sent."""
        sent = self._run(["claude is ready. " * 20])
        assert self.result is False
        assert sent == []

    def test_times_out_when_nothing_appears(self) -> None:
        """An empty pane until the deadline is not a prompt."""
        sent = self._run([""])
        assert self.result is False
        assert sent == []
