"""
Per-CLI harness definitions for the Marcus experiment runner.

A *harness* is the agent CLI a spawned tmux pane runs — currently
``claude`` (Anthropic's Claude Code), ``codex`` (OpenAI's Codex CLI),
or ``gemini`` (Google's Gemini CLI).  All three speak the same Marcus
MCP protocol, but the surface around them — the exact shell command,
the worker-keepalive wrapper, the trust-dialog handling, the
workflow-file convention, and the install hint shown on a missing
binary — differs.

Before this module landed, ``AgentSpawner`` had six ``if/elif`` sites
spread across one ~2000-line file, one per harness-specific quirk.
Adding a third harness (e.g. ``gemini``) meant editing every site.
This module collects every harness-specific behavior into a single
:class:`Harness` Protocol with one implementation per CLI, plus a
:data:`HARNESSES` registry the spawner looks up by name.

The contract is "byte-identical rendered shell scripts": the strings
returned here must be exactly what the previous ``if/elif`` blocks
returned, so the existing ``TestCodexScriptsRender`` and sibling
tests stay green without modification.

Classes
-------
Harness
    Structural protocol every harness implementation must satisfy.
ClaudeHarness
    Default harness — Anthropic Claude Code CLI.
CodexHarness
    OpenAI Codex CLI harness with ``--enable goals`` + a bounded
    bash relaunch loop.
GeminiHarness
    Google Gemini CLI harness with ``--skip-trust --yolo`` + the
    same bounded bash relaunch loop pattern (``gemini -p`` is
    fire-and-exit like ``codex exec``).

Functions
---------
get_harness(name)
    Resolve a harness by name, raising :class:`ValueError` for
    unknown names with a list of supported choices.

Data
----
HARNESSES
    Mapping of harness name -> :class:`Harness` instance.
"""

from pathlib import Path
from typing import Dict, List, Protocol, Tuple, runtime_checkable


def _bounded_relaunch_loop(
    inner_cmd: str,
    cli_name: str,
    *,
    break_on_clean_exit: bool,
    max_relaunches: int = 50,
) -> str:
    """Render a bash relaunch loop for fire-and-exit harnesses.

    Both :class:`CodexHarness` and :class:`GeminiHarness` wrap their
    per-prompt invocations in this loop so a worker whose CLI exits
    is brought back to keep polling Marcus.  The loop semantics
    differ between the two harnesses by exactly one switch:

    * **Codex** with ``--enable goals`` stays engaged across many
      model turns inside one invocation — when ``codex exec`` exits
      ``rc=0`` it genuinely means "the goal completed."  Breaking on
      ``rc=0`` is correct; relaunching would burn tokens (especially
      under ``--epictetus``).
    * **Gemini** has no goals-equivalent.  Every ``gemini -p`` cycle
      ends in ``rc=0`` after one prompt iteration.  Breaking on
      ``rc=0`` would kill the worker after a single cycle before it
      can finish a task or pick up another one — the worker MUST
      relaunch unconditionally and rely on ``MAX_RELAUNCHES`` plus
      the monitor's ``end_experiment`` tmux-kill to terminate.

    Discovered live during PR #587 validation: a gemini worker
    started a task, wrote a placeholder file, exited ``rc=0``, the
    wrapper broke, and the task sat ``in_progress`` forever.

    Parameters
    ----------
    inner_cmd : str
        The single-shot per-pane invocation produced by
        :meth:`Harness.build_agent_command`.
    cli_name : str
        Identifier surfaced in the ``[worker]`` pane-history echoes
        (``codex``, ``gemini``) so scrollback is harness-explicit.
    break_on_clean_exit : bool
        See above.  ``True`` for codex; ``False`` for gemini.
    max_relaunches : int, optional
        Loop cap.  Default 50.

    Returns
    -------
    str
        A multi-line bash block ready to be embedded in the worker
        shell script.
    """
    if break_on_clean_exit:
        clean_exit_branch = (
            "  if [ $rc -eq 0 ]; then\n"
            f'    echo "[worker] {cli_name} finished cleanly (rc=0);'
            ' not relaunching"\n'
            "    break\n"
            "  fi\n"
        )
    else:
        clean_exit_branch = ""
    return (
        f"MAX_RELAUNCHES={max_relaunches}\n"
        "for relaunch in $(seq 1 $MAX_RELAUNCHES); do\n"
        f'  echo "[worker] {cli_name} launch $relaunch/$MAX_RELAUNCHES"\n'
        f"  {inner_cmd}\n"
        "  rc=$?\n"
        f"{clean_exit_branch}"
        f'  echo "[worker] {cli_name} exited rc=$rc;'
        ' relaunching in 3s..."\n'
        "  sleep 3\n"
        "done\n"
        'echo "[worker] hit MAX_RELAUNCHES=$MAX_RELAUNCHES — giving up"'
    )


@runtime_checkable
class Harness(Protocol):
    """Structural protocol every per-CLI harness implementation satisfies.

    Attributes
    ----------
    name : str
        Stable identifier the runner stores on ``config.harness`` and
        the user picks on the command line (``--harness <name>``).
    binary : str
        Executable name looked up on PATH during the spawner
        pre-flight.
    provider : str
        Cost-tracking provider label (``"anthropic"`` / ``"openai"``).
        Reserved for the cost ingester work tracked in issue #582 —
        ``src/cost_tracking/worker_ingester.py`` writes
        ``provider="anthropic"`` today; once #582 lands the codex
        ingester will read this field rather than hardcoding a
        provider string per code path.
    needs_trust_dialog_poll : bool
        ``True`` when the harness raises an interactive trust dialog
        on first launch in a directory; the spawner then polls each
        pane for the prompt and confirms it.  Claude only.
    needs_pretrust_directory : bool
        ``True`` when the harness reads a per-directory trust file
        (e.g. ``~/.claude.json``) that the spawner must seed before
        launch.  Claude only.
    workflow_files : Tuple[str, ...]
        Workflow filenames the spawner copies into the implementation
        directory so the agent picks up Marcus-specific guidance.
        For codex, both ``CLAUDE.md`` and ``AGENTS.md`` are written —
        the latter is the codex-specific convention
        (https://developers.openai.com/codex/guides/agents-md).
    """

    name: str
    binary: str
    provider: str
    needs_trust_dialog_poll: bool
    needs_pretrust_directory: bool
    workflow_files: Tuple[str, ...]

    def build_agent_command(
        self,
        workdir: Path,
        prompt_file: Path,
        *,
        model_flag: str,
        print_mode: bool = False,
    ) -> str:
        """Return the per-pane shell command line that launches the agent."""
        ...

    def wrap_worker_invocation(self, inner_cmd: str) -> str:
        """Return the worker shell block: bare invocation or wrapped loop.

        ``inner_cmd`` is the pre-rendered single-shot command produced
        by :meth:`build_agent_command`.  Implementations decide whether
        to return it unchanged (claude TUI stays alive) or wrap it
        (codex relaunch loop).

        .. note::
            Resist adding knobs through ``inner_cmd`` — if a future
            harness needs different wrapping behavior, expose the
            distinction as a *structured* parameter or a new method.
            Threading semantics through the inner command string
            re-couples the strategy classes to each other.
        """
        ...

    def build_mcp_register_snippet(self) -> str:
        """Return the snippet that registers Marcus MCP from inside a pane."""
        ...

    def install_hint(self, marcus_mcp_url: str) -> List[str]:
        """Return printable lines suggesting how to install the missing CLI."""
        ...


class ClaudeHarness:
    """Harness for Anthropic's Claude Code CLI (the original Marcus harness).

    Claude is a long-lived TUI process: a single launch stays alive
    across many model turns inside the pane.  No keepalive wrapper is
    needed.  Claude raises an interactive trust dialog the first time
    it touches a directory, so the spawner polls for it and writes
    ``~/.claude.json`` ahead of time (pretrust).
    """

    name: str = "claude"
    binary: str = "claude"
    provider: str = "anthropic"
    needs_trust_dialog_poll: bool = True
    needs_pretrust_directory: bool = True
    workflow_files: Tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")

    def build_agent_command(
        self,
        workdir: Path,
        prompt_file: Path,
        *,
        model_flag: str,
        print_mode: bool = False,
    ) -> str:
        """Render the ``claude`` invocation for one pane.

        Parameters
        ----------
        workdir : Path
            Directory passed via ``--add-dir`` as the agent's writable
            workspace.
        prompt_file : Path
            File piped to claude on stdin.
        model_flag : str
            Pre-formatted ``"--model X "`` string (empty when no model
            override is set).  Pre-formatted by the caller so the
            harness does not need to know about model-resolution rules.
        print_mode : bool, optional
            When ``True``, append ``--print`` so claude exits with
            stdout flushed.  Used by the project-creator pane.

        Returns
        -------
        str
            Single shell command with backslash-newline continuations,
            matching the byte layout the prior ``if/elif`` produced.
        """
        print_flag = "--print " if print_mode else ""
        # Restrict the agent to ONLY the Marcus MCP server. Without this,
        # the pane inherits every globally-registered MCP server (Gmail,
        # Drive, Calendar, Docker, ...). That large tool surface pushes
        # claude into deferred-tool mode, where the mcp__marcus__* tools
        # are not directly available and must be loaded via ToolSearch
        # first. Smaller models (e.g. haiku) mishandle that and try to
        # invoke Marcus through nonexistent shell CLIs ("mcp call ...")
        # instead of calling the tool, so they never register and the run
        # spawn-thrashes. --strict-mcp-config narrows the session to just
        # Marcus, and "alwaysLoad": true exempts it from Tool Search
        # deferral (https://code.claude.com/docs/en/mcp) so the ~16 Marcus
        # tools load upfront and are directly callable — without it the
        # tools stay behind ToolSearch, which is exactly the step a small
        # model fumbles. $MARCUS_MCP_URL is exported by the pane wrapper
        # before this command runs.
        marcus_only_mcp = (
            r'"{\"mcpServers\":{\"marcus\":'
            r"{\"type\":\"http\",\"url\":\"$MARCUS_MCP_URL\","
            r'\"alwaysLoad\":true}}}"'
        )
        return (
            f"claude --add-dir {workdir} \\\n"
            f"  --strict-mcp-config --mcp-config {marcus_only_mcp} \\\n"
            f"  {model_flag}--dangerously-skip-permissions "
            f"{print_flag}< {prompt_file}"
        )

    def wrap_worker_invocation(self, inner_cmd: str) -> str:
        """Claude TUI stays alive across model turns — no wrapper needed."""
        return inner_cmd

    def build_mcp_register_snippet(self) -> str:
        """No-op: the agent command pins Marcus via ``--strict-mcp-config``.

        Workers and the project creator now launch with
        ``--strict-mcp-config --mcp-config <marcus-only>`` (see
        :meth:`build_agent_command`), which ignores ``~/.claude.json``
        entirely. A ``claude mcp add`` here would therefore be both
        redundant and a liability: many panes writing ``~/.claude.json``
        in parallel can corrupt it. Emit a harmless marker instead.
        """
        return 'echo "  Marcus MCP pinned via --strict-mcp-config (marcus-only)"'

    def install_hint(self, marcus_mcp_url: str) -> List[str]:
        """Single-line install hint shown when ``claude`` is missing."""
        return ["  Install Claude Code CLI then retry."]


class CodexHarness:
    """Harness for OpenAI's Codex CLI.

    ``codex exec`` is fire-and-exit by default: each invocation runs
    until the model judges the goal complete or the token budget is
    reached.  ``--enable goals`` extends that significantly (the
    agent treats the prompt as a never-ending goal and keeps polling
    Marcus across empty ``request_next_task`` cycles), but the
    invocation can still exit on edge cases — the model handing in
    ``update_goal(complete)``, a token-budget cap, an unrecoverable
    error.  A bounded bash relaunch loop wraps the invocation as a
    safety net: ``MAX_RELAUNCHES`` caps runaway token spend if
    something is genuinely wedged, and a clean ``rc=0`` exit breaks
    the loop early so a deliberately-completed run does not keep
    burning tokens (especially relevant under ``--epictetus``).
    """

    name: str = "codex"
    binary: str = "codex"
    provider: str = "openai"
    needs_trust_dialog_poll: bool = False
    needs_pretrust_directory: bool = False
    workflow_files: Tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")

    def build_agent_command(
        self,
        workdir: Path,
        prompt_file: Path,
        *,
        model_flag: str,
        print_mode: bool = False,
    ) -> str:
        """Render the ``codex exec`` invocation for one pane.

        Flags
        -----
        ``--dangerously-bypass-approvals-and-sandbox``
            Documented "YOLO mode" — approval=never,
            sandbox=danger-full-access.  Required for unattended
            tmux panes where no human can confirm approvals.
        ``--skip-git-repo-check``
            Monitor pane and similar start before any commits exist;
            this stops codex from refusing to launch on an "empty"
            repository.
        ``--disable guardian_approval``
            Turns off codex's "files changed under me without my
            doing it" safety check.  Required because Marcus workers
            run ``git merge main --no-edit`` between tasks (see
            agent_prompt.md) which guardian otherwise treats as
            anomalous and halts the agent on.
        ``--enable goals``
            Turns on codex's autonomous agentic loop.  The agent
            treats the prompt as a never-ending goal and keeps
            polling instead of wrapping up after two empty cycles.

        ``print_mode`` is ignored — ``codex exec`` is inherently
        non-interactive.

        Parameters
        ----------
        workdir : Path
            Working root passed to ``-C`` and ``--add-dir``.
        prompt_file : Path
            File piped to codex on stdin.
        model_flag : str
            Pre-formatted ``"--model X "`` string.
        print_mode : bool, optional
            Ignored.  Codex is always non-interactive.

        Returns
        -------
        str
            Single shell command with backslash-newline continuations.
        """
        del print_mode  # codex is always non-interactive
        return (
            f"codex exec --dangerously-bypass-approvals-and-sandbox "
            f"--skip-git-repo-check --disable guardian_approval "
            f"--enable goals "
            f"-C {workdir} --add-dir {workdir} \\\n"
            f"  {model_flag}< {prompt_file}"
        )

    def wrap_worker_invocation(self, inner_cmd: str) -> str:
        """Wrap ``codex exec`` in the shared bounded relaunch loop.

        Codex ``exec`` is fire-and-exit: each invocation runs until
        the model judges its goal complete or the token budget is
        reached.  The relaunch loop brings it back to keep polling
        Marcus.  See :func:`_bounded_relaunch_loop` for the loop
        semantics (``rc=0`` break, ``MAX_RELAUNCHES`` cap).
        """
        return _bounded_relaunch_loop(inner_cmd, "codex", break_on_clean_exit=True)

    def build_mcp_register_snippet(self) -> str:
        """``codex mcp add`` writes to ``~/.codex/config.toml``.

        ``|| true`` swallows the "already exists" error so re-spawned
        panes do not abort on the registration step.
        """
        return 'codex mcp add marcus --url "$MARCUS_MCP_URL" ' "2>/dev/null || true"

    def install_hint(self, marcus_mcp_url: str) -> List[str]:
        """Two-line install hint shown when ``codex`` is missing."""
        return [
            "  Install via: npm install -g @openai/codex",
            (
                "  Then register Marcus MCP: codex mcp add marcus "
                f'--url "{marcus_mcp_url}"'
            ),
        ]


class GeminiHarness:
    """Harness for Google's Gemini CLI (``@google/gemini-cli``).

    Like :class:`CodexHarness`, ``gemini -p`` is fire-and-exit: each
    invocation runs the prompt to completion and terminates.  Workers
    therefore use the same bounded bash relaunch loop pattern, with a
    clean ``rc=0`` exit breaking the loop early so a deliberately
    finished run stays quiescent under ``--epictetus``.

    Gemini exposes two distinct first-run gates:

    * **Trusted-directory gate** — interactive on first launch in a
      directory.  Bypassed per-session via the ``--skip-trust`` flag.
      The flag is in every rendered command, so the spawner does not
      need a poll fixture (``needs_trust_dialog_poll = False``) or a
      pretrust step (``needs_pretrust_directory = False``).
    * **Per-tool-call approval gate** — interactive by default.
      Bypassed by ``--yolo`` (auto-approve all tool calls), the
      documented equivalent of codex's
      ``--dangerously-bypass-approvals-and-sandbox``.

    MCP registration uses ``gemini mcp add --transport http --scope
    user`` so the marcus server lands in ``~/.gemini/settings.json``
    (user scope, matching the per-user trust files used by the other
    two harnesses).
    """

    name: str = "gemini"
    binary: str = "gemini"
    provider: str = "google"
    needs_trust_dialog_poll: bool = False
    needs_pretrust_directory: bool = False
    # ``GEMINI.md`` is Gemini's per-directory agent-instructions file
    # (analogous to ``CLAUDE.md`` for Claude Code and ``AGENTS.md`` for
    # codex).  All three are written into the implementation dir;
    # harmless when not the active harness.
    workflow_files: Tuple[str, ...] = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

    def build_agent_command(
        self,
        workdir: Path,
        prompt_file: Path,
        *,
        model_flag: str,
        print_mode: bool = False,
    ) -> str:
        """Render the ``gemini`` invocation for one pane.

        Flags
        -----
        ``--skip-trust``
            Trust the current workspace for this session.  Required
            for unattended tmux panes where no human can confirm the
            trusted-directory prompt.  Equivalent of the
            ``GEMINI_CLI_TRUST_WORKSPACE=true`` env var documented in
            the headless guide.
        ``--yolo``
            Automatically approve all tool calls.  Matches the
            documented "yolo" approval mode (``--approval-mode yolo``)
            in short form.  Required for unattended panes.
        ``--include-directories``
            Adds ``workdir`` to the agent's workspace, matching
            ``claude --add-dir`` / ``codex --add-dir``.

        Prompt delivery uses stdin (``< prompt_file``) for parity with
        the other two harnesses; gemini documents this as "Appended to
        input on stdin (if any)".  ``print_mode`` is ignored — gemini
        with ``-p``/stdin is inherently non-interactive.

        Parameters
        ----------
        workdir : Path
            Working root passed to ``--include-directories``.
        prompt_file : Path
            File piped to gemini on stdin.
        model_flag : str
            Pre-formatted ``"--model X "`` string.
        print_mode : bool, optional
            Ignored.  Gemini in headless mode is always
            non-interactive.

        Returns
        -------
        str
            Single shell command with backslash-newline continuations.
        """
        del print_mode  # gemini headless mode is always non-interactive
        # ``--include-directories`` mirrors ``claude --add-dir`` and
        # ``codex --add-dir``: only the worker's own workdir is named.
        # Shared coordination state (``project_info.json``) is
        # materialized into the workdir by the worker shell script's
        # setup phase so the agent never needs to read outside its
        # sandbox.  Peer worktrees stay invisible to the agent — a
        # nice property the other two harnesses don't enforce, but
        # parity at the architectural level.
        return (
            f"gemini --skip-trust --yolo "
            f"--include-directories {workdir} \\\n"
            f"  {model_flag}< {prompt_file}"
        )

    def wrap_worker_invocation(self, inner_cmd: str) -> str:
        """Wrap ``gemini`` in the shared bounded relaunch loop.

        Critically passes ``break_on_clean_exit=False``: ``gemini
        -p`` exits ``rc=0`` after every prompt cycle (gemini has no
        codex ``--enable goals`` equivalent that keeps it engaged
        across cycles), so the worker MUST relaunch unconditionally
        to keep polling Marcus.  Breaking on ``rc=0`` was the live-
        validation failure mode discovered on PR #587: the worker
        started a task, wrote a placeholder file, exited 0, the
        wrapper broke, and the task sat ``in_progress`` forever.
        """
        return _bounded_relaunch_loop(inner_cmd, "gemini", break_on_clean_exit=False)

    def build_mcp_register_snippet(self) -> str:
        """``gemini mcp add`` writes to ``~/.gemini/settings.json`` at user scope.

        ``--scope user`` mirrors the per-user trust storage used by
        claude (``~/.claude.json``) and codex (``~/.codex/config.toml``).
        ``--transport http`` selects HTTP MCP (gemini supports stdio,
        sse, and http transports; marcus runs over HTTP).  ``|| true``
        swallows the "already exists" error so re-spawned panes do not
        abort on the registration step.
        """
        return (
            "gemini mcp add --transport http --scope user "
            'marcus "$MARCUS_MCP_URL" 2>/dev/null || true'
        )

    def install_hint(self, marcus_mcp_url: str) -> List[str]:
        """Two-line install hint shown when ``gemini`` is missing."""
        return [
            "  Install via: npm install -g @google/gemini-cli",
            (
                "  Then register Marcus MCP: gemini mcp add "
                f'--transport http --scope user marcus "{marcus_mcp_url}"'
            ),
        ]


HARNESSES: Dict[str, Harness] = {
    ClaudeHarness.name: ClaudeHarness(),
    CodexHarness.name: CodexHarness(),
    GeminiHarness.name: GeminiHarness(),
}


def get_harness(name: str) -> Harness:
    """Look up a harness implementation by name.

    Parameters
    ----------
    name : str
        Harness identifier — must be a key in :data:`HARNESSES`.

    Returns
    -------
    Harness
        The matching implementation singleton.

    Raises
    ------
    ValueError
        When ``name`` is not a registered harness.  The error message
        lists every supported name so the user sees the right value
        immediately.
    """
    try:
        return HARNESSES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(HARNESSES))
        raise ValueError(f"Unknown harness {name!r}; supported: {choices}") from exc
