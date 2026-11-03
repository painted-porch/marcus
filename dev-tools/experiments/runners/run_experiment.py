#!/usr/bin/env python3
"""
Marcus experiment runner: set up an experiment directory and start sessions.

``--init`` lays out the directory from the templates, ``--validate`` checks
it, and a plain run hands the experiment to the session launcher
(:mod:`runners.session_launcher`), which creates the project over MCP and
starts N persistent worker sessions. Marcus itself starts nothing (epic
#706, ADR-0012 D10).
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from runners.session_launcher import SessionLauncher  # noqa: E402
from runners.worker_host import ExperimentConfig  # noqa: E402


def load_seed_tasks(path: Path) -> List[Dict[str, Any]]:
    """Read a seed file: a YAML list of tasks, or a mapping with a ``tasks`` list.

    Parameters
    ----------
    path : Path
        The YAML file.

    Returns
    -------
    List[Dict[str, Any]]
        The task specs, exactly as ``create_project`` expects in
        ``options.tasks``. Marcus validates them (names, dependencies,
        hours, priority) and refuses a malformed list before any state.

    Raises
    ------
    ValueError
        When the file is missing, unreadable, or not a list of mappings.
    """
    import yaml

    if not path.is_file():
        raise ValueError(f"seed file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError(f"seed file is not valid YAML: {path}: {error}") from error
    if isinstance(data, dict):
        data = data.get("tasks")
    if (
        not isinstance(data, list)
        or not data
        or not all(isinstance(item, dict) for item in data)
    ):
        raise ValueError(
            f"seed file must hold a non-empty list of task mappings: {path}"
        )
    return data


def create_experiment_structure(experiment_dir: Path, templates_dir: Path) -> bool:
    """
    Create experiment directory structure with templates.

    Parameters
    ----------
    experiment_dir : Path
        Directory for the experiment
    templates_dir : Path
        Directory containing templates
    """
    import subprocess

    experiment_dir.mkdir(parents=True, exist_ok=True)

    config_file = experiment_dir / "config.yaml"
    spec_file = experiment_dir / "project_spec.md"

    # Copy templates if they don't exist
    if not config_file.exists():
        template_config = templates_dir / "config.yaml.template"
        shutil.copy(template_config, config_file)
        print("✓ Created config.yaml from template")
        print(f"  Edit {config_file} to configure your experiment")

    if not spec_file.exists():
        # Create a minimal project spec template
        with open(spec_file, "w") as f:
            f.write("""# Project Specification

## Overview
[Describe what you want to build]

## Features
- [Feature 1]
- [Feature 2]
- [Feature 3]

## Technical Requirements
- [Requirement 1]
- [Requirement 2]

## Technical Stack (Suggested)
- Backend: Python, FastAPI
- Database: PostgreSQL, SQLAlchemy
- Frontend: React, TypeScript (if applicable)
- Testing: pytest
- Authentication: JWT

## Deliverables
- [Deliverable 1]
- [Deliverable 2]

## Agent Notes
All agents are full-stack "unicorn" developers capable of working on any task.
They will accept and complete tasks regardless of their listed skill preferences.
""")
        print("✓ Created project_spec.md template")
        print(f"  Edit {spec_file} to describe your project")

    # Create subdirectories
    (experiment_dir / "prompts").mkdir(exist_ok=True)
    (experiment_dir / "logs").mkdir(exist_ok=True)
    implementation_dir = experiment_dir / "implementation"
    implementation_dir.mkdir(exist_ok=True)
    (experiment_dir / "worktrees").mkdir(exist_ok=True)

    # Remove stale .git at experiment root if it exists (GH-250)
    # Only implementation/ should have .git. A stale root .git
    # causes worktrees to branch from the wrong repo.
    stale_git = experiment_dir / ".git"
    if stale_git.exists() and stale_git.is_dir():
        # ``shutil`` is already imported at module scope (line 10);
        # re-importing here would make Python treat the name as local
        # across the whole function and raise ``UnboundLocalError`` on
        # the earlier ``shutil.copy`` call.
        shutil.rmtree(stale_git)
        print("✓ Removed stale .git at experiment root")

    # Initialize git repository in implementation directory
    git_dir = implementation_dir / ".git"  # pragma: allowlist secret
    if not git_dir.exists():
        print("\n[Setup] Initializing git repository...")
        try:
            subprocess.run(
                ["git", "init"],  # pragma: allowlist secret
                cwd=implementation_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-b", "main"],  # pragma: allowlist secret
                cwd=implementation_dir,
                check=True,
                capture_output=True,
            )
            print(
                "✓ Git repository initialized on main branch"
            )  # pragma: allowlist secret
        except subprocess.CalledProcessError as e:
            print(f"⚠️  Git initialization failed: {e}")

    # Verify Marcus MCP is configured
    print("\n[Setup] Verifying Marcus MCP configuration...")
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],  # pragma: allowlist secret
            capture_output=True,
            text=True,
            check=True,
        )
        if "marcus" in result.stdout:
            print("✓ Marcus MCP is configured")
            print("  Agents will have access to Marcus tools")
        else:
            print("⚠️  Marcus MCP not found in configuration")
            # pragma: allowlist secret
            print(
                "  Please run: claude mcp add marcus -t http "
                "http://localhost:4298/mcp"
            )
    except subprocess.CalledProcessError:
        print("⚠️  Could not verify MCP configuration")
        print("  Ensure Claude Code CLI is installed")
        # pragma: allowlist secret
        print(
            "  To configure Marcus: claude mcp add marcus -t http "
            "http://localhost:4298/mcp"
        )

    return not (config_file.exists() and spec_file.exists())


def validate_experiment(experiment_dir: Path) -> bool:
    """
    Validate that experiment directory is ready to run.

    Parameters
    ----------
    experiment_dir : Path
        Directory for the experiment

    Returns
    -------
    bool
        True if valid, False otherwise
    """
    import yaml

    config_file = experiment_dir / "config.yaml"

    errors = []

    if not config_file.exists():
        errors.append(f"Missing config.yaml at {config_file}")
        print("Validation errors:")
        for error in errors:
            print(f"  ✗ {error}")
        return False

    # Read config to get the project spec file name
    try:
        with open(config_file, "r") as f:
            config = yaml.safe_load(f)
            spec_filename = config.get("project_spec_file", "project_spec.md")
    except Exception as e:
        errors.append(f"Error reading config.yaml: {e}")
        print("Validation errors:")
        for error in errors:
            print(f"  ✗ {error}")
        return False

    spec_file = experiment_dir / spec_filename

    if not spec_file.exists():
        errors.append(f"Missing {spec_filename} at {spec_file}")

    if errors:
        print("Validation errors:")
        for error in errors:
            print(f"  ✗ {error}")
        return False

    return True


def main() -> None:
    """Run the experiment launcher."""
    parser = argparse.ArgumentParser(
        description="Run a Marcus multi-agent experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a new experiment
  python run_experiment.py --init ~/experiments/my-project

  # Run an existing experiment (sessions from config.yaml)
  python run_experiment.py ~/experiments/my-project

  # Run with three worker sessions
  python run_experiment.py ~/experiments/my-project --sessions 3

  # Validate experiment config without running
  python run_experiment.py --validate ~/experiments/my-project
        """,
    )

    parser.add_argument(
        "experiment_dir",
        type=str,
        help="Path to experiment directory (will be created if --init)",
    )

    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialize a new experiment with templates",
    )

    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate experiment configuration without running",
    )

    parser.add_argument(
        "--epictetus",
        action="store_true",
        default=False,
        help=(
            "Keep the tmux session alive after the run ends. "
            "Use when running Epictetus post-experiment interrogation so "
            "the tool can query agents after the project is complete. "
            "Default: session is killed once all sessions have gone home."
        ),
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=(
            "Override the model used by the worker sessions' agent CLI. "
            "The same model string is applied to every session and is "
            "passed through "
            "verbatim to whichever harness is active "
            "(``claude --model`` or ``codex --model``).  No "
            "per-harness validation — invalid model names surface as "
            "CLI errors in the agent panes.  Resolution order: this "
            "flag overrides ``agent_model`` in ``config.yaml``, which "
            "overrides ``ai.model`` in ``config_marcus.json``.  When "
            "unset, falls back to whatever the harness CLI is "
            "globally configured for.  Affects ONLY the worker "
            "sessions; Marcus's Planner model continues to "
            "read from ``config_marcus.json`` unchanged."
        ),
    )

    parser.add_argument(
        "--harness",
        type=str,
        default=None,
        choices=["claude", "codex", "gemini"],
        help=(
            "Agent CLI each session runs.  ``claude`` "
            "(default) runs Anthropic's claude CLI with "
            "``--dangerously-skip-permissions``; ``codex`` runs "
            "OpenAI's codex CLI with ``exec "
            "--dangerously-bypass-approvals-and-sandbox`` (the "
            "documented flag for approval=never, "
            "sandbox=danger-full-access; equivalent to the "
            "colloquial ``--yolo`` mode); ``gemini`` runs Google's "
            "gemini CLI with ``--skip-trust --yolo`` (bypass the "
            "trusted-directory dialog and auto-approve all tool "
            "calls).  When unset, falls back to ``harness`` in "
            "``config.yaml`` which itself defaults to ``claude``.  "
            "All sessions in a single run use the same harness; "
            "mixed-harness teams are intentionally out of scope for "
            "v1.  Pre-flight verifies the harness CLI is on PATH and "
            "that the Marcus MCP server is registered in the "
            "harness's config (``~/.claude.json`` vs "
            "``~/.codex/config.toml`` vs ``~/.gemini/settings.json``)."
        ),
    )

    parser.add_argument(
        "--sessions",
        type=int,
        default=None,
        metavar="N",
        help=(
            "How many persistent worker sessions to start (one chore per "
            "harness invocation, fresh context each chore). Defaults to "
            "``sessions`` in config.yaml, or the number of listed agents. "
            "The launcher creates the project over MCP, waits for every "
            "session to go home on BOARD_DRAINED, then tears down; no "
            "restarts, no watchdog (epic #706 Milestone A)."
        ),
    )
    parser.add_argument(
        "--max-chores",
        type=int,
        default=200,
        help="Per-session chore cap (default 200).",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        default=False,
        help="Return as soon as the sessions are started.",
    )
    parser.add_argument(
        "--seed",
        type=str,
        default=None,
        metavar="TASKS_YAML",
        help=(
            "Seed the board with the exact tasks in this YAML file instead of "
            "decomposing the spec: no LLM decomposition, no added integration "
            "or README tasks, no subtask splitting. One to three tasks make a "
            "live check take minutes. See templates/seed_tasks.example.yaml."
        ),
    )
    args = parser.parse_args()

    experiment_dir = Path(args.experiment_dir).resolve()

    # Find templates directory (now at parent level)
    script_dir = Path(__file__).parent
    templates_dir = script_dir.parent / "templates"

    if not templates_dir.exists():
        print(f"Error: Templates directory not found at {templates_dir}")
        sys.exit(1)

    # Initialize mode
    if args.init:
        print(f"Initializing experiment at: {experiment_dir}")
        print()
        create_experiment_structure(experiment_dir, templates_dir)
        print()
        print("✓ Experiment initialized!")
        print()
        print("Next steps:")
        print(f"  1. Edit {experiment_dir / 'config.yaml'}")
        print(f"  2. Edit {experiment_dir / 'project_spec.md'}")
        print(f"  3. Run: python run_experiment.py {experiment_dir}")
        sys.exit(0)

    # Validate experiment exists
    if not experiment_dir.exists():
        print(f"Error: Experiment directory not found: {experiment_dir}")
        print()
        print("To create it, run:")
        print(f"  python run_experiment.py --init {experiment_dir}")
        sys.exit(1)

    # Validate configuration
    if not validate_experiment(experiment_dir):
        sys.exit(1)

    # Validate-only mode
    if args.validate:
        print(f"✓ Experiment configuration is valid: {experiment_dir}")
        sys.exit(0)

    # Run the experiment
    print(f"Running experiment: {experiment_dir}")
    print()

    config_file = experiment_dir / "config.yaml"
    config = ExperimentConfig(config_file)
    if args.seed:
        try:
            config.project_options["tasks"] = load_seed_tasks(Path(args.seed))
        except ValueError as error:
            print(f"Error: {error}")
            sys.exit(1)
        print(f"Seeding the board with {len(config.project_options['tasks'])} task(s)")
    sessions = args.sessions if args.sessions is not None else config.sessions
    launcher = SessionLauncher(
        config,
        templates_dir,
        sessions=sessions,
        epictetus=args.epictetus,
        agent_model=args.model,
        harness=args.harness,
        max_chores=args.max_chores,
        wait=not args.no_wait,
    )
    sys.exit(0 if launcher.run() else 1)


if __name__ == "__main__":
    main()
