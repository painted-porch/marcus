#!/usr/bin/env python3
"""The worker host: experiment config, tmux, worktrees, and the harness command.

This module is what remained of the spawn-per-task runner once epic #706
deleted the spawning (ADR-0012 D10: a run is the launcher's business, not
Marcus's). It holds the pieces the session launcher
(:mod:`runners.session_launcher`) needs to put a worker on a machine:

- :class:`ExperimentConfig` reads ``config.yaml``;
- :class:`WorkerHost` owns the tmux session, starts one window per session,
  creates the per-session git worktree, renders the harness command, and
  seeds Claude's trust file;
- :func:`check_mcp_health` is the pre-flight probe of the Marcus endpoint.

The host starts nothing on its own and has no loop, no watchdog, and no
notion of "a run"; the launcher decides when sessions start and the board
decides when they go home.
"""

import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import List, Optional

import yaml

# ``runners/`` must import as a package from every entry point (the runner,
# the tests, a direct ``python runners/worker_host.py``); bootstrap the
# parent directory once. The insert is idempotent.
_RUNNERS_PARENT = str(Path(__file__).resolve().parent.parent)
if _RUNNERS_PARENT not in sys.path:
    sys.path.insert(0, _RUNNERS_PARENT)

from runners.harness import HARNESSES, Harness, get_harness  # noqa: E402

DEFAULT_MARCUS_MCP_URL = "http://localhost:4298/mcp"


def wait_for_pane_ready(
    pane_target: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """Wait for a tmux pane's shell to be ready before sending commands.

    Polls the pane content until a shell prompt indicator appears or the
    content stabilises (stops changing for two consecutive polls), so
    ``send-keys`` never fires before the shell is initialised.

    Parameters
    ----------
    pane_target : str
        tmux pane target (``session:window.pane`` or a ``%N`` pane id).
    timeout : float
        Maximum seconds to wait before giving up.
    poll_interval : float
        Seconds between polls.

    Returns
    -------
    bool
        True if the pane is ready, False if it timed out.
    """
    prompt_indicators = {"$", "%", "#", "❯", ">", "›"}
    deadline = time.monotonic() + timeout
    prev_content = ""
    stable_count = 0

    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            time.sleep(poll_interval)
            continue

        content = result.stdout.rstrip()
        lines = [ln for ln in content.split("\n") if ln.strip()]
        if lines:
            last_line = lines[-1].strip()
            if any(last_line.endswith(ind) for ind in prompt_indicators):
                return True

        if content and content == prev_content:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0

        prev_content = content
        time.sleep(poll_interval)

    return False


def confirm_trust_if_prompted(
    pane_target: str,
    timeout: float = 5.0,
    poll_interval: float = 0.2,
) -> bool:
    """Poll a tmux pane and auto-confirm Claude trust and permission dialogs.

    Claude Code can pause on a directory trust prompt or a
    ``--dangerously-skip-permissions`` confirmation when launched in a fresh
    directory. This detects those screens and sends the keystrokes to proceed.

    Parameters
    ----------
    pane_target : str
        tmux pane target.
    timeout : float
        Maximum seconds to poll before giving up.
    poll_interval : float
        Seconds between polls.

    Returns
    -------
    bool
        True if a prompt was detected and confirmed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_target],
            capture_output=True,
            text=True,
        )
        text = result.stdout.lower() if result.returncode == 0 else ""

        if ("trust this folder" in text or "trust the contents" in text) and (
            "enter to confirm" in text
            or "press enter" in text
            or "enter to continue" in text
        ):
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_target, "Enter"],
                capture_output=True,
            )
            time.sleep(0.5)
            return True

        if "yes, i accept" in text and (
            "dangerously-skip-permissions" in text
            or "skip permissions" in text
            or "permission" in text
            or "approval" in text
        ):
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_target, "-l", "\x1b[B"],
                capture_output=True,
            )
            time.sleep(0.2)
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_target, "Enter"],
                capture_output=True,
            )
            time.sleep(0.5)
            return True

        # Trust prompts dominate the pane at startup; substantial content
        # without any trust keyword means the agent started normally.
        if (
            len(text) > 200
            and "trust" not in text
            and "permission" not in text
            and "approval" not in text
        ):
            return False

        time.sleep(poll_interval)

    return False


def check_mcp_health(url: str = "") -> bool:
    """Check whether the Marcus MCP server answers on its health endpoint.

    Parameters
    ----------
    url : str
        Base URL of the MCP server. Defaults to ``MARCUS_URL`` or the
        standard local endpoint.

    Returns
    -------
    bool
        True if the server responded.
    """
    if not url:
        url = os.environ.get("MARCUS_URL", DEFAULT_MARCUS_MCP_URL)
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"{url}/health"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


class ExperimentConfig:
    """Configuration for one experiment, read from ``config.yaml``.

    Parameters
    ----------
    config_path : Path
        Path to the experiment's ``config.yaml``; its parent is the
        experiment directory.

    Attributes
    ----------
    project_name : str
        The Marcus project name.
    project_spec_file : Path
        The specification sent to ``create_project``.
    project_options : dict
        Options forwarded to ``create_project``; ``project_root`` and
        ``project_info_path`` are filled in here.
    agents : list
        Session identities (``id``, ``name``, ``role``, ``skills``).
    sessions : int
        How many sessions the launcher starts; the ``sessions`` key, or the
        number of listed agents, or one.
    harness : str
        Which agent CLI runs each session.
    agent_model : Optional[str]
        Model for the agent CLI, or None for the CLI's own default.
    """

    def __init__(self, config_path: Path):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.experiment_dir = config_path.parent
        self.project_name = self.config["project_name"]
        self.project_spec_file = self.experiment_dir / self.config["project_spec_file"]
        self.project_options = self.config.get("project_options", {})
        self.agents = list(self.config.get("agents") or [])
        self.timeouts = self.config.get("timeouts", {})

        raw_sessions = self.config.get("sessions")
        if raw_sessions is None:
            self.sessions = len(self.agents) or 1
        else:
            self.sessions = int(raw_sessions)
        if self.sessions < 1:
            raise ValueError(
                f"sessions must be at least 1 in config.yaml (got {self.sessions})"
            )

        # The Marcus MCP server is agent-agnostic; the harness only decides
        # which CLI each session runs. The legal names are the registry in
        # ``harness.py``.
        raw_harness = str(self.config.get("harness", "claude")).strip().lower()
        if raw_harness not in HARNESSES:
            supported = ", ".join(sorted(HARNESSES))
            raise ValueError(
                f"Unsupported harness {raw_harness!r} in config.yaml; "
                f"expected one of: {supported}."
            )
        self.harness: str = raw_harness

        self.prompts_dir = self.experiment_dir / "prompts"
        self.logs_dir = self.experiment_dir / "logs"
        self.implementation_dir = self.experiment_dir / "implementation"
        self.prompts_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.implementation_dir.mkdir(exist_ok=True)

        # Marcus validates project_root and runs the smoke gate there.
        if "project_root" not in self.project_options:
            self.project_options["project_root"] = str(self.implementation_dir)

        self.agent_model: Optional[str] = self._resolve_agent_model()

        # Marcus writes project_info.json itself at this path; a pre-set
        # override must be the file the launcher waits for.
        self.project_info_file = self.experiment_dir / "project_info.json"
        if "project_info_path" not in self.project_options:
            self.project_options["project_info_path"] = str(self.project_info_file)
        else:
            self.project_info_file = Path(self.project_options["project_info_path"])

    def get_timeout(self, key: str, default: int) -> int:
        """Return a timeout from the ``timeouts`` block, or ``default``.

        Parameters
        ----------
        key : str
            The timeout name, for example ``project_creation``.
        default : int
            Seconds to use when the key is absent.

        Returns
        -------
        int
            The timeout in seconds.
        """
        return int(self.timeouts.get(key, default))

    def _resolve_agent_model(self) -> Optional[str]:
        """Read ``agent_model`` from yaml, falling back to ``config_marcus.json``.

        Resolution order: ``agent_model`` in ``config.yaml``, then ``ai.model``
        in the Planner's config (honouring ``MARCUS_CONFIG`` exactly as
        ``src/config/marcus_config.py`` does), then None. A missing or
        unreadable Planner config degrades to None rather than raising; a
        broken Planner config must not also break the launcher.

        Returns
        -------
        Optional[str]
            The model string, or None for the CLI's own default.
        """
        yaml_model = self.config.get("agent_model")
        if yaml_model:
            return str(yaml_model)

        env_config_path = os.environ.get("MARCUS_CONFIG", "").strip()
        if env_config_path:
            config_marcus_path = Path(env_config_path)
        else:
            # <marcus_root>/dev-tools/experiments/runners/worker_host.py
            marcus_root = Path(__file__).resolve().parents[3]
            config_marcus_path = marcus_root / "config_marcus.json"

        if not config_marcus_path.exists():
            return None
        try:
            with open(config_marcus_path) as f:
                marcus_cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        ai_block = marcus_cfg.get("ai") or {}
        model = ai_block.get("model")
        return str(model) if model else None


class WorkerHost:
    """Put workers on a machine: tmux windows, worktrees, harness commands.

    Parameters
    ----------
    config : ExperimentConfig
        The experiment configuration.
    agent_model : Optional[str]
        CLI override for the agent model; wins over ``config.agent_model``.
    harness : Optional[str]
        CLI override for the harness; wins over ``config.harness``.
    """

    def __init__(
        self,
        config: ExperimentConfig,
        agent_model: Optional[str] = None,
        harness: Optional[str] = None,
    ):
        self.config = config
        self.agent_model: Optional[str] = (
            agent_model if agent_model else config.agent_model
        )
        if harness is not None:
            normalized = harness.strip().lower()
            if normalized not in HARNESSES:
                supported = ", ".join(sorted(HARNESSES))
                raise ValueError(
                    f"Unsupported harness {harness!r}; "
                    f"expected one of: {supported}."
                )
            self.harness: str = normalized
        else:
            self.harness = config.harness
        # Resolve the per-CLI strategy eagerly so a bad name fails here, not
        # thirty seconds into a tmux dance.
        self.harness_impl: Harness = get_harness(self.harness)
        self.model_flag: str = (
            f"--model {self.agent_model} " if self.agent_model else ""
        )
        # tmux uses ``:`` and ``.`` as target separators; keep only safe
        # characters in the session name.
        safe_name = re.sub(
            r"[^A-Za-z0-9_-]+", "_", str(self.config.project_name).lower()
        ).strip("_")
        self.tmux_session = f"marcus_{safe_name or 'experiment'}"
        # tmux panes do not inherit the caller's environment, so the URL is
        # resolved once and baked into every generated script.
        self.marcus_mcp_url: str = os.environ.get("MARCUS_URL", DEFAULT_MARCUS_MCP_URL)

    # ------------------------------------------------------------ harness

    def build_mcp_register_snippet(self) -> str:
        """Return the shell line that registers Marcus MCP for this harness.

        Returns
        -------
        str
            A one-line shell command; idempotent, never fails the script.
        """
        return self.harness_impl.build_mcp_register_snippet()

    def build_agent_command(
        self,
        workdir: Path,
        prompt_file: Path,
        *,
        print_mode: bool = False,
    ) -> str:
        """Return the shell command that runs the agent CLI once.

        Parameters
        ----------
        workdir : Path
            The directory the agent may write to.
        prompt_file : Path
            The prompt piped to the CLI on stdin.
        print_mode : bool
            For claude, run non-interactively and exit when done; ignored
            by harnesses that are already non-interactive.

        Returns
        -------
        str
            The command line.
        """
        return self.harness_impl.build_agent_command(
            workdir,
            prompt_file,
            model_flag=self.model_flag,
            print_mode=print_mode,
        )

    @staticmethod
    def pretrust_directory(directory: Path) -> None:
        """Mark a directory trusted in ``~/.claude.json``.

        Uses an exclusive lock file so parallel runs do not race on the
        read-modify-write of the trust file.

        Parameters
        ----------
        directory : Path
            Directory to mark as trusted.
        """
        import fcntl

        claude_json = Path.home() / ".claude.json"
        lock_path = Path.home() / ".claude.json.lock"
        try:
            with open(lock_path, "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    if claude_json.exists():
                        with open(claude_json, "r") as f:
                            config = json.load(f)
                    else:
                        config = {}

                    projects = config.setdefault("projects", {})
                    dir_key = str(directory)

                    if dir_key not in projects:
                        projects[dir_key] = {
                            "allowedTools": [],
                            "mcpContextUris": [],
                            "mcpServers": {},
                            "enabledMcpjsonServers": [],
                            "disabledMcpjsonServers": [],
                            "hasTrustDialogAccepted": True,
                        }
                        print(f"  ✓ Pre-trusted directory: {directory}")
                    elif not projects[dir_key].get("hasTrustDialogAccepted"):
                        projects[dir_key]["hasTrustDialogAccepted"] = True
                        print(f"  ✓ Re-trusted directory: {directory}")
                    else:
                        print(f"  ✓ Directory already trusted: {directory}")
                        return

                    with open(claude_json, "w") as f:
                        json.dump(config, f, indent=2)
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠️  Could not pre-trust directory: {e}")

    # --------------------------------------------------------------- tmux

    def create_tmux_session(self) -> None:
        """Create the run's tmux session, replacing a stale one of the same name."""
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_session],
            capture_output=True,
        )
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.tmux_session,
                "-n",
                "sessions",
                "-x",
                "200",
                "-y",
                "50",
            ],
            check=True,
        )
        subprocess.run(
            ["tmux", "set-option", "-t", self.tmux_session, "mouse", "on"],
            check=True,
        )
        subprocess.run(
            [
                "tmux",
                "set-option",
                "-t",
                self.tmux_session,
                "pane-border-status",
                "top",
            ],
            check=True,
        )
        subprocess.run(
            [
                "tmux",
                "set-option",
                "-t",
                self.tmux_session,
                "pane-border-format",
                "#{pane_index}: #{pane_title}",
            ],
            check=True,
        )
        print(f"✓ Created tmux session: {self.tmux_session}")

    def start_window(self, script_file: Path, title: str) -> str:
        """Run a session script in its own detached tmux window.

        The window closes when the script ends because the script is
        ``exec``'d in place of the shell; a pane that is gone is a session
        that went home.

        Parameters
        ----------
        script_file : Path
            The session script.
        title : str
            Window and pane title.

        Returns
        -------
        str
            The pane's stable ``%N`` id.
        """
        result = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-t",
                self.tmux_session,
                "-n",
                title[:24],
                "-P",
                "-F",
                "#{pane_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        target = result.stdout.strip()
        time.sleep(0.2)
        return self._launch_script_in_pane(target, title, script_file)

    def _launch_script_in_pane(self, target: str, title: str, script_file: Path) -> str:
        """Title a pane, wait for its shell, exec the script, return the pane id.

        Parameters
        ----------
        target : str
            tmux pane target.
        title : str
            Pane title.
        script_file : Path
            Script to run.

        Returns
        -------
        str
            The pane's stable ``%N`` id.
        """
        subprocess.run(
            ["tmux", "select-pane", "-t", target, "-T", title],
            check=True,
        )
        if not wait_for_pane_ready(target):
            print(f"  ⚠ Pane {target} did not stabilize, sending anyway")
        subprocess.run(
            ["tmux", "send-keys", "-t", target, f"exec bash {script_file}", "Enter"],
            check=True,
        )
        if self.harness_impl.needs_trust_dialog_poll:
            time.sleep(1)
            confirm_trust_if_prompted(target)
        # Pane ids are never reused, unlike window and pane indices.
        id_result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return id_result.stdout.strip()

    def count_live_panes(self, pane_ids: List[str]) -> int:
        """Return how many of the given panes still exist in the session.

        Parameters
        ----------
        pane_ids : List[str]
            Pane ids returned by :meth:`start_window`.

        Returns
        -------
        int
            The number still present.
        """
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", self.tmux_session, "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        alive = set(result.stdout.split())
        return sum(1 for pane_id in pane_ids if pane_id in alive)

    def kill_session(self) -> None:
        """Kill the run's tmux session."""
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_session],
            check=False,
        )

    # ---------------------------------------------------------------- git

    def create_worktree(self, agent_id: str, branch: str, workspace: Path) -> None:
        """Create an isolated git worktree for one session, branched from main.

        Stale state from an earlier run with the same id (directory, branch,
        dangling worktree records) is removed first. Git identity is set in
        the session script through the ``GIT_*`` environment, not here,
        because worktrees share ``.git/config``.

        Parameters
        ----------
        agent_id : str
            The session identity, for the console line.
        branch : str
            Branch name, for example ``marcus/agent_unicorn_1``.
        workspace : Path
            Where the worktree goes (``worktrees/<agent_id>``, the path
            shape cost attribution reads).
        """
        repo = self.config.implementation_dir
        if workspace.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(workspace)],
                cwd=repo,
                capture_output=True,
            )
        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)
        subprocess.run(
            ["git", "worktree", "add", str(workspace), "-b", branch],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        print(f"  ✓ Worktree for {agent_id}: {workspace} (branch: {branch})")
