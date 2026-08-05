#!/usr/bin/env python3
"""
Marcus Multi-Agent Experiment Spawner.

Spawns autonomous agents for a Marcus experiment based on config.yaml.
All agents work on the main branch in the experiment's implementation directory.
"""

import json
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml


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
        self.timeouts = self.config.get("timeouts", {})

        # Set up experiment directories
        self.prompts_dir = self.experiment_dir / "prompts"
        self.logs_dir = self.experiment_dir / "logs"
        self.implementation_dir = self.experiment_dir / "implementation"

        # Create directories
        self.prompts_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.implementation_dir.mkdir(exist_ok=True)

        # Add project_root to project_options for create_project MCP call
        # This tells Marcus where the implementation files will be created
        self.project_options["project_root"] = str(self.implementation_dir)

        # Entry-point discriminator for cost attribution. Marcus's
        # create_project wrapper reads ``options["path"]`` and stamps
        # it onto the ``runs.path`` column so the dashboard can break
        # cost out by *which* entry point produced each run.
        #
        # Default to "marcus" since spawn_agents.py is the meta-runner
        # behind the /marcus skill. Posidonius overrides this in its
        # config.yaml with ``path: "posidonius"``. Plain MCP callers
        # get ``"direct"`` for free (the wrapper's default) because
        # they never hit this code path.
        #
        # Honor an explicit override in config — letting users
        # extend the taxonomy without code changes.
        self.project_options.setdefault("path", "marcus")

        # Project info file (shared between creator and workers)
        self.project_info_file = self.experiment_dir / "project_info.json"

    def get_timeout(self, key: str, default: int) -> int:
        """Get timeout value from config or use default."""
        return int(self.timeouts.get(key, default))


class AgentSpawner:
    """Spawns and manages autonomous agents for an experiment."""

    def __init__(self, config: ExperimentConfig, templates_dir: Path):
        """
        Initialize the agent spawner.

        Parameters
        ----------
        config : ExperimentConfig
            Experiment configuration
        templates_dir : Path
            Path to templates directory (in marcus repo)
        """
        self.config = config
        self.templates_dir = templates_dir
        self.agent_prompt_template = templates_dir / "agent_prompt.md"
        self.processes: List[subprocess.Popen[bytes]] = []
        self.tmux_session = (
            f"marcus_{self.config.project_name.lower().replace(' ', '_')}"
        )
        self.panes_per_window = 4
        self.current_window = 0
        self.current_pane = 0

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

2. IMMEDIATELY call mcp__marcus__create_project with these EXACT parameters:
   - project_name: "{self.config.project_name}"
   - description: (the full spec below)
   - options: {options_str}

PROJECT SPECIFICATION:
{project_description}

3. As soon as the project is created (DO NOT WAIT for user input):
   - Write project_id and board_id to: {self.config.project_info_file}
   - Format: {{"project_id": "<id>", "board_id": "<board_id>"}}
   - Run: git add -A && git commit -m "Initial commit: Marcus project created"
   - Print: "PROJECT CREATED: project_id=<id> board_id=<board_id>"

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
"provider": "{self.config.project_options.get('provider', 'planka')}"}}
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

    def create_worker_prompt(self, agent: Dict[str, Any]) -> str:
        """
        Create the prompt for a worker agent.

        Parameters
        ----------
        agent : Dict
            Agent configuration from config.yaml

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

        worker_prompt = f"""You are {agent_name} (ID: {agent_id})
in a Marcus multi-agent experiment.

Your role: {agent_role}
Your skills: {", ".join(agent_skills)}
Project root: {self.config.implementation_dir}

STARTUP SEQUENCE:
1. Wait for project_info.json to exist at {self.config.project_info_file}
   (check every 5 seconds, max 60 seconds)
   (This signals the project has been created and is ready)

2. Use mcp__marcus__register_agent to register yourself:
   - agent_id: "{agent_id}"
   - name: "{agent_name}"
   - role: "{agent_role}"
   - skills: {json.dumps(agent_skills)}

3. Register {num_subagents} subagents:
   For i in 1 to {num_subagents}:
   - Use mcp__marcus__register_agent with:
     - agent_id: "{agent_id}_sub{{i}}"
     - name: "{agent_name} Subagent {{i}}"
     - role: "{agent_role}"
     - skills: {json.dumps(agent_skills)}

4. Call mcp__marcus__request_next_task:
   - No parameters needed
   - This will find tasks suitable for your skills
   - If you get "no suitable tasks", follow the PERSISTENCE PATTERN below:
     check get_project_status, and while work remains keep retrying with
     backoff (30s -> 60s -> 120s max). Only exit when all tasks are complete.
     A task can return to the board late in the run (e.g. sent back for
     redo during integration verification) — someone must be there to claim it.

5. When you get a task:
   - Check dependencies with get_task_context
   - Work on it in: {self.config.implementation_dir}
   - Report progress at 25%, 50%, 75%, 100%
   - Commit to main branch (git add, commit, push)
   - When 100% complete, IMMEDIATELY call request_next_task again

6. Repeat step 5 until get_project_status shows all tasks complete

---

{base_prompt}

---

CRITICAL REMINDERS:
- Work directory: {self.config.implementation_dir}
- Git branch: main (all agents work together on main)
- After EVERY task completion, IMMEDIATELY request_next_task
- Use get_task_context for tasks with dependencies
- Use log_decision for architectural choices
- Use log_artifact with project_root: {self.config.implementation_dir}
- If "no suitable tasks", follow the PERSISTENCE PATTERN
  (retry with backoff while work remains)

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

        # Create new session (detached)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.tmux_session, "-n", "agents-0"],
            check=True,
        )
        print(f"✓ Created tmux session: {self.tmux_session}")

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
                    f"{self.tmux_session}:{window}",
                    "-n",
                    f"agents-{window}",
                ],
                check=True,
            )
            self.current_window = window

        self.current_pane += 1
        return window, pane

    def run_in_tmux_pane(
        self, window: int, pane: int, script_file: Path, title: str
    ) -> None:
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
        """
        # For first pane in window, use existing pane
        if pane == 0:
            target = f"{self.tmux_session}:{window}.0"
        else:
            # Split the window and get the new pane's target
            # Layout: 0=top-left, 1=top-right, 2=bottom-left, 3=bottom-right
            if pane == 1:
                # Split window 0 horizontally (right side)
                split_direction = "-h"
                split_target = f"{self.tmux_session}:{window}.0"
            elif pane == 2:
                # Split window 0 vertically (bottom-left)
                split_direction = "-v"
                split_target = f"{self.tmux_session}:{window}.0"
            elif pane == 3:
                # Split window 1 vertically (bottom-right)
                split_direction = "-v"
                split_target = f"{self.tmux_session}:{window}.1"
            else:
                raise ValueError(f"Invalid pane number: {pane}")

            # Split and capture the new pane ID
            result = subprocess.run(
                [
                    "tmux",
                    "split-window",
                    split_direction,
                    "-t",
                    split_target,
                    "-P",  # Print new pane ID
                    "-F",
                    "#{pane_id}",  # Format: just the pane ID
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            # Use the actual pane ID returned by tmux
            target = result.stdout.strip()
            time.sleep(0.2)  # Give tmux time to stabilize

        # Send commands to the pane using its actual ID
        subprocess.run(
            ["tmux", "send-keys", "-t", target, f"bash {script_file}", "Enter"],
            check=True,
        )

        # Set pane title
        subprocess.run(
            ["tmux", "select-pane", "-t", target, "-T", title],
            check=True,
        )

        time.sleep(0.1)  # Brief delay before next operation

    def copy_agent_workflow_to_implementation(self) -> None:
        """
        Copy agent workflow instructions to CLAUDE.md in implementation directory.

        This ensures agents can reference the workflow throughout their session
        even after the initial prompt is forgotten.
        """
        claude_md_path = self.config.implementation_dir / "CLAUDE.md"

        # Read the agent prompt template
        with open(self.agent_prompt_template, "r") as f:
            workflow_content = f.read()

        # Write to CLAUDE.md in implementation directory
        with open(claude_md_path, "w") as f:
            f.write(workflow_content)

        print(f"✓ Agent workflow copied to {claude_md_path}")

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

cd {self.config.implementation_dir} || exit 1
echo "=========================================="
echo "PROJECT CREATOR AGENT"
echo "Working Directory: $(pwd)"
echo "=========================================="
echo ""
echo "Configuring Marcus MCP..."
claude mcp add marcus -t http http://localhost:4298/mcp 2>/dev/null || true
echo ""
echo "Creating Marcus project: {self.config.project_name}"
echo ""
# Launch Claude from the implementation directory (cwd matters!)
claude --dangerously-skip-permissions --print < {prompt_file}
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

    def spawn_worker(self, agent: Dict[str, Any]) -> None:
        """
        Spawn a worker agent in a tmux pane.

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

        prompt = self.create_worker_prompt(agent)
        prompt_file = self.config.prompts_dir / f"{agent_id}.txt"

        with open(prompt_file, "w") as f:
            f.write(prompt)

        # Create a script to run in tmux
        script = f"""#!/bin/bash
# Source shell profile to get nvm/claude in PATH
[ -f ~/.zshrc ] && source ~/.zshrc
[ -f ~/.bashrc ] && source ~/.bashrc

cd {self.config.implementation_dir} || exit 1
echo "=========================================="
echo "{agent_name.upper()}"
echo "ID: {agent_id}"
echo "Role: {agent_role}"
echo "Branch: main (shared)"
echo "Working Directory: $(pwd)"
echo "=========================================="
echo ""
echo "Waiting for project creation..."
while [ ! -f {self.config.project_info_file} ]; do
    sleep 2
done
echo "✓ Project found, starting agent..."
echo ""
# Launch Claude from the implementation directory (cwd matters!)
claude --dangerously-skip-permissions < {prompt_file}
echo ""
echo "=========================================="
echo "{agent_name} - Work Complete"
echo "=========================================="
"""
        script_file = self.config.prompts_dir / f"{agent_id}.sh"
        with open(script_file, "w") as f:
            f.write(script)
        script_file.chmod(0o755)

        # Get pane location and run
        window, pane = self.get_next_pane_location()
        self.run_in_tmux_pane(window, pane, script_file, agent_name)

        print(f"  ✓ Spawned in tmux window {window}, pane {pane}")
        print(f"  Prompt: {prompt_file}")
        print(f"  Subagents: {num_subagents}")

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

        # Calculate tmux layout
        total_agents = 1 + len(self.config.agents)  # creator + workers
        num_windows = (
            total_agents + self.panes_per_window - 1
        ) // self.panes_per_window
        print(
            f"Tmux Layout: {num_windows} window(s) with up to "
            f"{self.panes_per_window} panes each"
        )
        print("=" * 60)

        # Create tmux session
        print("\n[Setup] Creating tmux session")
        self.create_tmux_session()

        # Phase 1: Spawn project creator
        print("\n[Phase 1] Creating Project")
        self.spawn_project_creator()

        # Wait for project creation
        timeout = self.config.get_timeout("project_creation", 300)
        start_time = time.time()

        print("\nWaiting for project creation...")
        while not self.config.project_info_file.exists():
            if time.time() - start_time > timeout:
                print("✗ Project creation timed out!")
                return False

            time.sleep(5)

        print("✓ Project created!")

        # Read project info
        with open(self.config.project_info_file, "r") as f:
            project_info = json.load(f)
            project_id = project_info.get("project_id")
            board_id = project_info.get("board_id")

        print(f"  Project ID: {project_id}")
        print(f"  Board ID: {board_id}")

        # Phase 2: Spawn worker agents
        print("\n" + "=" * 60)
        num_agents = len(self.config.agents)
        print(f"[Phase 2] Spawning {num_agents} Worker Agents")
        print("=" * 60)

        for agent in self.config.agents:
            self.spawn_worker(agent)
            time.sleep(0.5)  # Stagger starts to avoid tmux race conditions

        print("\n" + "=" * 60)
        print("All Agents Spawned!")
        print("=" * 60)
        print(f"\n✓ All agents running in tmux session: {self.tmux_session}")
        print(f"✓ 1 project creator + {len(self.config.agents)} worker agents")
        print(f"✓ {total_subagents} subagents will be registered by workers")
        print(
            f"\n📺 Tmux layout: {num_windows} window(s), "
            f"{self.panes_per_window} panes max per window"
        )
        print("\nTmux commands:")
        print(f"  - Attach to session: tmux attach -t {self.tmux_session}")
        print("  - Switch windows: Ctrl+b n (next) or Ctrl+b p (previous)")
        print("  - Switch panes: Ctrl+b arrow keys")
        print("  - Detach: Ctrl+b d")
        print(f"  - Kill session: tmux kill-session -t {self.tmux_session}")
        print(
            "\nAll agents work on the MAIN branch in the same "
            "implementation directory."
        )
        print("Marcus coordinates task assignment to prevent conflicts.")
        print("\nAttaching to tmux session in 3 seconds...")

        # Give time to read the instructions
        time.sleep(3)

        # Attach to the tmux session (this blocks until user detaches)
        try:
            subprocess.run(["tmux", "attach", "-t", self.tmux_session])
        except KeyboardInterrupt:
            print("\n\nDetached from tmux session.")

        print("\nExperiment manager shutting down...")
        print(f"Tmux session '{self.tmux_session}' is still running.")
        print(f"To re-attach: tmux attach -t {self.tmux_session}")
        print(f"To kill session: tmux kill-session -t {self.tmux_session}")
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
