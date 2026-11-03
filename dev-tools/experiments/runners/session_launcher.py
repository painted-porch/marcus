"""The session launcher: start N workers and get out of the way.

Epic #706 Phase 3 Milestone A, launcher design piece 1
(``docs/design/session-launcher-design.md``). The old runner spawned one
agent per task and killed it afterwards, then layered a spawn loop, a stall
watchdog and a thrash detector on top. Several run-killers lived in that
machinery. The launcher deletes it instead of fixing it:

- it creates the project itself over MCP (no project-creator agent, no
  ``start_experiment``; "a run" is the launcher's business, ADR-0012 D10),
- it writes the one worker contract (``prompts/Agent_prompt.md``) into every
  workspace's instruction files and hands each chore a short session header
  that points at it,
- it starts N **sessions**, each a persistent identity in its own worktree
  (``worktrees/<agent_id>``, the path shape Cato's cost ingestion reads),
- each session's shell script loops **one chore per harness invocation**:
  the worker registers (idempotently), takes one task, does it, reports it,
  exits, and the script invokes it again with a fresh context. That loop is
  the per-chore context reset the design calls O5 at minimum.
- each chore gets a fresh worktree and branch cut from ``main`` (same path,
  so cost attribution keeps working), and the session script removes its
  worktree when the session goes home or is killed (#628),
- it never restarts a dead worker and has no watchdog; it waits for every
  session to go home, which a worker does when ``request_next_task``
  returns ``BOARD_DRAINED`` and it writes ``.marcus_session_done``,
- it prints the board's final counts and tears the tmux session down.

Everything tmux, git and harness-command related lives in
:class:`runners.worker_host.WorkerHost`; this module adds the session
contract and the small loop around it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from runners.harness import HARNESSES
from runners.marcus_client import MarcusMCPClient
from runners.worker_host import WorkerHost, check_mcp_health

SESSION_DONE_MARKER = ".marcus_session_done"
# Written by the worker when it no longer holds a task (completion accepted
# or task handed back). The script then resets the branch to main before
# the next chore, so a handed-back task's commits never ride into main with
# the next task's merge (Codex P1 on PR #779; the #730 hazard).
CHORE_DONE_MARKER = ".marcus_chore_done"
# Written by the worker when request_next_task returned no task: it holds
# the retry_after_seconds. The script sleeps that long before the next
# chore. The wait lives here because a harness may refuse a bare ``sleep``
# in the model's shell (Claude Code blocks standalone sleeps), which turned
# the retry into a two-second poll in a live run.
RETRY_MARKER = ".marcus_retry_after"
MAX_RETRY_WAIT_SECONDS = 300
DEFAULT_SESSION_ROLE = "full-stack"
DEFAULT_SESSION_SKILLS = [
    "python",
    "javascript",
    "typescript",
    "testing",
    "documentation",
]
DEFAULT_MAX_CHORES = 200
CREATE_PROJECT_TIMEOUT_SECONDS = 900.0
RUNNER_LOG_NAME = "runner.log"
# The one worker contract. The session header (templates/session_prompt.md)
# adds only what is specific to a chore and points workers at this file,
# which the launcher writes into every workspace as CLAUDE.md / AGENTS.md /
# GEMINI.md (epic #706, "one instruction sheet").
CANONICAL_PROMPT = Path(__file__).resolve().parents[3] / "prompts" / "Agent_prompt.md"
RUN_PATH_LABEL = "marcus"


def plan_sessions(
    config_agents: List[Dict[str, Any]], count: int
) -> List[Dict[str, Any]]:
    """Return the identities for ``count`` sessions.

    The first ``count`` agents from ``config.yaml`` are used as written
    (their ``subagents`` key is dropped: sessions have no phantom
    subagents); when the config lists fewer, generated identities fill the
    rest.

    Parameters
    ----------
    config_agents : List[Dict[str, Any]]
        The ``agents`` list from ``config.yaml``.
    count : int
        How many sessions to start.

    Returns
    -------
    List[Dict[str, Any]]
        One dict per session with ``id``, ``name``, ``role``, ``skills``.
    """
    if count < 1:
        raise ValueError("sessions must be at least 1")
    planned: List[Dict[str, Any]] = []
    for agent in config_agents[:count]:
        planned.append(
            {
                "id": agent["id"],
                "name": agent.get("name", agent["id"]),
                "role": agent.get("role", DEFAULT_SESSION_ROLE),
                "skills": list(agent.get("skills") or DEFAULT_SESSION_SKILLS),
            }
        )
    for index in range(len(planned) + 1, count + 1):
        planned.append(
            {
                "id": f"session_{index}",
                "name": f"Session {index}",
                "role": DEFAULT_SESSION_ROLE,
                "skills": list(DEFAULT_SESSION_SKILLS),
            }
        )
    return planned


def build_create_project_options(
    project_options: Dict[str, Any], implementation_dir: Path, project_info_file: Path
) -> Dict[str, Any]:
    """Return the ``create_project`` options for a session run.

    ``project_root`` is the implementation directory (where the smoke gate
    runs and where workers' branches merge), ``project_info_path`` makes
    Marcus write ``project_info.json`` itself, and ``path`` labels the run's
    entry point in the cost store (``runs.path``) unless the config already
    says otherwise.

    Parameters
    ----------
    project_options : Dict[str, Any]
        ``project_options`` from ``config.yaml``.
    implementation_dir : Path
        The experiment's implementation directory.
    project_info_file : Path
        Where ``project_info.json`` must land.

    Returns
    -------
    Dict[str, Any]
        The options dict to send.
    """
    options = dict(project_options or {})
    options.setdefault("provider", "sqlite")
    options.setdefault("path", RUN_PATH_LABEL)
    options["project_root"] = str(implementation_dir)
    options["project_info_path"] = str(project_info_file)
    return options


def render_session_prompt(
    agent: Dict[str, Any], workspace: Path, branch: str, template_text: str
) -> str:
    """Fill the session contract for one worker.

    Parameters
    ----------
    agent : Dict[str, Any]
        A planned session identity.
    workspace : Path
        The session's worktree.
    branch : str
        The session's git branch.
    template_text : str
        The contents of ``templates/session_prompt.md``.

    Returns
    -------
    str
        The prompt piped to the harness on every chore.
    """
    return template_text.format(
        agent_id=agent["id"],
        agent_name=agent["name"],
        agent_role=agent["role"],
        agent_skills=json.dumps(agent["skills"]),
        workspace=str(workspace),
        branch=branch,
    )


def build_session_script(
    *,
    agent: Dict[str, Any],
    workspace: Path,
    branch: str,
    repo: Path,
    project_info_file: Path,
    marcus_mcp_url: str,
    register_snippet: str,
    chore_command: str,
    max_chores: int = DEFAULT_MAX_CHORES,
) -> str:
    """Return the bash script that runs one session until it goes home.

    Each loop iteration is one chore. A chore gets a workspace of its own:
    when the previous chore ended with the worker no longer holding a task
    (it wrote ``CHORE_DONE_MARKER``), the worktree is removed and created
    again from ``main`` on a fresh branch of the same name, so a branch holds
    exactly one chore's commits and nothing from a handed-back task can ride
    into ``main`` with the next one (#730). A chore that ended mid-task (no
    marker) keeps its workspace so the worker resumes. The workspace path
    (``worktrees/<agent_id>``) never changes, because cost attribution reads
    it.

    The script owns teardown (#628): an exit trap removes the worktree when
    the session goes home, hits the chore cap, or is killed, so no run leaves
    orphaned worktrees behind. The branch is kept: Marcus's recovery path
    reads a dead worker's branch so the next agent can inherit its commits.
    There is no restart of a crashed harness beyond the next iteration and
    no watchdog.

    Parameters
    ----------
    agent : Dict[str, Any]
        The session identity.
    workspace : Path
        The session's worktree (``worktrees/<agent_id>``).
    branch : str
        The session's branch.
    repo : Path
        The implementation repository the worktree hangs off.
    project_info_file : Path
        The experiment's ``project_info.json``, copied into the workspace.
    marcus_mcp_url : str
        The Marcus MCP endpoint, exported for the harness.
    register_snippet : str
        The harness's MCP registration one-liner.
    chore_command : str
        The single-shot harness command for one chore.
    max_chores : int
        Upper bound on iterations.

    Returns
    -------
    str
        The script text.
    """
    marker = workspace / SESSION_DONE_MARKER
    chore_marker = workspace / CHORE_DONE_MARKER
    retry_marker = workspace / RETRY_MARKER
    sid = agent["id"]
    return f"""#!/bin/bash
[ -f ~/.zshrc ] && source ~/.zshrc
[ -f ~/.bashrc ] && source ~/.bashrc
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION
if [ "$TERM" = "dumb" ] || [ -z "$TERM" ]; then
    export TERM=xterm-256color
fi
export GIT_AUTHOR_NAME="{agent["name"]}"
export GIT_AUTHOR_EMAIL="{sid}@marcus.ai"
export GIT_COMMITTER_NAME="{agent["name"]}"
export GIT_COMMITTER_EMAIL="{sid}@marcus.ai"
REPO="{repo}"
WORKSPACE="{workspace}"
BRANCH="{branch}"

# Teardown is the harness's job, not Marcus's (#628): remove this session's
# worktree when the session goes home, hits the chore cap, or is killed.
# The branch stays so a dead worker's commits remain reachable for the next
# agent (Marcus's recovery path reads it).
# Job control gives each chore its own process group, so a kill of this
# session can stop the harness AND everything it started (test runners,
# dev servers) before the workspace is removed from under them.
set -m
CHORE_PID=""
# Stop everything left in the chore's process group: the harness itself on
# a kill, or children it started and did not stop (a dev server, a test
# watcher) after it exited normally. Waiting on the harness PID never waits
# for its descendants (Codex P2 x2 on PR #784).
reap_chore() {{
    [ -n "$CHORE_PID" ] || return 0
    kill -TERM -- "-$CHORE_PID" >/dev/null 2>&1 \
        || kill -TERM "$CHORE_PID" >/dev/null 2>&1 || true
    sleep 1
    kill -KILL -- "-$CHORE_PID" >/dev/null 2>&1 || true
    wait "$CHORE_PID" 2>/dev/null
    CHORE_PID=""
}}
cleanup() {{
    trap - EXIT HUP TERM INT
    reap_chore
    cd "$REPO" 2>/dev/null || exit 0
    git worktree remove --force "$WORKSPACE" >/dev/null 2>&1 || true
    git worktree prune >/dev/null 2>&1 || true
    echo "[session {sid}] worktree removed: $WORKSPACE"
}}
on_signal() {{
    echo "[session {sid}] killed, tearing down"
    cleanup
    exit 143
}}
trap cleanup EXIT
trap on_signal HUP TERM INT

# A fresh workspace for a fresh chore: the same path, a new worktree, and a
# new branch of the same name cut from main.
fresh_workspace() {{
    cd "$REPO" || exit 1
    git worktree remove --force "$WORKSPACE" >/dev/null 2>&1 || true
    git worktree prune >/dev/null 2>&1 || true
    git branch -D "$BRANCH" >/dev/null 2>&1 || true
    git worktree add "$WORKSPACE" -b "$BRANCH" main >/dev/null 2>&1 || exit 1
    cd "$WORKSPACE" || exit 1
    cp {project_info_file} ./project_info.json
}}

cd "$WORKSPACE" || exit 1
rm -f {marker} {chore_marker} {retry_marker}
echo "=========================================="
echo "SESSION {agent["name"].upper()}"
echo "ID: {sid}"
echo "Branch: {branch} (isolated worktree)"
echo "Working Directory: $(pwd)"
echo "=========================================="
cp {project_info_file} ./project_info.json
MARCUS_MCP_URL="{marcus_mcp_url}"
{register_snippet}
chore=0
while true; do
    chore=$((chore + 1))
    echo ""
    echo "[session {sid}] chore $chore"
    if [ -f {chore_marker} ]; then
        # The previous chore's task was accepted (already merged) or handed
        # back (must not ride into main): this chore gets a fresh workspace.
        fresh_workspace
        echo "[session {sid}] fresh worktree from main for a new chore"
    fi
    git merge main --no-edit 2>/dev/null || true
    # The harness runs as a background job we wait on: a kill of this
    # session is handled at once (bash defers traps while a foreground
    # command runs), and with job control on the job is its own process
    # group, so the trap can stop the harness and its children before
    # removing the worktree.
    {chore_command} &
    CHORE_PID=$!
    wait "$CHORE_PID"
    rc=$?
    reap_chore
    echo "[session {sid}] chore $chore ended rc=$rc"
    if [ -f {marker} ]; then
        echo "[session {sid}] board drained, going home"
        break
    fi
    if [ $chore -ge {max_chores} ]; then
        echo "[session {sid}] chore cap {max_chores} reached, stopping"
        break
    fi
    # No task this chore: the worker left the server's retry_after_seconds
    # in the retry marker and exited; wait here, outside the model.
    if [ -f {retry_marker} ]; then
        wait_s=$(tr -dc '0-9' < {retry_marker} | head -c 6)
        rm -f {retry_marker}
        [ -z "$wait_s" ] && wait_s=30
        [ "$wait_s" -gt {MAX_RETRY_WAIT_SECONDS} ] && wait_s={MAX_RETRY_WAIT_SECONDS}
        echo "[session {sid}] no task; waiting ${{wait_s}}s before the next chore"
        sleep "$wait_s"
        continue
    fi
    sleep 2
done
echo "=========================================="
echo "{agent["name"]} - session over"
echo "=========================================="
"""


def format_unfinished_report(
    unfinished: Optional[List[Dict[str, Any]]], total: int
) -> List[str]:
    """Render the end-of-run statement of what did and did not get built.

    Counts alone let a run end looking successful while a deliverable is
    silently missing. Missing data must never read as success either:
    ``unfinished`` is None when Marcus could not produce a trustworthy task
    list, and a zero total says nothing about whether work was done.

    Parameters
    ----------
    unfinished : Optional[List[Dict[str, Any]]]
        ``{"id", "name", "status", "reason"}`` per task that did not reach
        DONE, as ``get_project_status`` reports under ``unfinished_tasks``.
    total : int
        Total task count for the project.

    Returns
    -------
    List[str]
        Lines to emit: one confirmation line for a clean run; otherwise every
        unfinished task, why it stopped, and the consequence.
    """
    if unfinished is None:
        return [
            "RESULT: could not verify completion; Marcus returned no "
            "trustworthy task list (board unreachable?). Check the board "
            "before treating this run as successful."
        ]
    if total <= 0:
        return [
            "RESULT: could not verify completion; no tasks reported for "
            "this project. Check the board before treating this run as "
            "successful."
        ]
    done = total - len(unfinished)
    if not unfinished:
        return [f"RESULT: all {done}/{total} tasks complete"]
    lines = [
        f"RESULT: {done}/{total} tasks complete; "
        f"{len(unfinished)} task(s) did NOT complete:"
    ]
    for task in unfinished:
        lines.append(
            f"  - [{task.get('status', '?')}] {task.get('name', '?')} "
            f"({str(task.get('id', ''))[:8]}): {task.get('reason', 'unknown')}"
        )
    lines.append(
        "  Whatever these tasks were meant to produce may be missing from "
        "the deliverable; check the board and the product before treating "
        "this run as successful."
    )
    return lines


class SessionLauncher:
    """Start N sessions for one experiment and wait for them to go home.

    Parameters
    ----------
    config : Any
        An ``ExperimentConfig`` (or a stand-in with the same attributes:
        ``project_name``, ``project_spec_file``, ``project_options``,
        ``agents``, ``experiment_dir``, ``implementation_dir``,
        ``prompts_dir``, ``project_info_file``).
    templates_dir : Path
        The experiments ``templates`` directory.
    sessions : int
        How many sessions to start.
    epictetus : bool
        Keep the tmux session alive after the run.
    agent_model : Optional[str]
        Model override passed to the harness.
    harness : Optional[str]
        Harness name override.
    max_chores : int
        Per-session chore cap.
    wait : bool
        Wait for every session to exit before returning.
    poll_seconds : int
        How often to check on the sessions while waiting.
    host : Optional[WorkerHost]
        Injected for tests; built from ``config`` otherwise.
    mcp_factory : Optional[Callable[[float], Any]]
        Returns an MCP client for a timeout; injected for tests.
    sleep : Callable[[float], None]
        Injected for tests.
    contract_path : Optional[Path]
        The worker contract written into the instruction files; defaults to
        ``prompts/Agent_prompt.md`` in the Marcus checkout.
    """

    def __init__(
        self,
        config: Any,
        templates_dir: Path,
        sessions: int,
        *,
        epictetus: bool = False,
        agent_model: Optional[str] = None,
        harness: Optional[str] = None,
        max_chores: int = DEFAULT_MAX_CHORES,
        wait: bool = True,
        poll_seconds: int = 15,
        host: Optional[Any] = None,
        mcp_factory: Optional[Callable[[float], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        contract_path: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.templates_dir = Path(templates_dir)
        self.sessions = sessions
        self.epictetus = epictetus
        self.max_chores = max_chores
        self.wait = wait
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self.host = host or WorkerHost(config, agent_model=agent_model, harness=harness)
        self.session_template = self.templates_dir / "session_prompt.md"
        self.contract_path = Path(contract_path) if contract_path else CANONICAL_PROMPT
        self._mcp_factory = mcp_factory or (
            lambda timeout: MarcusMCPClient(self.host.marcus_mcp_url, timeout=timeout)
        )
        self.project_info: Dict[str, Any] = {}
        self.runner_log = Path(self.config.experiment_dir) / "logs" / RUNNER_LOG_NAME

    def say(self, message: str) -> None:
        """Print a progress line and mirror it to ``logs/runner.log``.

        The launcher is usually run in the background, so its stdout is not
        in front of the operator; the log file is what ``tail -f`` follows.

        Parameters
        ----------
        message : str
            The line to emit.
        """
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line)
        try:
            self.runner_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.runner_log, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:  # the log is an aid, never the run
            print(f"  (runner.log unavailable: {exc})")

    # ---------------------------------------------------------------- setup

    def preflight(self) -> bool:
        """Check the harness binary and the Marcus endpoint before anything starts."""
        binary = getattr(self.host.harness_impl, "binary", self.host.harness)
        if shutil.which(binary) is None:
            print(f"Error: harness CLI {binary!r} is not on PATH")
            for line in self.host.harness_impl.install_hint(self.host.marcus_mcp_url):
                print(line)
            return False
        if not check_mcp_health(self.host.marcus_mcp_url):
            print(f"Error: Marcus MCP is not reachable at {self.host.marcus_mcp_url}")
            return False
        return True

    def init_git(self) -> None:
        """Initialise the implementation repository on ``main`` with a commit.

        ``--init`` runs ``git init`` but commits nothing, and ``git worktree
        add -b`` needs a commit to branch from. The old flow got one from
        the project-creator agent; the launcher makes it itself.
        """
        repo = self.config.implementation_dir
        if not (repo / ".git").exists():
            print("[Setup] Initializing git repository...")
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "checkout", "-b", "main"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode != 0:
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "Initial commit"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            print("  Git repository initialized on main with an initial commit")
        else:
            print("[Setup] Git repository already initialized")

    def wait_for_scaffold(self, timeout_seconds: float = 180.0) -> bool:
        """Wait until Marcus's background scaffold generation has settled.

        ``create_project`` returns before the design artifacts and the
        scaffold are written; Marcus generates them in the background and
        commits them to ``main`` itself. Cutting worktrees before that
        lands gives sessions an empty tree (or fails on an unborn HEAD).
        The repository counts as settled when it has a commit, no
        ``index.lock``, and an unchanged ``git status`` across two polls.

        Parameters
        ----------
        timeout_seconds : float
            Give up waiting after this long; the run proceeds regardless.

        Returns
        -------
        bool
            True when the repository settled before the timeout.
        """
        repo = Path(self.config.implementation_dir)
        deadline = time.monotonic() + timeout_seconds
        previous: Optional[str] = None
        print("[Project] waiting for Marcus to finish writing the scaffold...")
        while time.monotonic() < deadline:
            head_ok = (
                subprocess.run(
                    ["git", "rev-parse", "--verify", "HEAD"],
                    cwd=repo,
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            locked = (repo / ".git" / "index.lock").exists()
            if head_ok and not locked and previous is not None and status == previous:
                print("  Scaffold settled on main")
                return True
            previous = status
            self.sleep(2)
        print("  Scaffold wait timed out; continuing with what is on main")
        return False

    def commit_scaffold(self) -> None:
        """Commit whatever ``create_project`` wrote (design docs, scaffold, contract).

        Sessions branch from ``main`` and merge it before every chore, so the
        project's starting files must be on ``main`` before the first
        worktree is cut. The creator agent used to make this commit.
        """
        repo = self.config.implementation_dir
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        committed = subprocess.run(
            ["git", "commit", "-q", "-m", "Initial commit: Marcus project created"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if committed.returncode == 0:
            print("  Project scaffold committed on main")
        else:
            print("  Nothing new to commit on main")

    def write_instruction_files(self) -> List[Path]:
        """Write the worker contract into every harness's instruction file.

        The launcher keeps this one job of the old runner: Codex and Gemini
        workers read ``AGENTS.md`` / ``GEMINI.md`` from the directory they
        run in (Claude reads ``CLAUDE.md``), and a worker that finds no
        instructions silently does nothing. The file is the canonical
        ``prompts/Agent_prompt.md``, exactly what the README tells operators
        to copy; the per-chore prompt is only a header that points at it.
        The files are committed with the scaffold, so every worktree has
        them.
        """
        contract = self.contract_path.read_text()
        filenames: set[str] = set()
        for impl in HARNESSES.values():
            filenames.update(impl.workflow_files)
        written: List[Path] = []
        for filename in sorted(filenames):
            target = self.config.implementation_dir / filename
            target.write_text(contract)
            written.append(target)
            print(f"  Session contract written to {target}")
        return written

    # -------------------------------------------------------------- project

    def create_project(self) -> bool:
        """Create the project over MCP and make sure ``project_info.json`` exists.

        Returns
        -------
        bool
            True when the project exists and its info file is on disk.
        """
        get_timeout = getattr(self.config, "get_timeout", None)
        timeout = float(
            get_timeout("project_creation", int(CREATE_PROJECT_TIMEOUT_SECONDS))
            if callable(get_timeout)
            else CREATE_PROJECT_TIMEOUT_SECONDS
        )
        mcp = self._mcp_factory(timeout)
        if not mcp.connect():
            print("Error: could not open an MCP session with Marcus")
            return False
        description = Path(self.config.project_spec_file).read_text()
        options = build_create_project_options(
            self.config.project_options,
            self.config.implementation_dir,
            self.config.project_info_file,
        )
        print(
            f"[Project] create_project '{self.config.project_name}' "
            "(this takes a minute)..."
        )
        result = mcp.call_tool(
            "create_project",
            {
                "description": description,
                "project_name": self.config.project_name,
                "options": options,
            },
        )
        if not result or not result.get("success"):
            print(f"Error: create_project failed: {result}")
            return False
        self.project_info = {
            "project_id": result.get("project_id", ""),
            "board_id": (result.get("board") or {}).get(
                "board_id", result.get("board_id", "")
            ),
            "tasks_created": result.get("tasks_created", 0),
            "recommended_agents": result.get("recommended_agents", 0),
        }
        info_file = Path(self.config.project_info_file)
        if not info_file.exists():
            info_file.parent.mkdir(parents=True, exist_ok=True)
            info_file.write_text(json.dumps(self.project_info, indent=2))
        print(
            f"  Project {self.project_info['project_id']} created: "
            f"{self.project_info['tasks_created']} tasks"
        )
        return True

    # ------------------------------------------------------------- sessions

    def launch_session(self, agent: Dict[str, Any]) -> str:
        """Create the session's worktree, prompt and script, and start it.

        Parameters
        ----------
        agent : Dict[str, Any]
            A planned session identity.

        Returns
        -------
        str
            The tmux pane id running the session.
        """
        branch = f"marcus/{agent['id']}"
        workspace = Path(self.config.experiment_dir) / "worktrees" / agent["id"]
        self.host.create_worktree(agent["id"], branch, workspace)
        if self.host.harness_impl.needs_pretrust_directory:
            self.host.pretrust_directory(workspace)
        prompt = render_session_prompt(
            agent, workspace, branch, self.session_template.read_text()
        )
        prompt_file = Path(self.config.prompts_dir) / f"{agent['id']}.txt"
        prompt_file.write_text(prompt)
        script = build_session_script(
            agent=agent,
            workspace=workspace,
            branch=branch,
            repo=Path(self.config.implementation_dir),
            project_info_file=Path(self.config.project_info_file),
            marcus_mcp_url=self.host.marcus_mcp_url,
            register_snippet=self.host.build_mcp_register_snippet(),
            chore_command=self.host.build_agent_command(
                workspace, prompt_file, print_mode=True
            ),
            max_chores=self.max_chores,
        )
        script_file = Path(self.config.prompts_dir) / f"{agent['id']}.sh"
        script_file.write_text(script)
        script_file.chmod(0o755)
        pane_id = self.host.start_window(script_file, agent["name"])
        self.say(f"Session {agent['id']} started in pane {pane_id}")
        return pane_id

    def board_summary(self) -> Dict[str, Any]:
        """Return the board's status counts from ``get_project_status``, best effort."""
        try:
            mcp = self._mcp_factory(30.0)
            if not mcp.connect():
                return {}
            return mcp.call_tool("get_project_status") or {}
        except Exception:  # noqa: BLE001 - a summary must never fail the run
            return {}

    def final_report(self) -> None:
        """Read the board once and say what did and did not get built."""
        summary = self.board_summary()
        project = summary.get("project") if isinstance(summary, dict) else None
        if not isinstance(project, dict):
            for line in format_unfinished_report(None, total=0):
                self.say(line)
            return
        counts = {
            k: project.get(k)
            for k in ("total_tasks", "completed", "in_progress", "blocked")
        }
        self.say(f"[Board] {json.dumps(counts, default=str)}")
        for line in format_unfinished_report(
            project.get("unfinished_tasks"), total=int(project.get("total_tasks") or 0)
        ):
            self.say(line)

    def wait_for_sessions(self, pane_ids: List[str]) -> None:
        """Block until every session pane has exited, reporting now and then."""
        while True:
            alive = self.host.count_live_panes(pane_ids)
            if alive == 0:
                return
            self.say(f"[Sessions] {alive} of {len(pane_ids)} still working")
            self.sleep(self.poll_seconds)

    def teardown(self) -> None:
        """Kill the tmux session unless the run is kept for inspection."""
        if self.epictetus:
            self.say(
                "Epictetus mode: tmux session kept alive: "
                f"tmux attach -t {self.host.tmux_session}"
            )
            return
        self.host.kill_session()
        self.say(f"Tmux session '{self.host.tmux_session}' terminated.")

    # ------------------------------------------------------------------ run

    def run(self) -> bool:
        """Create the project, start the sessions, wait for them, summarise.

        Returns
        -------
        bool
            True when the project was created and the sessions started.
        """
        print("=" * 60)
        print("Marcus session run")
        print("=" * 60)
        print(f"Experiment: {self.config.experiment_dir}")
        print(f"Project: {self.config.project_name}")
        print(f"Sessions: {self.sessions}  Harness: {self.host.harness}")
        if not self.preflight():
            return False
        self.init_git()
        self.write_instruction_files()
        if Path(self.config.project_info_file).exists():
            Path(self.config.project_info_file).unlink()
        if not self.create_project():
            return False
        self.wait_for_scaffold()
        self.commit_scaffold()
        self.host.create_tmux_session()
        if self.host.harness_impl.needs_pretrust_directory:
            self.host.pretrust_directory(Path(self.config.implementation_dir))
        pane_ids = [
            self.launch_session(agent)
            for agent in plan_sessions(self.config.agents, self.sessions)
        ]
        self.say(
            f"[Sessions] {len(pane_ids)} started; attach with: "
            f"tmux attach -t {self.host.tmux_session}"
        )
        self.say(f"[Sessions] follow this log with: tail -f {self.runner_log}")
        if not self.wait:
            return True
        self.wait_for_sessions(pane_ids)
        self.final_report()
        self.say("[Sessions] all sessions have gone home")
        self.teardown()
        return True
