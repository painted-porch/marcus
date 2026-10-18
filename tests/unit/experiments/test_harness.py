"""
Unit tests for the per-CLI harness registry.

``dev-tools/experiments/runners/harness.py`` defines the :class:`Harness`
Protocol and one implementation per agent CLI (``ClaudeHarness``,
``CodexHarness``).  These tests pin the contract directly — the
``test_spawn_agents.py`` suite continues to cover the integration with
``AgentSpawner``.
"""

from pathlib import Path

import pytest

# ``conftest.py`` puts ``dev-tools/experiments/`` on ``sys.path``,
# making ``runners`` a normal Python package for this test subtree.
from runners import harness

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    """``HARNESSES`` + ``get_harness`` resolve names to implementations."""

    def test_registry_lists_all_supported_harnesses(self) -> None:
        """Every supported harness is registered."""
        assert set(harness.HARNESSES) == {"claude", "codex", "gemini"}

    def test_get_harness_returns_singleton(self) -> None:
        """Repeated lookups for the same name return the same object."""
        assert harness.get_harness("claude") is harness.get_harness("claude")
        assert harness.get_harness("codex") is harness.get_harness("codex")

    def test_get_harness_raises_with_choices_listed(self) -> None:
        """Unknown harness name → ValueError naming every supported choice."""
        with pytest.raises(ValueError, match="claude, codex, gemini") as exc:
            harness.get_harness("totally-fake-harness")
        # User who typed the wrong name sees the supported list — every
        # registered harness appears in the message so a future entry
        # added to ``HARNESSES`` is immediately visible.
        assert "totally-fake-harness" in str(exc.value)
        for name in ("claude", "codex", "gemini"):
            assert name in str(exc.value)


# ---------------------------------------------------------------------------
# Per-harness metadata (the cheap-to-read attributes)
# ---------------------------------------------------------------------------


class TestClaudeHarnessMetadata:
    """``ClaudeHarness`` carries claude-specific flags + provider tag."""

    def test_identity(self) -> None:
        """Name + binary are both ``"claude"``; provider is anthropic."""
        impl = harness.get_harness("claude")
        assert impl.name == "claude"
        assert impl.binary == "claude"
        assert impl.provider == "anthropic"

    def test_needs_trust_dialog_poll_true(self) -> None:
        """Claude's first-run prompt requires the spawner to poll for it."""
        assert harness.get_harness("claude").needs_trust_dialog_poll is True

    def test_needs_pretrust_directory_true(self) -> None:
        """Claude reads ~/.claude.json; pretrust seeds it."""
        assert harness.get_harness("claude").needs_pretrust_directory is True

    def test_workflow_files_include_both_conventions(self) -> None:
        """Both CLAUDE.md and AGENTS.md are written — harmless for claude,
        load-bearing for codex (and we always copy both)."""
        impl = harness.get_harness("claude")
        assert "CLAUDE.md" in impl.workflow_files
        assert "AGENTS.md" in impl.workflow_files


class TestCodexHarnessMetadata:
    """``CodexHarness`` carries codex-specific flags + provider tag."""

    def test_identity(self) -> None:
        """Name + binary are both ``"codex"``; provider is openai."""
        impl = harness.get_harness("codex")
        assert impl.name == "codex"
        assert impl.binary == "codex"
        assert impl.provider == "openai"

    def test_needs_trust_dialog_poll_false(self) -> None:
        """Codex under ``--yolo`` has no interactive trust dialog."""
        assert harness.get_harness("codex").needs_trust_dialog_poll is False

    def test_needs_pretrust_directory_false(self) -> None:
        """Codex bypasses its own per-directory trust dialog under ``--yolo``."""
        assert harness.get_harness("codex").needs_pretrust_directory is False

    def test_workflow_files_include_both_conventions(self) -> None:
        """AGENTS.md is the codex-specific convention; CLAUDE.md harmless."""
        impl = harness.get_harness("codex")
        assert "AGENTS.md" in impl.workflow_files
        assert "CLAUDE.md" in impl.workflow_files


# ---------------------------------------------------------------------------
# Rendered shell — must match the strings the old if/elif produced
# ---------------------------------------------------------------------------


class TestClaudeRenderedShell:
    """Strings the ClaudeHarness emits, asserted exactly."""

    def test_agent_command_print_mode(self, tmp_path: Path) -> None:
        """Project-creator pane uses ``--print``."""
        impl = harness.get_harness("claude")
        cmd = impl.build_agent_command(
            tmp_path / "work",
            tmp_path / "prompt.txt",
            model_flag="--model X ",
            print_mode=True,
        )
        assert "claude --add-dir" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--print" in cmd
        assert "--model X" in cmd

    def test_agent_command_non_print_mode_strips_print_flag(
        self, tmp_path: Path
    ) -> None:
        """Worker panes do not get ``--print``."""
        impl = harness.get_harness("claude")
        cmd = impl.build_agent_command(
            tmp_path / "work",
            tmp_path / "prompt.txt",
            model_flag="",
            print_mode=False,
        )
        assert "--print" not in cmd

    def test_wrap_worker_invocation_is_identity(self) -> None:
        """Claude TUI stays alive — wrapper returns the inner command verbatim."""
        impl = harness.get_harness("claude")
        inner = "claude --add-dir /x < /p"
        assert impl.wrap_worker_invocation(inner) == inner

    def test_marcus_pinned_via_strict_config_not_global_add(
        self, tmp_path: Path
    ) -> None:
        """Marcus is pinned per-pane via --strict-mcp-config, not ``claude mcp add``.

        ``claude mcp add`` writes ~/.claude.json, which --strict-mcp-config
        ignores anyway and which parallel panes can corrupt. The register
        snippet is now a no-op marker; the agent command carries the
        marcus-only --mcp-config with ``alwaysLoad`` so the tools load
        upfront (exempt from ToolSearch deferral).
        """
        impl = harness.get_harness("claude")
        snippet = impl.build_mcp_register_snippet()
        assert "claude mcp add" not in snippet
        assert "codex" not in snippet
        cmd = impl.build_agent_command(
            tmp_path / "work",
            tmp_path / "prompt.txt",
            model_flag="",
            print_mode=False,
        )
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" in cmd
        assert "marcus" in cmd
        assert "alwaysLoad" in cmd

    def test_install_hint_is_single_line(self) -> None:
        """Claude install hint is a single instruction."""
        lines = harness.get_harness("claude").install_hint("http://localhost:4298/mcp")
        assert len(lines) == 1
        assert "Claude Code CLI" in lines[0]


class TestCodexRenderedShell:
    """Strings the CodexHarness emits, asserted exactly."""

    def test_agent_command_contains_required_flags(self, tmp_path: Path) -> None:
        """Every documented codex flag must land in the rendered command."""
        impl = harness.get_harness("codex")
        cmd = impl.build_agent_command(
            tmp_path / "work",
            tmp_path / "prompt.txt",
            model_flag="--model gpt-5.3-codex ",
            print_mode=False,
        )
        assert "codex exec --dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--disable guardian_approval" in cmd
        assert "--enable goals" in cmd
        assert "--model gpt-5.3-codex" in cmd
        # ``-C`` and ``--add-dir`` both point at the working directory.
        assert f"-C {tmp_path / 'work'}" in cmd
        assert f"--add-dir {tmp_path / 'work'}" in cmd

    def test_agent_command_ignores_print_mode(self, tmp_path: Path) -> None:
        """``print_mode`` is silently dropped — codex is always non-interactive."""
        impl = harness.get_harness("codex")
        with_print = impl.build_agent_command(
            tmp_path / "w",
            tmp_path / "p",
            model_flag="",
            print_mode=True,
        )
        without_print = impl.build_agent_command(
            tmp_path / "w",
            tmp_path / "p",
            model_flag="",
            print_mode=False,
        )
        assert with_print == without_print
        assert "--print" not in with_print

    def test_wrap_worker_invocation_renders_bounded_loop(self) -> None:
        """Wrapper is a bash for-loop bounded by MAX_RELAUNCHES."""
        wrapped = harness.get_harness("codex").wrap_worker_invocation(
            "codex exec --foo < prompt"
        )
        assert "MAX_RELAUNCHES=50" in wrapped
        assert "for relaunch in $(seq 1 $MAX_RELAUNCHES)" in wrapped
        assert "codex exec --foo < prompt" in wrapped
        assert "hit MAX_RELAUNCHES" in wrapped

    def test_wrap_worker_invocation_breaks_on_clean_exit(self) -> None:
        """Codex wrapper breaks the loop on rc=0 — Codex review P1 on #554."""
        wrapped = harness.get_harness("codex").wrap_worker_invocation("codex exec")
        assert "rc=$?" in wrapped
        assert "[ $rc -eq 0 ]" in wrapped
        assert "break" in wrapped

    def test_mcp_register_snippet_uses_url_flag(self) -> None:
        """``codex mcp add ... --url`` + idempotent ``|| true``."""
        snippet = harness.get_harness("codex").build_mcp_register_snippet()
        assert "codex mcp add marcus --url" in snippet
        assert "|| true" in snippet
        assert "claude" not in snippet

    def test_install_hint_includes_npm_and_mcp_steps(self) -> None:
        """Codex install hint covers both install + MCP registration."""
        lines = harness.get_harness("codex").install_hint("http://localhost:4298/mcp")
        assert len(lines) == 2
        assert "npm install -g @openai/codex" in lines[0]
        assert "codex mcp add marcus" in lines[1]
        # The user's specific MCP URL is interpolated, not a placeholder.
        assert "http://localhost:4298/mcp" in lines[1]


class TestGeminiHarnessMetadata:
    """``GeminiHarness`` carries gemini-specific flags + provider tag."""

    def test_identity(self) -> None:
        """Name + binary are both ``"gemini"``; provider is google."""
        impl = harness.get_harness("gemini")
        assert impl.name == "gemini"
        assert impl.binary == "gemini"
        assert impl.provider == "google"

    def test_needs_trust_dialog_poll_false(self) -> None:
        """Gemini's ``--skip-trust`` flag handles the trust dialog in-band."""
        assert harness.get_harness("gemini").needs_trust_dialog_poll is False

    def test_needs_pretrust_directory_false(self) -> None:
        """``--skip-trust`` covers it; no per-directory pretrust step needed."""
        assert harness.get_harness("gemini").needs_pretrust_directory is False

    def test_workflow_files_include_gemini_specific_convention(self) -> None:
        """``GEMINI.md`` is gemini's per-directory instructions file; the
        other two conventions ride along harmlessly."""
        impl = harness.get_harness("gemini")
        assert "GEMINI.md" in impl.workflow_files
        # The other two conventions are still written — harmless for
        # gemini, useful if a user later switches harness on the same dir.
        assert "CLAUDE.md" in impl.workflow_files
        assert "AGENTS.md" in impl.workflow_files


class TestGeminiRenderedShell:
    """Strings the GeminiHarness emits, asserted exactly."""

    def test_agent_command_contains_required_flags(self, tmp_path: Path) -> None:
        """Every gemini headless-mode flag must land in the rendered command."""
        impl = harness.get_harness("gemini")
        cmd = impl.build_agent_command(
            tmp_path / "work",
            tmp_path / "prompt.txt",
            model_flag="--model gemini-2.5-pro ",
            print_mode=False,
        )
        # Trust + approval flags so the agent runs unattended.
        assert "--skip-trust" in cmd
        assert "--yolo" in cmd
        # Workspace dir passed via gemini's documented flag — when no
        # experiment_dir kwarg is given, only the workdir is included.
        assert f"--include-directories {tmp_path / 'work'}" in cmd
        # Model + prompt-file plumbing match the other two harnesses.
        assert "--model gemini-2.5-pro" in cmd
        assert f"< {tmp_path / 'prompt.txt'}" in cmd

    def test_agent_command_only_includes_workdir(self, tmp_path: Path) -> None:
        """Only the worker's workdir lands in ``--include-directories``.

        Mirrors ``claude --add-dir <workdir>`` and ``codex --add-dir
        <workdir>``: the harness layer does not whitelist peer
        worktrees or shared coordination dirs.  Shared state
        (``project_info.json``) is materialized into the workdir by
        the worker shell script's setup phase so the agent never
        needs to read outside its sandbox.  Regression test for the
        live-validation review finding on PR #587: keep gemini's
        filesystem footprint the same shape as the other two
        harnesses.
        """
        impl = harness.get_harness("gemini")
        workdir = tmp_path / "experiment" / "worktrees" / "agent_1"
        cmd = impl.build_agent_command(
            workdir,
            tmp_path / "prompt.txt",
            model_flag="",
            print_mode=False,
        )
        # Workdir is the ONLY path passed; no comma, no peer dirs.
        assert f"--include-directories {workdir} " in cmd
        # The experiment root must NOT appear — peer worktrees stay
        # invisible to the agent.
        assert str(tmp_path / "experiment") + " " not in cmd

    def test_agent_command_ignores_print_mode(self, tmp_path: Path) -> None:
        """``print_mode`` is silently dropped — gemini headless is always
        non-interactive (parity with codex)."""
        impl = harness.get_harness("gemini")
        with_print = impl.build_agent_command(
            tmp_path / "w",
            tmp_path / "p",
            model_flag="",
            print_mode=True,
        )
        without_print = impl.build_agent_command(
            tmp_path / "w",
            tmp_path / "p",
            model_flag="",
            print_mode=False,
        )
        assert with_print == without_print
        assert "--print" not in with_print

    def test_wrap_worker_invocation_renders_bounded_loop(self) -> None:
        """Wrapper is a bash for-loop bounded by MAX_RELAUNCHES (same
        shape as codex; gemini ``-p`` is also fire-and-exit)."""
        wrapped = harness.get_harness("gemini").wrap_worker_invocation(
            "gemini --skip-trust --yolo < prompt"
        )
        assert "MAX_RELAUNCHES=50" in wrapped
        assert "for relaunch in $(seq 1 $MAX_RELAUNCHES)" in wrapped
        assert "gemini --skip-trust --yolo < prompt" in wrapped
        assert "hit MAX_RELAUNCHES" in wrapped
        # Mentions gemini in the [worker] launch echo so pane history
        # is harness-explicit when reading scrollback.
        assert "[worker] gemini launch" in wrapped

    def test_wrap_worker_invocation_does_not_break_on_clean_exit(self) -> None:
        """Gemini wrapper relaunches unconditionally on rc=0.

        Unlike codex (whose ``--enable goals`` makes ``rc=0``
        genuinely mean "goal complete"), ``gemini -p`` exits ``rc=0``
        after every single prompt cycle — gemini has no goals-
        equivalent.  Breaking on ``rc=0`` would kill the worker
        after one cycle, before it could finish a task or pick up
        another.  Discovered live during PR #587 validation: a
        gemini worker started a task, wrote a placeholder file,
        exited 0, the wrapper broke, and the task sat
        ``in_progress`` forever.

        The wrapper must therefore relaunch unconditionally and
        rely on ``MAX_RELAUNCHES`` + the monitor's
        ``end_experiment`` tmux-kill to terminate.
        """
        wrapped = harness.get_harness("gemini").wrap_worker_invocation("gemini")
        # rc is captured (still useful for the relaunch echo) but
        # there is NO branch that breaks on rc=0.
        assert "rc=$?" in wrapped
        assert "[ $rc -eq 0 ]" not in wrapped, (
            "Gemini wrapper must NOT short-circuit on clean exit — "
            "gemini -p exits 0 every cycle"
        )
        assert "finished cleanly" not in wrapped
        # Relaunch is unconditional — sleep + loop continues.
        assert "sleep 3" in wrapped

    def test_mcp_register_snippet_uses_http_transport_at_user_scope(
        self,
    ) -> None:
        """``gemini mcp add --transport http --scope user`` writes to
        ``~/.gemini/settings.json``; ``|| true`` keeps re-spawn idempotent."""
        snippet = harness.get_harness("gemini").build_mcp_register_snippet()
        assert "gemini mcp add" in snippet
        assert "--transport http" in snippet
        assert "--scope user" in snippet
        assert "|| true" in snippet
        # Must not leak claude/codex CLI names.
        assert "claude" not in snippet
        assert "codex" not in snippet

    def test_install_hint_includes_npm_and_mcp_steps(self) -> None:
        """Two-line install hint covers install + MCP registration."""
        lines = harness.get_harness("gemini").install_hint("http://localhost:4298/mcp")
        assert len(lines) == 2
        assert "npm install -g @google/gemini-cli" in lines[0]
        assert "gemini mcp add" in lines[1]
        # The user's specific MCP URL is interpolated, not a placeholder.
        assert "http://localhost:4298/mcp" in lines[1]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Every implementation satisfies the runtime-checkable
    :class:`Harness` Protocol."""

    def test_claude_implements_harness_protocol(self) -> None:
        """isinstance check passes — ClaudeHarness has every method/attr."""
        assert isinstance(harness.get_harness("claude"), harness.Harness)

    def test_codex_implements_harness_protocol(self) -> None:
        """isinstance check passes — CodexHarness has every method/attr."""
        assert isinstance(harness.get_harness("codex"), harness.Harness)

    def test_gemini_implements_harness_protocol(self) -> None:
        """isinstance check passes — GeminiHarness has every method/attr."""
        assert isinstance(harness.get_harness("gemini"), harness.Harness)
