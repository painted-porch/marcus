#!/usr/bin/env python3
"""
Marcus Multi-Agent Experiment Spawner.

Spawns autonomous agents for a Marcus experiment based on config.yaml.
All agents work on the main branch in the experiment's implementation directory.
"""

import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Per-CLI harness implementations live in ``harness.py`` (sibling).
# This module loads under three entry points:
#
# 1. ``run_experiment.py`` → ``from runners.spawn_agents import ...``
#    (production; ``dev-tools/experiments/`` is on ``sys.path``).
# 2. Unit tests → ``from runners.spawn_agents import ...``
#    (``tests/unit/experiments/conftest.py`` adds the parent first).
# 3. Direct invocation → ``python .../runners/spawn_agents.py``
#    (Python puts ``runners/`` on ``sys.path``, NOT its parent — so
#    ``from runners.harness import ...`` would otherwise raise
#    ``ModuleNotFoundError`` before any CLI handling ran).
#
# Bootstrap the parent directory unconditionally before the harness
# import.  The insert is idempotent — paths 1/2 don't stack a
# duplicate entry.
_RUNNERS_PARENT = str(Path(__file__).resolve().parent.parent)
if _RUNNERS_PARENT not in sys.path:
    sys.path.insert(0, _RUNNERS_PARENT)

from runners.harness import HARNESSES, Harness, get_harness  # noqa: E402
from runners.marcus_client import MarcusMCPClient  # noqa: E402
from runners.spawn_controller import (  # noqa: E402
    SpawnThrashDetector,
    StallWatchdog,
    compute_spawn_count,
    experiment_lifecycle_state,
)

# A project whose widest DAG layer is this narrow is effectively
# sequential — multi-agent coordination adds overhead with little
# parallelism to gain. The control loop warns the user once when the
# graph is at or below this width. Advisory only; tune freely.
NARROW_DAG_WARN_WIDTH = 2


def wait_for_pane_ready(
    pane_target: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """Wait for a tmux pane's shell to be ready before sending commands.

    Polls the pane content until a shell prompt indicator appears or the
    content stabilizes (stops changing for 2+ consecutive polls). This
    prevents send-keys from firing before the shell is initialized.

    Parameters
    ----------
    pane_target : str
        Tmux pane target (e.g. ``session:window.pane`` or a pane ID).
    timeout : float
        Maximum seconds to wait before giving up.
    poll_interval : float
        Seconds between polls.

    Returns
    -------
    bool
        True if the pane is ready, False if timed out.
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

        # Check for shell prompt indicators on the last non-empty line
        lines = [ln for ln in content.split("\n") if ln.strip()]
        if lines:
            last_line = lines[-1].strip()
            if any(last_line.endswith(ind) for ind in prompt_indicators):
                return True

        # Content stabilization: if content hasn't changed for 2 polls
        # and there IS content, the shell is ready
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
    """Poll a tmux pane and auto-confirm Claude trust/permission dialogs.

    Claude Code can pause on a directory trust prompt or a
    --dangerously-skip-permissions confirmation dialog when launched in a
    fresh directory. This detects those screens and sends the appropriate
    keystrokes to proceed.

    Parameters
    ----------
    pane_target : str
        Tmux pane target (e.g. ``session:window.pane`` or a pane ID).
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

        # Trust prompt: "Do you trust this folder?"
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

        # --dangerously-skip-permissions confirmation dialog
        if "yes, i accept" in text and (
            "dangerously-skip-permissions" in text
            or "skip permissions" in text
            or "permission" in text
            or "approval" in text
        ):
            # Move selection to "Yes, I accept" then confirm
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

        # Early exit: trust/permission prompts appear immediately on
        # startup and dominate the pane. If the pane has substantial
        # content without any trust-related keywords, Claude has
        # started normally and no prompt is coming.
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
    """Check if the Marcus MCP server is running and healthy.

    Parameters
    ----------
    url : str
        Base URL of the MCP server. Defaults to MARCUS_URL env var or
        http://localhost:4298/mcp.

    Returns
    -------
    bool
        True if server responds successfully.
    """
    import os

    if not url:
        url = os.environ.get("MARCUS_URL", "http://localhost:4298/mcp")
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


def wait_for_project_info(
    config: "ExperimentConfig",
    poll_interval: float = 5.0,
    log_interval: float = 15.0,
) -> bool:
    """Wait for project_info.json with countdown logging.

    Parameters
    ----------
    config : ExperimentConfig
        Experiment configuration containing project_info_file path
        and timeout settings.
    poll_interval : float
        Seconds between file existence checks.
    log_interval : float
        Seconds between status log messages.

    Returns
    -------
    bool
        True if file was found, False if timed out.
    """
    timeout = config.get_timeout("project_creation", 600)
    start_time = time.time()
    last_log_time = start_time

    print("\nWaiting for project creation...")
    print("  (create_project will take 30-60s for AI task decomposition)")

    while not config.project_info_file.exists():
        elapsed = time.time() - start_time
        if elapsed > timeout:
            remaining = 0
            print(f"\n✗ Project creation timed out after " f"{int(elapsed)}s!")
            return False

        # Log countdown at regular intervals
        if time.time() - last_log_time >= log_interval:
            remaining = int(timeout - elapsed)
            print(
                f"  ... still waiting ({int(elapsed)}s elapsed, "
                f"{remaining}s remaining)"
            )
            last_log_time = time.time()

        time.sleep(poll_interval)

    print("✓ Project created!")
    return True


class ExperimentConfig:
    """Configuration for a Marcus experiment."""

    def __init__(self, config_path: Path):
        """
        Initialize experiment configuration from YAML file.

        Parameters
        ----------
        config_path : Path
            Path to config.yaml file
        """
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.experiment_dir = config_path.parent
        self.project_name = self.config["project_name"]
        self.project_spec_file = self.experiment_dir / self.config["project_spec_file"]
        self.project_options = self.config.get("project_options", {})
        self.agents = self.config["agents"]
        # max_agents is an optional concurrency cap. Absent → None →
        # the agent pool sizes to each DAG layer's full width (peaks at
        # the widest layer). An int caps it below that.
        _raw_max_agents = self.config.get("max_agents")
        self.max_agents: Optional[int] = (
            int(_raw_max_agents) if _raw_max_agents is not None else None
        )
        self.timeouts = self.config.get("timeouts", {})
        # CPM override: when True, Marcus's CPM-derived
        # ``recommended_agents`` overrides the agent template count and
        # determines how many workers spawn. When False (default),
        # exactly ``len(self.agents)`` workers spawn — the count the
        # user configured. Defaults to OFF so controlled experiments
        # (where agent count is the independent variable) get the
        # exact count specified.
        self.cpm_override = bool(self.config.get("cpm_override", False))
        # Stall watchdog: if the monitor sees no change in task counts
        # (completed/in_progress/blocked) for this many minutes, it kills
        # the tmux session to stop idle agents from polling and burning
        # tokens. 0 disables the watchdog. Kept at config top level (not
        # in project_options) so it is not forwarded to Marcus's
        # create_project, which would reject the unknown key.
        self.stall_timeout_minutes = int(self.config.get("stall_timeout_minutes", 20))

        # Spawn-thrash fast-fail: tears down after this many consecutive
        # polls of (to_spawn > 0 AND completed unchanged). Catches the
        # failure mode where a BLOCKED task gates every claimable
        # downstream task — the runner would otherwise keep spawning
        # ephemeral agents that immediately exit, burning ~$0.50–$1.00
        # per agent until the 20-minute stall watchdog fires. 5 polls at
        # the default 30s cadence = a 2.5-minute fail-fast. 0 disables.
        self.spawn_thrash_polls = int(self.config.get("spawn_thrash_polls", 5))

        # Agent harness selection (GH issue: codex harness support).
        # ``claude`` (default) spawns Anthropic's claude CLI in each pane;
        # ``codex`` spawns OpenAI's codex CLI. The Marcus MCP server is
        # agent-agnostic — the harness choice only affects which CLI
        # process is launched and which MCP registry it reads from
        # (``~/.claude.json`` vs ``~/.codex/config.toml`` vs
        # ``~/.gemini/settings.json``). Same model is applied to all
        # agents; mixed-harness teams are intentionally out of scope
        # for v1. The set of legal names is the registry from
        # ``harness.py`` — adding a fourth harness requires no edit
        # here.
        raw_harness = str(self.config.get("harness", "claude")).strip().lower()
        if raw_harness not in HARNESSES:
            supported = ", ".join(sorted(HARNESSES))
            raise ValueError(
                f"Unsupported harness {raw_harness!r} in config.yaml; "
                f"expected one of: {supported}."
            )
        self.harness: str = raw_harness

        # Set up experiment directories
        self.prompts_dir = self.experiment_dir / "prompts"
        self.logs_dir = self.experiment_dir / "logs"
        self.implementation_dir = self.experiment_dir / "implementation"

        # Create directories
        self.prompts_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.implementation_dir.mkdir(exist_ok=True)

        # Add project_root to project_options (required by Marcus validation)
        # This tells Marcus where agents will write code
        if "project_root" not in self.project_options:
            self.project_options["project_root"] = str(self.implementation_dir)

        # Issue: per-experiment agent model selection.
        #
        # Resolution order for which model the spawned ``claude``
        # processes will run on:
        #
        #   1. ``--model`` CLI flag on ``run_experiment.py``
        #      (applied by :class:`AgentSpawner.__init__`, not here)
        #   2. ``agent_model`` field at the top level of ``config.yaml``
        #      (per-experiment override committed alongside the spec)
        #   3. ``ai.model`` in ``config_marcus.json`` (the Planner's
        #      model — reused as the default Agent model so a single
        #      config setting governs both classes by default)
        #   4. ``None`` — fall back to whatever the user's ``claude``
        #      CLI is configured for globally
        #
        # The two LLM classes (Marcus-side Planner calls vs spawned
        # Agent ``claude`` processes) are independently configurable
        # but share a sensible default: whatever you set for the
        # Planner is what the Agents will also use unless you
        # override.  Cost optimization — pay for one model class by
        # default rather than two.
        self.agent_model: Optional[str] = self._resolve_agent_model()

        # Project info file (shared between creator and workers)
        self.project_info_file = self.experiment_dir / "project_info.json"

        # Tell Marcus where to write project_info.json server-side so the
        # spawner can read recommended_agents without a second HTTP session.
        # If project_info_path is pre-set in options (custom override), keep
        # self.project_info_file in sync so wait_for_project_info and the
        # project_info open() both target the same path (Codex P2 fix).
        if "project_info_path" not in self.project_options:
            self.project_options["project_info_path"] = str(self.project_info_file)
        else:
            from pathlib import Path as _Path

            self.project_info_file = _Path(self.project_options["project_info_path"])

    def get_timeout(self, key: str, default: int) -> int:
        """Get timeout value from config or use default."""
        return int(self.timeouts.get(key, default))

    def _resolve_agent_model(self) -> Optional[str]:
        """Read ``agent_model`` from yaml, falling back to config_marcus.json.

        See the comment above ``self.agent_model =`` for the resolution
        order.  Returns ``None`` when neither source provides a value;
        the caller (:class:`AgentSpawner`) then leaves ``--model`` off
        the spawned ``claude`` command lines and the agent runs on
        whatever the user's CLI is globally configured for.

        Honors the ``MARCUS_CONFIG`` environment variable the same way
        Marcus's Planner does (see ``src/config/marcus_config.py``
        ``get_config()``).  Without this, an experiment run with
        ``MARCUS_CONFIG=/path/to/custom.json`` would see the Planner
        read its model from the custom file while this resolver
        silently read the repo-root default — breaking the documented
        "inherit the Planner model" behaviour (Codex P2 on PR #540).

        Defensive: a missing or unreadable config file degrades to
        ``None`` rather than raising.  We don't want a broken Planner
        config to also break agent spawning — the two failure modes
        are independent and should stay that way.
        """
        yaml_model = self.config.get("agent_model")
        if yaml_model:
            return str(yaml_model)

        # ``MARCUS_CONFIG`` mirrors the env-var precedence used by
        # ``src/config/marcus_config.py:get_config()`` so the Agent
        # resolver always reads the SAME file the Planner reads.  The
        # repo-root ``config_marcus.json`` is the fallback only when
        # the env var is unset, matching the Planner's behaviour.
        #
        # spawn_agents.py lives at:
        #   <marcus_root>/dev-tools/experiments/runners/spawn_agents.py
        # so four ``parent`` hops lands at the repo root where
        # ``config_marcus.json`` sits in repo-checkout deployments.
        # Pip-installed Marcus must set ``MARCUS_CONFIG`` explicitly;
        # there is no canonical install location otherwise.
        env_config_path = os.environ.get("MARCUS_CONFIG", "").strip()
        if env_config_path:
            config_marcus_path = Path(env_config_path)
        else:
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


class AgentSpawner:
    """Spawns and manages autonomous agents for an experiment."""

    def __init__(
        self,
        config: ExperimentConfig,
        templates_dir: Path,
        epictetus: bool = False,
        agent_model: Optional[str] = None,
        harness: Optional[str] = None,
    ):
        """
        Initialize the agent spawner.

        Parameters
        ----------
        config : ExperimentConfig
            Experiment configuration
        templates_dir : Path
            Path to templates directory (in marcus repo)
        epictetus : bool, optional
            When True, suppresses tmux session kill on experiment completion
            so that the Epictetus post-experiment interrogation tool can
            query agents after the project is done.  Default: False (kill).
        agent_model : Optional[str], optional
            Override the model used for spawned agent CLI processes
            (project creator, workers, monitor).  Highest priority in
            the resolution chain — overrides ``config.agent_model``
            (which itself reads ``config.yaml`` and
            ``config_marcus.json`` in turn).  When ``None``, falls back
            to whatever ``config.agent_model`` resolved to.  Affects
            ONLY the spawned Agent processes; the Marcus MCP server's
            Planner LLM continues to read its model from
            ``config_marcus.json`` unchanged.  Single ``--model``
            string is passed through to whichever harness is active;
            no validation against per-harness model namespaces is
            performed here — invalid model names surface as CLI
            errors in the agent panes.
        harness : Optional[str], optional
            Agent harness to spawn: ``'claude'`` or ``'codex'``.  When
            ``None``, falls back to ``config.harness`` (which itself
            defaults to ``'claude'``).  All spawned agents in a single
            experiment use the same harness; mixed-harness teams are
            intentionally out of scope for v1.
        """
        self.config = config
        self.templates_dir = templates_dir
        self.epictetus = epictetus
        # CLI override wins over yaml/config_marcus.json resolution.
        # ``None`` here means "use whatever ExperimentConfig resolved
        # to" — which may itself be ``None`` (fallback to ``claude``
        # global default).
        self.agent_model: Optional[str] = (
            agent_model if agent_model else config.agent_model
        )
        self.agent_prompt_template = templates_dir / "agent_prompt.md"
        # CLI override wins over yaml resolution for harness too.
        # ``None`` here means "use whatever ExperimentConfig parsed
        # from yaml" — which itself defaults to ``'claude'``.
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
        # Resolve the per-CLI strategy object eagerly so a misspelled
        # harness name in ``config.yaml`` fails at spawner-construct
        # time (caught by ``run_experiment.py --validate``) rather than
        # 30 seconds into a tmux dance.  Every dispatch site
        # (mcp-register, agent command, worker wrapper, pre-flight
        # binary lookup, install hint, trust-dialog poll, pretrust
        # gate) reads from ``self.harness_impl`` instead of branching
        # on ``self.harness == "..."``.
        self.harness_impl: Harness = get_harness(self.harness)
        # Pre-render the model flag fragment once.  Spliced into every
        # agent invocation below so all spawned panes (project creator,
        # workers, monitor) run on the same model.  Empty string when
        # no model is resolved → no ``--model`` flag → harness CLI uses
        # its global default.  Both ``claude`` and ``codex`` accept the
        # long-form ``--model X`` spelling, so a single rendered
        # fragment works for either harness.
        self.model_flag: str = (
            f"--model {self.agent_model} " if self.agent_model else ""
        )
        # Back-compat alias for any code path still referencing the
        # old name.  TODO: remove once nothing reads it externally.
        self.claude_model_flag: str = self.model_flag
        self.processes: List[subprocess.Popen[bytes]] = []
        # tmux uses ``:`` as the session/window separator and ``.``
        # as the window/pane separator, so any of those characters in
        # the session name turn every later ``tmux`` call into a parse
        # error (``no such window: marcus_foo:_bar``). Strip every
        # character that is not safe for a tmux target identifier —
        # keep ASCII letters, digits, underscore, and hyphen — and
        # collapse runs of unsafe chars into a single underscore.
        safe_name = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            self.config.project_name.lower(),
        ).strip("_")
        self.tmux_session = f"marcus_{safe_name or 'experiment'}"
        self.panes_per_window = 2
        self.current_window = 0
        self.current_pane = 0
        # tmux base indices (base-index / pane-base-index). Placeholder
        # defaults here; the real values are detected in
        # create_tmux_session() once the session exists. Detection MUST
        # happen there, not now: tmux may have no server running at
        # construction time, and `tmux new-session` is what starts the
        # server and sources ~/.tmux.conf. Reading the options before
        # that would always return the default 0 even when the user's
        # config sets base-index 1.
        self._tmux_base_index = 0
        self._tmux_pane_base_index = 0
        # Resolve the Marcus MCP URL once at spawner init time so it is
        # baked into each generated shell script. tmux new-session does NOT
        # inherit the calling process's environment (tmux runs a daemon), so
        # ${MARCUS_URL:-...} inside pane shells always falls back to the
        # default port 4298 even when MARCUS_URL is set in the caller's env.
        self.marcus_mcp_url: str = os.environ.get(
            "MARCUS_URL", "http://localhost:4298/mcp"
        )

    def _build_mcp_register_snippet(self) -> str:
        """Return the shell snippet that registers Marcus MCP for this harness.

        Each agent pane re-runs this on startup so that fresh shells
        (and freshly-provisioned dev machines) pick up the Marcus MCP
        endpoint without manual setup.  The registration is idempotent
        for both harnesses and any non-zero exit is swallowed so a
        pre-existing registration does not abort the pane.

        Returns
        -------
        str
            A one-line shell command (no trailing newline).  The caller
            interpolates this into the per-pane bash script alongside
            the ``MARCUS_MCP_URL`` env var.
        """
        return self.harness_impl.build_mcp_register_snippet()

    def _build_worker_invocation_block(self, workdir: Path, prompt_file: Path) -> str:
        """Return the shell block that runs a worker agent in its pane.

        Issue #595 Fix 3: workers are ephemeral — each does exactly one
        task, then the process must exit so its tmux pane closes and the
        runner's live-agent count drops by one. The agent is therefore
        launched in non-interactive ``--print`` mode (``print_mode=True``).
        The interactive ``claude`` TUI would instead sit alive at a prompt
        after the task finished — the pane would never close, the runner
        would count it as a live agent forever, and spawning would stall.

        (codex note: ``wrap_worker_invocation`` still wraps codex in a
        relaunch loop, which predates the ephemeral model and should be
        removed for codex as a follow-up; the claude path is unwrapped.)

        Parameters
        ----------
        workdir : Path
            Agent's worktree directory.
        prompt_file : Path
            Path to the agent prompt fed to the agent CLI.

        Returns
        -------
        str
            Shell block interpolated into the generated worker script.
        """
        single_cmd = self._build_agent_command(workdir, prompt_file, print_mode=True)
        return self.harness_impl.wrap_worker_invocation(single_cmd)

    def _build_agent_command(
        self,
        workdir: Path,
        prompt_file: Path,
        *,
        print_mode: bool = False,
    ) -> str:
        """Return the shell command that launches the agent CLI.

        Both harnesses are invoked with a piped prompt
        (``< prompt_file``) and run unattended:

        - ``claude`` uses ``--dangerously-skip-permissions`` and
          (optionally) ``--print`` for non-interactive output.
        - ``codex`` uses ``exec --dangerously-bypass-approvals-and-sandbox``.
          That is the documented flag (visible in ``codex exec --help``)
          for "skip all confirmation prompts and execute commands
          without sandboxing".  There is also a hidden ``--yolo``
          alias that behaves identically on ``codex-cli`` ≥0.130.0,
          but hidden aliases are not part of any stability contract
          and have a history of being renamed/removed without notice.
          The documented flag has a much longer half-life.
          ``codex exec`` is inherently non-interactive — the
          ``print_mode`` argument is therefore ignored for codex.

        Parameters
        ----------
        workdir : Path
            Directory the agent should treat as its writable workspace.
            Translated to ``claude --add-dir`` or ``codex -C ... --add-dir``.
        prompt_file : Path
            File whose contents become the agent's initial prompt
            (piped on stdin).
        print_mode : bool, optional
            For ``claude``: append ``--print`` so the agent runs
            non-interactively and exits with stdout flushed.  Used by
            the project creator pane.  Ignored for ``codex`` (which is
            always non-interactive).

        Returns
        -------
        str
            A single shell command line (line continuations preserved
            with trailing backslash + newline).
        """
        return self.harness_impl.build_agent_command(
            workdir,
            prompt_file,
            model_flag=self.model_flag,
            print_mode=print_mode,
        )

    @staticmethod
    def _first_int(cmd: List[str], default: int) -> int:
        """Run a command and return the first line of stdout as an int.

        Parameters
        ----------
        cmd : List[str]
            Command to run (a ``tmux`` query producing one int per line).
        default : int
            Value returned when the command fails or yields no integer.

        Returns
        -------
        int
            The first integer line of stdout, or ``default``.
        """
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                if lines and lines[0].strip().isdigit():
                    return int(lines[0].strip())
        except Exception:
            pass
        return default

    @staticmethod
    def _pretrust_directory(directory: Path) -> None:
        """Pre-trust a directory in ~/.claude.json.

        Marks the directory as trusted so Claude skips the
        'Do you trust this folder?' prompt.

        Uses an exclusive lock file so parallel experiments don't race
        to read-modify-write ~/.claude.json simultaneously, which can
        cause one process to overwrite another's trust entry.

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

    def create_project_creator_prompt(self) -> str:
        """
        Create the prompt for the project creator agent.

        Returns
        -------
        str
            Prompt for project creation
        """
        with open(self.config.project_spec_file, "r") as f:
            project_description = f.read()

        options_str = json.dumps(self.config.project_options)

        prompt = f"""You are an autonomous Project Creator Agent. Execute these \
steps IMMEDIATELY without asking for permission.

WORKING DIRECTORY: {self.config.implementation_dir}

EXECUTE NOW - DO NOT ASK FOR CONFIRMATION:

1. First, verify the environment (quick checks only):
   - Confirm working directory: {self.config.implementation_dir}
   - Ping Marcus: mcp__marcus__ping

2. Call mcp__marcus__create_project and WAIT FOR COMPLETE RESPONSE:
   - project_name: "{self.config.project_name}"
   - description: (the full spec below)
   - options: {options_str}

   CRITICAL: This call is SYNCHRONOUS and takes 30-60 seconds
   - The AI will break down the spec into tasks AND subtasks
   - Do NOT proceed to step 3 until you see the FULL response
   - The response contains project_id, board_id, and tasks_created count
   - When the response arrives, ALL subtasks are already created in Kanban

PROJECT SPECIFICATION:
{project_description}

3. ONLY AFTER create_project returns with full response:
   - Marcus has already written project_info.json automatically.
     DO NOT overwrite that file — it contains recommended_agents
     from CPM.  You do not need to read it; the create_project
     response already gave you project_id and board_id.
   - Extract project_id from the create_project response for use in step 4.
   - Run: git add -A && git commit -m "Initial commit: Marcus project created"
   - Print: "PROJECT CREATED: project_id=<id> tasks=<count>"

4. IMMEDIATELY start MLflow experiment tracking:
   - Call mcp__marcus__start_experiment with:
     - experiment_name: "\
{self.config.project_name.lower().replace(' ', '_')}_experiment"
     - run_name: "\
{self.config.project_name.lower().replace(' ', '_')}_{{timestamp}}"
     - project_id: (the project_id from step 2)
     - board_id: (the board_id from step 2)
     - tags: {{"project_type": \
"{self.config.project_options.get('complexity', 'standard')}", \
"provider": "{self.config.project_options.get('provider', 'sqlite')}"}}
     - params: {{"num_agents": {len(self.config.agents)}}}
   - Print: "EXPERIMENT STARTED: <experiment_name>"
   - Exit

CRITICAL INSTRUCTIONS:
- Work in: {self.config.implementation_dir}
- DO NOT ask "May I proceed?" - just do it
- DO NOT wait for confirmation - execute immediately
- This is an automated process - no human interaction needed
"""
        return prompt

    def create_worker_prompt(
        self,
        agent: Dict[str, Any],
        workspace: Optional[Path] = None,
        branch: str = "main",
    ) -> str:
        """
        Create the prompt for a worker agent.

        Parameters
        ----------
        agent : Dict
            Agent configuration from config.yaml
        workspace : Path, optional
            Agent's worktree path. Defaults to implementation_dir.
        branch : str
            Agent's git branch. Defaults to "main".

        Returns
        -------
        str
            Prompt for worker agent
        """
        # Read the base agent prompt template
        with open(self.agent_prompt_template, "r") as f:
            base_prompt = f.read()

        agent_id = agent["id"]
        agent_name = agent["name"]
        agent_role = agent["role"]
        agent_skills = agent["skills"]
        num_subagents = agent.get("subagents", 0)

        work_dir = workspace or self.config.implementation_dir

        worker_prompt = f"""You are {agent_name} (ID: {agent_id})
in a Marcus multi-agent experiment.

Your role: {agent_role}
Your skills: {", ".join(agent_skills)}
Project root: {work_dir}

STARTUP SEQUENCE:
1. project_info.json has already been materialized into your
   working directory by the worker shell script (cwd-local at
   ``./project_info.json``).  Keeping it inside your workdir means
   harnesses with a workspace sandbox (gemini) can read it without
   special directory whitelisting; harnesses with full filesystem
   access (claude, codex) work the same way.

2. Read ./project_info.json (relative to your workdir) and extract
   project_id (required for project-scoped task assignment — GH-388).
   Then use mcp__marcus__register_agent to register yourself:
   - agent_id: "{agent_id}"
   - name: "{agent_name}"
   - role: "{agent_role}"
   - skills: {json.dumps(agent_skills)}
   - project_id: <project_id from project_info.json>

3. Register {num_subagents} subagents:
   For i in 1 to {num_subagents}:
   - Use mcp__marcus__register_agent with:
     - agent_id: "{agent_id}_sub{{i}}"
     - name: "{agent_name} Subagent {{i}}"
     - role: "{agent_role}"
     - skills: {json.dumps(agent_skills)}
     - project_id: <same project_id from project_info.json>

4. Call mcp__marcus__request_next_task ONCE:
   - No parameters needed. This finds a task suitable for your skills.
   - If you receive a task → go to step 5.
   - If you receive "no suitable tasks" → there is no work for you.
     Print a one-line note and EXIT immediately. Do NOT sleep, do NOT
     retry, do NOT poll. You are an ephemeral, single-task agent; the
     runner spawns a fresh agent whenever a task genuinely becomes
     available, so an idle agent that waits only burns tokens.

5. Do the task:
   - FIRST: run `git merge main --no-edit` to get latest completed work
   - You already have everything you need to start. Your worktree is
     an already-scaffolded, building project (Marcus set it up); your
     task description and get_task_context (the project contract plus
     the artifacts from your dependencies) carry the rest. Do NOT scan
     the filesystem with find/ls/grep to build a mental model — read
     your task, call get_task_context, then build. You are not
     discovering a codebase, you are adding one piece to a known one.
   - Work on it in: {work_dir}
   - Report progress at 25%, 50%, 75%, 100% with report_task_progress
   - Commit to your branch: {branch} (git add, commit)

6. When the task is complete (reported at 100%): print a one-line
   summary of what you did and EXIT.

   You do EXACTLY ONE task, then stop. Do NOT call request_next_task
   again. Do NOT poll for more work. Do NOT loop. The runner is
   responsible for spawning the next agent for the next task — that is
   not your job. Finishing your one task and exiting cleanly IS the
   correct, complete behavior.

---

{base_prompt}

---

CRITICAL REMINDERS:
- Work directory: {work_dir}
- Git branch: {branch} (your isolated workspace)
- You do EXACTLY ONE task, then exit. No loop, no polling.
- If request_next_task returns "no suitable tasks", exit immediately —
  do not sleep or retry.
- After completing your one task, exit — do NOT request another.
- Use get_task_context for tasks with dependencies
- Use log_decision for architectural choices
- Use log_artifact with project_root: {work_dir}

START NOW!
"""
        return worker_prompt

    def create_tmux_session(self) -> None:
        """Create tmux session for the experiment."""
        # Kill existing session if it exists
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_session],
            capture_output=True,
        )

        # Create new session (detached) with explicit dimensions so panes
        # are large enough for Claude's TUI even before a client attaches.
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.tmux_session,
                "-n",
                "agents-0",
                "-x",
                "200",
                "-y",
                "50",
            ],
            check=True,
        )

        # Enable mouse mode for easy pane navigation
        subprocess.run(
            ["tmux", "set-option", "-t", self.tmux_session, "mouse", "on"],
            check=True,
        )

        # Enable pane border status to show pane titles
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

        # Set pane border format to show the title clearly
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

        # Detect the tmux base indices from the session that now exists.
        # This is the reliable point to do it: `tmux new-session` above
        # has started the server and sourced ~/.tmux.conf, so the first
        # window/pane indices reflect the user's base-index setting.
        # Reading them from the live session (rather than global options)
        # also works regardless of whether a server was already running
        # when this spawner was constructed.
        self._tmux_base_index = self._first_int(
            ["tmux", "list-windows", "-t", self.tmux_session, "-F", "#{window_index}"],
            default=0,
        )
        self._tmux_pane_base_index = self._first_int(
            ["tmux", "list-panes", "-t", self.tmux_session, "-F", "#{pane_index}"],
            default=0,
        )

        print(f"✓ Created tmux session: {self.tmux_session}")
        print("  - Mouse mode enabled (click to switch panes)")
        print("  - Pane borders show agent names")
        print(
            f"  - tmux base-index={self._tmux_base_index}, "
            f"pane-base-index={self._tmux_pane_base_index}"
        )

    def get_next_pane_location(self) -> tuple[int, int]:
        """
        Get the next window and pane number for an agent.

        Returns
        -------
        tuple[int, int]
            (window_number, pane_number)
        """
        window = self.current_pane // self.panes_per_window
        pane = self.current_pane % self.panes_per_window

        # Create new window if needed
        if pane == 0 and window > self.current_window:
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-t",
                    f"{self.tmux_session}:{window + self._tmux_base_index}",
                    "-n",
                    f"agents-{window}",
                ],
                check=True,
            )
            self.current_window = window

        self.current_pane += 1
        return window, pane

    def run_in_tmux_pane(
        self,
        window: int,
        pane: int,
        script_file: Path,
        title: str,
        close_on_exit: bool = False,
    ) -> str:
        """
        Run a script in a specific tmux pane.

        Parameters
        ----------
        window : int
            Window number
        pane : int
            Pane number within the window
        script_file : Path
            Path to the script to run
        title : str
            Title for the pane
        close_on_exit : bool, optional
            When True, the pane closes the moment the script finishes
            (used for ephemeral worker panes so a finished agent stops
            being counted as live). When False the pane drops back to an
            idle shell and lingers — correct for the project-creator
            pane, which is kept as the session's anchor.
        """
        # For first pane in window, use existing pane
        if pane == 0:
            target = (
                f"{self.tmux_session}:"
                f"{window + self._tmux_base_index}.{self._tmux_pane_base_index}"
            )
        elif pane == 1:
            # Split window horizontally so pane 1 lands right of pane 0.
            split_target = (
                f"{self.tmux_session}:"
                f"{window + self._tmux_base_index}"
                f".{self._tmux_pane_base_index}"
            )
            result = subprocess.run(
                [
                    "tmux",
                    "split-window",
                    "-h",
                    "-t",
                    split_target,
                    "-P",
                    "-F",
                    "#{pane_id}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            target = result.stdout.strip()
            time.sleep(0.2)  # Give tmux time to stabilize
        else:
            raise ValueError(
                f"Invalid pane number: {pane} "
                f"(expected 0 or 1 with panes_per_window=2)"
            )

        return self._launch_script_in_pane(target, title, script_file, close_on_exit)

    def run_worker_in_new_window(self, script_file: Path, title: str) -> str:
        """
        Launch an ephemeral worker in its own fresh tmux window.

        Each ephemeral worker gets a new window rather than a pane in a
        shared 2-pane window. ``tmux new-window`` always succeeds — it
        does not depend on any existing window or pane surviving, unlike
        the monotonic ``get_next_pane_location`` + ``split-window``
        scheme, which breaks once finished workers' panes (and the
        windows holding them) close. The worker runs with
        ``close_on_exit=True`` so its window closes when the one task is
        done. ``-d`` keeps the new window from stealing focus.

        Parameters
        ----------
        script_file : Path
            Worker script to run.
        title : str
            Window/pane title (the agent name).

        Returns
        -------
        str
            The new pane's stable ``%N`` id.
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
        time.sleep(0.2)  # Give tmux time to stabilize
        return self._launch_script_in_pane(
            target, title, script_file, close_on_exit=True
        )

    def _launch_script_in_pane(
        self,
        target: str,
        title: str,
        script_file: Path,
        close_on_exit: bool,
    ) -> str:
        """
        Launch a script in an already-created tmux pane.

        Shared tail of :meth:`run_in_tmux_pane` and
        :meth:`run_worker_in_new_window`: title the pane, wait for its
        shell, send the launch command, clear any trust dialog, and
        return the pane's stable id.

        Parameters
        ----------
        target : str
            tmux pane target (a ``%N`` pane id or ``session:window.pane``).
        title : str
            Pane title.
        script_file : Path
            Script to run.
        close_on_exit : bool
            When True the script runs via ``exec`` so the pane closes
            the instant it finishes — an ephemeral worker that has done
            its one task stops being counted as a live agent. When False
            the pane drops back to an idle shell and lingers (the
            project-creator anchor pane).

        Returns
        -------
        str
            The pane's stable ``%N`` id.
        """
        subprocess.run(
            ["tmux", "select-pane", "-t", target, "-T", title],
            check=True,
        )

        # Wait for the pane shell to be ready before sending commands
        if not wait_for_pane_ready(target):
            print(f"  ⚠ Pane {target} did not stabilize, sending anyway")

        # ``exec`` (close_on_exit) replaces the pane's shell with the
        # script process, so the pane closes the instant the script
        # finishes — an ephemeral worker that has done its one task
        # stops being counted as a live agent. Without it the pane drops
        # back to an idle shell and lingers, miscounted as still working.
        launch_cmd = (
            f"exec bash {script_file}" if close_on_exit else f"bash {script_file}"
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", target, launch_cmd, "Enter"],
            check=True,
        )

        # Auto-confirm trust/permission prompts. Codex under ``--yolo``
        # raises no trust dialog, so the poll is gated per-harness.
        if self.harness_impl.needs_trust_dialog_poll:
            time.sleep(1)  # Let the agent start up before polling
            confirm_trust_if_prompted(target)

        # Resolve the stable tmux pane id so callers can track this
        # pane's liveness — tmux renumbers integer indices when panes
        # close, but pane ids (``%N``) are never reused.
        id_result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return id_result.stdout.strip()

    def copy_agent_workflow_to_implementation(self) -> None:
        """
        Copy agent workflow instructions into the implementation directory.

        The set of filenames to write comes from
        ``self.harness_impl.workflow_files`` — each registered harness
        declares the per-directory context-file conventions its CLI
        reads (``CLAUDE.md`` for Claude Code, ``AGENTS.md`` for Codex
        CLI per https://developers.openai.com/codex/guides/agents-md,
        ``GEMINI.md`` for Gemini CLI).  All declared files get the
        same template content; extras are harmless when the harness
        does not read them, but load-bearing when it does — a codex
        worker that finds no ``AGENTS.md`` (or a gemini worker that
        finds no ``GEMINI.md``) starts with zero Marcus workflow
        guidance and silently fails to call ``register_agent`` /
        ``request_next_task``.

        Writing every harness's declared files (not just the active
        harness's) keeps the spawn code harness-agnostic and means a
        future "swap harness mid-experiment" path stays viable.
        """
        # Read the agent prompt template once.
        with open(self.agent_prompt_template, "r") as f:
            workflow_content = f.read()

        # Union of every registered harness's workflow files — so a
        # codex agent always sees ``AGENTS.md``, a gemini agent
        # always sees ``GEMINI.md``, and so on, regardless of which
        # harness is currently active.
        filenames: set[str] = set()
        for impl in HARNESSES.values():
            filenames.update(impl.workflow_files)

        for filename in sorted(filenames):
            target = self.config.implementation_dir / filename
            with open(target, "w") as f:
                f.write(workflow_content)
            print(f"✓ Agent workflow copied to {target}")

    def spawn_project_creator(self) -> None:
        """Spawn the project creator agent in a tmux pane."""
        print("=" * 60)
        print("Spawning Project Creator Agent")
        print("=" * 60)

        # Copy agent workflow to implementation directory
        self.copy_agent_workflow_to_implementation()

        prompt = self.create_project_creator_prompt()
        prompt_file = self.config.prompts_dir / "project_creator.txt"

        with open(prompt_file, "w") as f:
            f.write(prompt)

        # Create a script to run in tmux
        script = f"""#!/bin/bash
# Source shell profile to get nvm/claude in PATH
[ -f ~/.zshrc ] && source ~/.zshrc
[ -f ~/.bashrc ] && source ~/.bashrc

# Prevent Claude from detecting nesting and refusing to start
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION

# Normalize TERM for non-interactive shells (IDE terminals, CI, cron)
if [ "$TERM" = "dumb" ] || [ -z "$TERM" ]; then
    export TERM=xterm-256color
fi

cd {self.config.implementation_dir} || exit 1
echo "=========================================="
echo "PROJECT CREATOR AGENT"
echo "Working Directory: $(pwd)"
echo "=========================================="
echo ""
echo "Configuring Marcus MCP..."
MARCUS_MCP_URL="{self.marcus_mcp_url}"
{self._build_mcp_register_snippet()}
echo ""
echo "Creating Marcus project: {self.config.project_name}"
echo ""
# Launch agent from the implementation directory (cwd matters!)
{self._build_agent_command(self.config.implementation_dir, prompt_file, print_mode=True)}  # noqa: E501
echo ""
echo "=========================================="
echo "Project Creator Complete"
echo "=========================================="
"""
        script_file = self.config.prompts_dir / "project_creator.sh"
        with open(script_file, "w") as f:
            f.write(script)
        script_file.chmod(0o755)

        # Get pane location and run
        window, pane = self.get_next_pane_location()
        self.run_in_tmux_pane(window, pane, script_file, "Project Creator")

        print(f"✓ Project creator in tmux window {window}, pane {pane}")
        print(f"  Prompt: {prompt_file}")

    def _create_agent_worktree(
        self,
        agent_id: str,
        agent_name: str,
        branch: str,
        workspace: Path,
    ) -> None:
        """
        Create an isolated git worktree for a worker agent.

        Each agent gets its own worktree branched from main HEAD.
        Git identity is set per worktree for commit attribution.
        See: https://github.com/lwgray/marcus/issues/250

        Parameters
        ----------
        agent_id : str
            Agent identifier
        agent_name : str
            Human-readable agent name (for git user.name)
        branch : str
            Branch name (e.g., marcus/agent_unicorn_1)
        workspace : Path
            Path for the worktree directory
        """
        repo = self.config.implementation_dir

        # Clean up stale worktree/branch if re-running
        if workspace.exists():
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace),
                ],
                cwd=repo,
                capture_output=True,
            )
        # Prune stale worktree references (e.g., from rm -rf cleanup)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo,
            capture_output=True,
        )

        # Create worktree (branches from current main HEAD)
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                str(workspace),
                "-b",
                branch,
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Git identity is set via GIT_AUTHOR_NAME/GIT_COMMITTER_NAME
        # environment variables in the agent's shell script, NOT via
        # git config — because worktrees share .git/config and the
        # second agent overwrites the first. (GH-308)

        print(f"  ✓ Worktree: {workspace} (branch: {branch})")

    def spawn_worker(self, agent: Dict[str, Any]) -> str:
        """
        Spawn a worker agent in a tmux pane with git worktree isolation.

        Each worker gets its own git worktree on branch marcus/{agent_id}.
        Git identity is set per worktree for commit attribution.
        See: https://github.com/lwgray/marcus/issues/250

        Parameters
        ----------
        agent : Dict
            Agent configuration
        """
        agent_id = agent["id"]
        agent_name = agent["name"]
        agent_role = agent["role"]
        num_subagents = agent.get("subagents", 0)

        print(f"\nSpawning {agent_name} ({agent_id})")
        print("-" * 60)

        # Create git worktree for this agent (GH-250)
        agent_branch = f"marcus/{agent_id}"
        agent_workspace = self.config.experiment_dir / "worktrees" / agent_id
        self._create_agent_worktree(agent_id, agent_name, agent_branch, agent_workspace)

        prompt = self.create_worker_prompt(agent, agent_workspace, agent_branch)
        prompt_file = self.config.prompts_dir / f"{agent_id}.txt"

        with open(prompt_file, "w") as f:
            f.write(prompt)

        # Create a script to run in tmux
        script = f"""#!/bin/bash
# Source shell profile to get nvm/claude in PATH
[ -f ~/.zshrc ] && source ~/.zshrc
[ -f ~/.bashrc ] && source ~/.bashrc

# Prevent Claude from detecting nesting and refusing to start
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION

# Normalize TERM for non-interactive shells (IDE terminals, CI, cron)
if [ "$TERM" = "dumb" ] || [ -z "$TERM" ]; then
    export TERM=xterm-256color
fi

# Set git identity via env vars — per-process, not shared config (GH-308)
# Worktrees share .git/config so git config user.name gets overwritten.
export GIT_AUTHOR_NAME="{agent_name}"
export GIT_AUTHOR_EMAIL="{agent_id}@marcus.ai"
export GIT_COMMITTER_NAME="{agent_name}"
export GIT_COMMITTER_EMAIL="{agent_id}@marcus.ai"

cd {agent_workspace} || exit 1
echo "=========================================="
echo "{agent_name.upper()}"
echo "ID: {agent_id}"
echo "Role: {agent_role}"
echo "Branch: {agent_branch} (isolated worktree)"
echo "Working Directory: $(pwd)"
echo "=========================================="
echo ""
echo "Waiting for project creation..."
while [ ! -f {self.config.project_info_file} ]; do
    sleep 2
done
echo "✓ Project found, starting agent..."
# Materialize project_info.json into the worker's cwd so the agent
# never has to read outside its workdir.  Required for the gemini
# harness (whose sandbox refuses tool calls outside
# ``--include-directories``); harmless for claude / codex.
cp {self.config.project_info_file} ./project_info.json
echo ""
echo "Configuring Marcus MCP..."
MARCUS_MCP_URL="{self.marcus_mcp_url}"
{self._build_mcp_register_snippet()}
echo ""
# Sync worktree with main to get design artifacts and any
# previously merged code (GH-302: per-task visibility)
echo "Syncing worktree with main..."
git merge main --no-edit 2>/dev/null || true
echo "✓ Worktree synced"
echo ""
# Launch agent from the agent's isolated worktree (cwd matters!)
# Workers use the relaunch-aware helper because codex exec exits
# when the model judges its goal complete; claude branch returns
# the single command unwrapped (TUI stays alive on its own).
{self._build_worker_invocation_block(agent_workspace, prompt_file)}
echo ""
echo "=========================================="
echo "{agent_name} - Work Complete"
echo "=========================================="
"""
        script_file = self.config.prompts_dir / f"{agent_id}.sh"
        with open(script_file, "w") as f:
            f.write(script)
        script_file.chmod(0o755)

        # Each ephemeral worker runs in its own fresh tmux window — the
        # window closes when its one task finishes. A shared-window pane
        # layout breaks once finished workers' windows close.
        pane_id = self.run_worker_in_new_window(script_file, agent_name)

        print(f"  ✓ Spawned worker pane {pane_id}")
        print(f"  Prompt: {prompt_file}")
        print(f"  Subagents: {num_subagents}")
        return pane_id

    @staticmethod
    def _fetch_recommended_agents(
        marcus_url: str = "http://localhost:4298/mcp",
        timeout: float = 10.0,
    ) -> int:
        """Query Marcus directly for the recommended agent count via MCP HTTP.

        Bypasses the LLM creator agent entirely.  Called after Phase 1 so
        that ``create_project`` has already populated the server's in-memory
        task list, giving CPM meaningful data to work with.

        Parameters
        ----------
        marcus_url : str
            Base MCP endpoint for the running Marcus server.
        timeout : float
            Per-request timeout in seconds.

        Returns
        -------
        int
            Recommended agent count, or 0 if the call fails (caller falls
            back to the user-supplied config count).
        """
        client = MarcusMCPClient(marcus_url=marcus_url, timeout=timeout)
        if not client.connect():
            print(
                "\n  CPM query failed (could not connect to Marcus); "
                "falling back to config count."
            )
            return 0

        result = client.call_tool("get_optimal_agent_count", {"include_details": False})
        if result is None:
            print("\n  CPM query returned no result; " "falling back to config count.")
            return 0

        try:
            return int(result.get("optimal_agents", 0))
        except (TypeError, ValueError):
            return 0

    def _count_live_worker_panes(self, worker_pane_ids: List[str]) -> int:
        """
        Count how many of the runner's worker panes are still alive.

        An ephemeral agent's pane closes when the agent finishes its one
        task and exits, so a pane id that no longer appears in the tmux
        session is a worker that has completed. This count is the
        runner's own ``live_agents`` — it must not be derived from
        Marcus, whose registered-agent view lags spawning and would
        cause double-spawn (issue #595 Fix 3).

        Parameters
        ----------
        worker_pane_ids : List[str]
            Every worker pane id the runner has spawned this run.

        Returns
        -------
        int
            Number of those panes still present in the tmux session.
        """
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-s",
                "-t",
                self.tmux_session,
                "-F",
                "#{pane_id}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        alive = set(result.stdout.split())
        return sum(1 for pane_id in worker_pane_ids if pane_id in alive)

    def _teardown(self, reason: str) -> None:
        """
        End the experiment: kill the tmux session after a grace period.

        Absorbs the old monitor agent's completion / stall actions. In
        epictetus mode the session is kept alive so the post-run
        interrogation tool can query agents; otherwise workers get a
        60-second grace period to exit cleanly and the session is then
        killed so no pane keeps burning tokens.

        Parameters
        ----------
        reason : str
            Why the run is ending — ``"complete"`` or ``"stalled"`` —
            used only for the console summary.
        """
        print("\n" + "=" * 60)
        print(f"[Teardown] Experiment {reason}")
        print("=" * 60)
        if self.epictetus:
            print(
                "Epictetus mode — tmux session kept alive for agent "
                f"interrogation: tmux attach -t {self.tmux_session}"
            )
            return
        print("Grace period (60s) for agents to exit cleanly...")
        time.sleep(60)
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_session],
            check=False,
        )
        print(f"Tmux session '{self.tmux_session}' terminated.")

    def run(self) -> bool:
        """Run the multi-agent experiment and return success status."""
        print("\n" + "=" * 60)
        print("Marcus Multi-Agent Experiment")
        print("=" * 60)
        print(f"Experiment: {self.config.experiment_dir}")
        print(f"Project: {self.config.project_name}")
        print(f"Implementation: {self.config.implementation_dir}")
        print(f"Agents: {len(self.config.agents)}")
        total_subagents = sum(a.get("subagents", 0) for a in self.config.agents)
        print(f"Subagents: {total_subagents}")
        print(f"Harness: {self.harness}")
        print(
            "Agent model: "
            + (
                self.agent_model
                or f"({self.harness} CLI default — no model set in "
                "config.yaml.agent_model, config_marcus.json.ai.model, or "
                "--model)"
            )
        )

        # Harness pre-flight: fail fast if the chosen CLI is not on
        # PATH. Without this the per-pane bash script silently fails
        # inside tmux ("command not found") and the user has to attach
        # to a pane to discover why nothing ran.
        #
        # Two-stage lookup: ``shutil.which`` covers the common case
        # (binary on the parent process's PATH). When that misses, fall
        # back to a bash subprocess that sources ``~/.zshrc`` and
        # ``~/.bashrc`` — exactly what the per-pane scripts do — and
        # tries ``command -v``. This catches the common nvm / npm / asdf
        # case where ``claude`` or ``codex`` is added to PATH only by
        # shell init files and the parent Python's PATH does not see it.
        import shutil as _shutil

        cli_name = self.harness_impl.binary

        def _harness_binary_available(name: str) -> bool:
            if _shutil.which(name) is not None:
                return True
            try:
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        (
                            "[ -f ~/.zshrc ] && source ~/.zshrc "
                            "2>/dev/null; "
                            "[ -f ~/.bashrc ] && source ~/.bashrc "
                            "2>/dev/null; "
                            f"command -v {name}"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                return result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        if not _harness_binary_available(cli_name):
            print(
                f"\n❌ Harness CLI '{cli_name}' not found — checked "
                "parent PATH and an interactive shell that sources "
                "~/.zshrc / ~/.bashrc."
            )
            for hint_line in self.harness_impl.install_hint(self.marcus_mcp_url):
                print(hint_line)
            return False

        # Calculate tmux layout
        total_agents = 1 + len(self.config.agents) + 1  # creator + workers + monitor
        num_windows = (
            total_agents + self.panes_per_window - 1
        ) // self.panes_per_window
        print(
            f"Tmux Layout: {num_windows} window(s) with up to "
            f"{self.panes_per_window} panes each"
        )
        print("=" * 60)

        # Verify MLflow is available
        print("\n[Setup] Verifying MLflow installation...")
        try:
            import warnings

            warnings.filterwarnings(
                "ignore",
                message='Field "model_name" has conflict with protected namespace',
            )
            import mlflow  # noqa: F401

            print("✓ MLflow is installed and ready")
            print("  Experiments will be tracked in: ./mlruns")
        except ImportError:
            print("⚠️  MLflow not found!")
            print("  Install with: pip install mlflow")
            print("  Experiment tracking will not be available")

        # Clean up state from previous runs
        print("\n[Setup] Cleaning up previous experiment state...")
        if self.config.project_info_file.exists():
            self.config.project_info_file.unlink()
            print("  ✓ Removed old project_info.json")

        # Initialize git repository if not already present
        git_dir = self.config.implementation_dir / ".git"
        if not git_dir.exists():
            print("\n[Setup] Initializing git repository...")
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=self.config.implementation_dir,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "checkout", "-b", "main"],
                    cwd=self.config.implementation_dir,
                    check=True,
                    capture_output=True,
                )
                # Create initial commit so worktrees and merges work
                subprocess.run(
                    ["git", "commit", "--allow-empty", "-m", "Initial commit"],
                    cwd=self.config.implementation_dir,
                    check=True,
                    capture_output=True,
                )
                print("  ✓ Git repository initialized on main branch")
            except subprocess.CalledProcessError as e:
                print(f"  ⚠️  Git initialization failed: {e}")
        else:
            print("\n[Setup] Git repository already initialized")

        # Pre-trust the implementation directory so a harness with its
        # own per-directory trust file (claude: ``~/.claude.json``)
        # does not show the "Do you trust this folder?" prompt.
        # Codex under ``--yolo`` bypasses its own trust/sandbox
        # dialogs entirely, so the pretrust step would be a wasted
        # side effect on codex-only runs — the gate is encoded as
        # ``needs_pretrust_directory`` on each harness.
        if self.harness_impl.needs_pretrust_directory:
            self._pretrust_directory(self.config.implementation_dir)

        # Create tmux session
        print("\n[Setup] Creating tmux session")
        self.create_tmux_session()

        # Phase 1: Spawn project creator
        print("\n[Phase 1] Creating Project")
        self.spawn_project_creator()

        # Wait for project creation
        # (create_project is synchronous - when project_info.json exists,
        #  all subtasks are already created)
        if not wait_for_project_info(self.config):
            return False

        # Read project info
        with open(self.config.project_info_file, "r") as f:
            project_info = json.load(f)
            project_id = project_info.get("project_id")
            board_id = project_info.get("board_id")
            tasks_created = project_info.get("tasks_created", "unknown")

        print(f"  Project ID: {project_id}")
        print(f"  Board ID: {board_id}")
        print(f"  Tasks Created: {tasks_created}")

        # The control loop below sizes the agent pool dynamically from
        # Marcus's get_desired_agent_count, so there is no fixed agent
        # count to compute up front. max_cap is an optional ceiling
        # (config.yaml ``max_agents``); None means size the pool to each
        # DAG layer's full width — the default.
        max_cap = self.config.max_agents

        # Phase 2: Control loop — layered ephemeral spawning (#595 Fix 3)
        #
        # The runner is now a long-lived, spawn-only controller. Each
        # cycle it polls Marcus and spawns ephemeral agents — each does
        # exactly one task and exits — to match the active DAG layer's
        # width. It never retires (agents self-terminate) and it has
        # absorbed the old monitor agent's job (lifecycle, stall
        # watchdog, teardown). This replaces the former fixed-pool spawn
        # plus separate monitor pane.
        print("\n" + "=" * 60)
        print("[Phase 2] Control loop — layered ephemeral spawning")
        print("=" * 60)

        # Runner log file. The control loop is a long-lived daemon whose
        # stdout the caller often cannot see (it is run backgrounded).
        # Mirror every progress line to logs/runner.log, line-buffered,
        # so the run can be followed live with `tail -f`.
        log_path = self.config.experiment_dir / "logs" / "runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        runner_log = open(  # noqa: SIM115 - closed in the finally below
            log_path, "a", buffering=1, encoding="utf-8"
        )

        def _emit(message: str) -> None:
            """Print a control-loop line and mirror it to runner.log."""
            line = f"[{time.strftime('%H:%M:%S')}] {message}"
            print(line)
            runner_log.write(line + "\n")

        _emit(f"project {project_id} — {tasks_created} tasks created")
        _emit(f"watch agents:  tmux attach -t {self.tmux_session}")
        _emit(f"watch loop:    tail -f {log_path}")
        _emit(f"kill:          tmux kill-session -t {self.tmux_session}")

        mcp = MarcusMCPClient(marcus_url=self.marcus_mcp_url)
        if not mcp.connect():
            _emit("ERROR: could not connect to Marcus MCP — aborting run.")
            runner_log.close()
            return False

        poll_seconds = self.config.get_timeout("control_poll_seconds", 30)
        stall_minutes = self.config.stall_timeout_minutes
        stall_polls = (
            max(1, (stall_minutes * 60) // poll_seconds) if stall_minutes > 0 else 0
        )
        watchdog = StallWatchdog(stall_polls=stall_polls)
        thrash_polls = self.config.spawn_thrash_polls
        thrash_detector = SpawnThrashDetector(thrash_polls=thrash_polls)
        _emit(
            f"poll every {poll_seconds}s; stall watchdog at "
            f"{stall_minutes} min ({stall_polls} unchanged polls); "
            f"spawn-thrash fast-fail at {thrash_polls} polls "
            f"(~{(thrash_polls * poll_seconds) // 60} min)"
        )

        worker_pane_ids: List[str] = []
        spawned_total = 0
        # Latch: True once is_running has been observed True at least
        # once. Until then a not-running poll means the monitor is
        # still spinning up, not that the run finished (#595 fix —
        # Marcus sets experiment_started before is_running).
        seen_running = False
        # Warn once if the task graph is near-sequential — multi-agent
        # coordination buys little parallelism on a narrow DAG (#595).
        warned_narrow = False
        templates = self.config.agents or [
            {
                "id": "agent_unicorn",
                "name": "Unicorn Developer",
                "role": "full-stack",
                "skills": ["python", "javascript"],
                "subagents": 0,
            }
        ]

        try:
            while True:
                try:
                    status = mcp.call_tool("get_experiment_status") or {}
                    is_running = bool(status.get("is_running", False))
                    if is_running:
                        seen_running = True
                    state = experiment_lifecycle_state(
                        bool(status.get("experiment_started", False)),
                        is_running,
                        seen_running,
                    )

                    if state == "waiting":
                        _emit("waiting for experiment to start...")
                        time.sleep(10)
                        continue

                    if state == "finished":
                        _emit("experiment finished — tearing down")
                        self._teardown("complete")
                        break

                    # state == "running"
                    live = self._count_live_worker_panes(worker_pane_ids)
                    # Omit max_agents entirely when uncapped (None) so the
                    # tool sizes the pool to each layer's full width.
                    desired_args = {} if max_cap is None else {"max_agents": max_cap}
                    signal = (
                        mcp.call_tool("get_desired_agent_count", desired_args) or {}
                    )
                    desired = int(signal.get("desired_agent_count", 0))
                    unclaimed = int(signal.get("unclaimed_tasks", 0))
                    # #632: spawn decision uses Marcus's IN_PROGRESS count
                    # (the right "who is covering an unclaimed task" signal
                    # under the ephemeral lifecycle), not the runner's
                    # tmux-pane count. The pane count is still computed
                    # above for teardown logic and stall reasoning.
                    in_flight = int(signal.get("in_flight_tasks", 0))
                    to_spawn = compute_spawn_count(desired, in_flight, unclaimed)

                    # Tell the user, once, how much parallelism the graph
                    # actually offers — and warn when it offers almost
                    # none, so coordination overhead is a known choice.
                    if not warned_narrow:
                        warned_narrow = True
                        max_width = int(signal.get("max_layer_width", 0))
                        _emit(f"DAG widest layer = {max_width} task(s)")
                        if 0 < max_width <= NARROW_DAG_WARN_WIDTH:
                            _emit(
                                f"NOTE: this task graph is near-sequential "
                                f"(widest layer {max_width}). Multi-agent "
                                f"coordination adds overhead here with "
                                f"little parallelism to gain — a single "
                                f"agent would be comparable or cheaper."
                            )

                    done = int(status.get("completed_tasks", 0))
                    total = int(status.get("total_tasks", 0))
                    in_progress = int(status.get("in_progress_tasks", 0))
                    blocked = int(status.get("blocked_tasks", 0))
                    # "Real progress" signal for the spawn-thrash detector:
                    # a monotonic, cumulative tally of the signals an agent
                    # emits while genuinely working a task — completions
                    # plus the report_task_progress heartbeat (25/50/75%)
                    # and logged work (context requests, artifacts,
                    # decisions, blockers). progress_updates is the earliest
                    # of these: it fires as soon as a claimed agent reports,
                    # well before the first artifact lands, closing the
                    # claim->first-artifact gap that would otherwise let the
                    # detector fast-fail a healthy run. It rises on real work
                    # and stays flat on claim-and-exit churn; unlike
                    # in_progress it never flickers back down, so it cannot
                    # mask thrash. See SpawnThrashDetector.observe.
                    activity = (
                        done
                        + int(status.get("progress_updates", 0))
                        + int(status.get("context_requests", 0))
                        + int(status.get("artifacts_created", 0))
                        + int(status.get("decisions_logged", 0))
                        + int(status.get("blockers_reported", 0))
                    )
                    _emit(
                        f"tasks {done}/{total} done "
                        f"({in_progress} in-progress, {blocked} blocked) | "
                        f"live={live} in_flight={in_flight} "
                        f"desired={desired} unclaimed={unclaimed} "
                        f"-> spawn {to_spawn}"
                    )

                    for _ in range(to_spawn):
                        template = dict(templates[spawned_total % len(templates)])
                        base_id = str(template.get("id", "agent_unicorn"))
                        template["id"] = f"{base_id}_{spawned_total + 1}"
                        template["name"] = (
                            f"{template.get('name', 'Unicorn Developer')} "
                            f"#{spawned_total + 1}"
                        )
                        template["subagents"] = 0
                        worker_pane_ids.append(self.spawn_worker(template))
                        spawned_total += 1
                        _emit(f"  spawned {template['id']}")
                        time.sleep(0.5)

                    if watchdog.update(done, in_progress, blocked):
                        _emit(
                            f"STALL: no task-count change for "
                            f"{stall_minutes} min — tearing down"
                        )
                        self._teardown("stalled")
                        break

                    if thrash_detector.observe(activity, to_spawn):
                        _emit(
                            f"SPAWN-THRASH: spawned an agent on each of the "
                            f"last {thrash_polls} polls but saw no forward "
                            f"progress — {done} completed, {in_progress} "
                            f"in-progress, and no agent logged any work "
                            f"(get_task_context / log_artifact / "
                            f"log_decision). Agents are starting but not "
                            f"claiming or working: likely deferred MCP tools, "
                            f"a skill mismatch, or every unclaimed task gated "
                            f"by a BLOCKED dependency. Tearing down."
                        )
                        self._teardown("spawn_thrash")
                        break

                except Exception as exc:  # noqa: BLE001
                    # A transient error must not kill the controller —
                    # log it and retry next cycle (#595 crash-resilience).
                    _emit(f"WARN control cycle error: " f"{type(exc).__name__}: {exc}")

                time.sleep(poll_seconds)
        finally:
            _emit(f"run finished — spawned {spawned_total} ephemeral agents")
            runner_log.close()

        return True


def main() -> None:
    """Run the experiment spawner."""
    if len(sys.argv) < 2:
        print("Usage: python spawn_agents.py <experiment_dir>")
        print("Example: python spawn_agents.py ~/my-experiments/task-api")
        sys.exit(1)

    experiment_dir = Path(sys.argv[1]).resolve()
    config_file = experiment_dir / "config.yaml"

    if not config_file.exists():
        print(f"Error: config.yaml not found at {config_file}")
        template_path = "experiments/templates/config.yaml.template"
        print(
            f"\nCreate a config.yaml in {experiment_dir} "
            f"using the template at {template_path}"
        )
        sys.exit(1)

    # Find templates directory (relative to this script)
    script_dir = Path(__file__).parent
    templates_dir = script_dir / "templates"

    if not templates_dir.exists():
        print(f"Error: Templates directory not found at {templates_dir}")
        sys.exit(1)

    # Load config and spawn agents
    config = ExperimentConfig(config_file)
    spawner = AgentSpawner(config, templates_dir)
    success = spawner.run()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
