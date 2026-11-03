# Marcus Multi-Agent Experiments

A comprehensive framework for running and evaluating multi-agent software development experiments with Marcus task orchestration.

## 📂 Directory Structure

```
experiments/
├── runners/              # Experiment runners & launchers
├── analysis/             # Analysis & visualization tools
├── scoring/              # Project evaluation & scoring
├── testing/              # Testing & validation tools
├── utils/                # Utility scripts
├── templates/            # Experiment configuration templates
├── examples/             # Example experiment configurations
├── docs/                 # Documentation & guides
├── test_projects/        # Test project specifications
├── benchmarks/           # Benchmarking tools (reserved)
├── mlruns/               # MLflow experiment tracking data
└── README.md             # This file
```

---

## 🚀 Quick Start

### 1. Create a New Multi-Agent Experiment

```bash
cd /Users/lwgray/dev/marcus/dev-tools/experiments
python runners/run_experiment.py --init ~/my-experiments/podcast-generator
```

This creates:
```
~/my-experiments/podcast-generator/
├── config.yaml          # Agent configuration
├── project_spec.md      # What to build
├── prompts/             # Auto-generated prompts
├── logs/                # Agent logs
└── implementation/      # Code output (git repo)
```

### 2. Configure Your Experiment

Edit `config.yaml`:
```yaml
project_name: "Podcast Generator"
project_spec_file: "project_spec.md"

project_options:
  complexity: "standard"
  provider: "sqlite"        # or "planka", "github", "linear"

agents:
  - id: "agent_backend"
    name: "Backend Developer"
    role: "backend"
    skills: ["python", "fastapi", "postgresql"]
    subagents: 5
```

Edit `project_spec.md` with your project description.

### 3. Test Optimal Agent Count (Recommended)

```bash
python testing/test_optimal_agents.py ~/my-experiments/podcast-generator
```

Analyzes your project and recommends the optimal number of agents based on task dependencies.

### 4. Run the Experiment

```bash
python runners/run_experiment.py ~/my-experiments/podcast-generator
```

---

## 📚 Detailed Component Documentation

### 🏃 Runners (`runners/`)

Main experiment execution scripts.

#### `run_experiment.py`
**Purpose**: Set up an experiment directory and start worker sessions
**Usage**:
```bash
# Initialize new experiment
python runners/run_experiment.py --init <experiment_dir>

# Validate configuration
python runners/run_experiment.py --validate <experiment_dir>

# Run: sessions from config.yaml (one per listed agent unless `sessions:` is set)
python runners/run_experiment.py <experiment_dir>

# Run with three worker sessions
python runners/run_experiment.py <experiment_dir> --sessions 3

# Seed the board with the exact tasks in a YAML file (no decomposition)
python runners/run_experiment.py <experiment_dir> --seed templates/seed_tasks.example.yaml
```

**Seeded boards (fast live checks).** `--seed tasks.yaml` hands `create_project` an explicit task list instead of a spec to decompose: no LLM decomposition, no added integration or README tasks, no subtask splitting. One to three tasks make a launcher run take minutes, which is how fixes to the coordination machinery (drain, teardown, recovery, merge) get live-checked. Each task has a `name` and optionally `description`, `labels`, `depends_on` (other seeded task names), `completion_criteria`, `estimated_hours`, and `priority`. Labels drive Marcus's gates (`implement` runs the validator and the TDD standard). Marcus validates the list and refuses a malformed one before any state is touched. Seeded runs do not exercise decomposition, contract generation, or the scaffold; keep real runs for those.

**What it does**:
- Creates the experiment directory structure from the templates
- Hands the run to the session launcher (`session_launcher.py`), which creates the project over MCP, starts N persistent worker sessions, waits for them to go home, and tears down
- Marcus itself starts nothing (epic #706, ADR-0012 D10)

**Key Features**:
- Portable (works outside Marcus repo)
- Configurable session identities and skills
- `--model`, `--harness claude|codex|gemini`, `--epictetus`, `--max-chores`, `--no-wait`

---

#### `session_launcher.py`
**Purpose**: Start N worker sessions and get out of the way
**Note**: Run through `run_experiment.py`.

**What it does**:
- Initialises the implementation repository on `main` and writes the one worker contract (`prompts/Agent_prompt.md`) into `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md`; each chore is handed a short session header (`templates/session_prompt.md`) that points at it
- Calls `create_project` over MCP with `project_root` set and commits the scaffold
- Gives each session a worktree at `worktrees/<agent_id>` and a script that runs the harness one chore at a time, fresh context each chore; a chore that follows a finished or handed-back one gets a fresh worktree and branch cut from `main` at the same path, and the script removes the worktree when the session goes home or is killed (#628)
- Never restarts a dead session and has no watchdog; sessions go home when `request_next_task` returns `BOARD_DRAINED`
- Mirrors its progress to `<experiment_dir>/logs/runner.log`

---

#### `worker_host.py`
**Purpose**: Experiment config, tmux, worktrees, and the harness command
**Note**: Used by `session_launcher.py`, not run directly.

**What it does**:
- Reads `config.yaml` (`ExperimentConfig`)
- Owns the tmux session and starts one detached window per session (`WorkerHost`)
- Creates the per-session git worktree and renders the agent CLI command for the chosen harness
- Seeds Claude's trust file so panes never block on a dialog

---

#### `run_single_agent_experiment.py`
**Purpose**: Single-agent controlled experiment for comparison testing
**Usage**:
```bash
# Initialize single-agent experiment
python runners/run_single_agent_experiment.py --init <experiment_dir>

# Run experiment
python runners/run_single_agent_experiment.py <experiment_dir>

# Log results to MLflow after completion
python runners/run_single_agent_experiment.py --log-results <log_file> <experiment_dir>
```

**What it does**:
- Runs same project spec with a single Claude Code instance
- Two modes:
  - **Structured**: Uses Marcus task breakdown with checkpoints
  - **Unstructured**: Raw project description (pure baseline)
- Time tracking with checkpoint markers
- MLflow integration for comparison

**Configuration** (`config_single_agent.yaml`):
```yaml
experiment_type: "single_agent"
project_name: "Your Project"
project_spec_file: "project_spec.md"

single_agent:
  model: "claude-sonnet-4-5-20250929"
  mode: "structured"        # or "unstructured"
  checkpoint_mode: true
  time_tracking: true
```

---

#### `run_comparison_experiment.py`
**Purpose**: Automated batch runner for comparing single vs multi-agent
**Usage**:
```bash
# Dry run to see what would execute
python runners/run_comparison_experiment.py --dry-run

# Run all test projects sequentially
python runners/run_comparison_experiment.py

# Run specific projects
python runners/run_comparison_experiment.py --projects 01_simple_calculator 04_ecommerce

# PARALLEL MODE: Run experiments in parallel (requires multiple Marcus instances)
python runners/run_comparison_experiment.py --parallel --max-parallel 3

# Parallel with custom instance configuration
python runners/run_comparison_experiment.py --parallel --marcus-instances instances.json
```

**What it does**:
- Automatically runs multiple experiment configurations
- Compares single-agent vs multi-agent performance
- Sequential or parallel execution modes
- Generates comparison reports and visualizations
- Full MLflow tracking

**Parallel Mode Requirements**:
- Multiple Marcus instances on different ports
- Each connected to separate Planka boards
- Configuration file: `marcus_instances.example.json`

**Example instances config**:
```json
[
  {"url": "http://localhost:4298", "board_id": "board_0"},
  {"url": "http://localhost:4299", "board_id": "board_1"},
  {"url": "http://localhost:4300", "board_id": "board_2"}
]
```

---

### 📊 Analysis (`analysis/`)

Tools for analyzing experiment results and generating insights.

#### `compare_experiments.py`
**Purpose**: Compare results across multiple MLflow experiments
**Location**: Called by `run_comparison_experiment.py`

**What it does**:
- Extracts metrics from MLflow runs
- Generates comparison reports (JSON)
- Statistical analysis of performance
- Identifies best configurations

---

#### `visualize_results.py`
**Purpose**: Create visualizations from experiment results
**Location**: Called by `run_comparison_experiment.py`

**What it does**:
- Time comparison charts
- Quality score distributions
- Agent utilization plots
- Saves to `plots/` directory

**Dependencies**: See `analysis/requirements.txt`
```bash
pip install -r analysis/requirements.txt
```

---

#### `analyze_reliability_decay.py`
**Purpose**: Compare Marcus against multiplicative reliability decay model
**Usage**:
```bash
python analysis/analyze_reliability_decay.py \
  --experiments "Reliability Test: 5 Agents" "Reliability Test: 10 Agents" \
  --output-dir results/reliability_analysis
```

**What it does**:
- Tests the claim that multi-agent systems suffer from multiplicative error propagation
- Compares actual Marcus performance vs predicted decay (p^n model)
- Generates plots showing Marcus prevents error cascade
- Proves board-mediated coordination advantage

**Key Insight**: Marcus's board-mediated architecture prevents the reliability decay that plagues pipeline-style multi-agent systems.

**Output**:
- `reliability_comparison.png` - Visual comparison graph
- `reliability_comparison_table.csv` - Data table
- `RELIABILITY_ANALYSIS.md` - Comprehensive report

---

#### `parse_single_agent_timing.py`
**Purpose**: Extract timing data from single-agent experiment logs
**Usage**:
```bash
# Display summary
python analysis/parse_single_agent_timing.py logs/experiment.log

# Output JSON
python analysis/parse_single_agent_timing.py logs/experiment.log --json

# Save metrics
python analysis/parse_single_agent_timing.py logs/experiment.log --output metrics.json
```

**What it does**:
- Parses checkpoint timestamps from logs
- Calculates total duration
- Extracts subtask completion times
- Formats for MLflow logging

**Expected log format**:
```
START: 2025-10-23 10:15:32
SUBTASK 1.1 COMPLETE: 10:18:45 (3:13 elapsed)
SUBTASK 1.2 COMPLETE: 10:22:30 (6:58 elapsed)
TASK 1 COMPLETE: 10:25:00
...
END: 2025-10-23 10:47:18
TOTAL: 31 minutes 46 seconds
```

---

### 🎯 Scoring (`scoring/`)

Automated project evaluation tools.

#### `score_project.py`
**Purpose**: Rule-based automated project scoring
**Usage**:
```bash
python scoring/score_project.py \
  --project-dir ~/experiments/test1/implementation \
  --output-file score.json
```

**What it does**:
- Evaluates projects across 6 categories (100 points total):
  - **Functionality** (25 pts): Does it run? Do tests pass?
  - **Code Quality** (20 pts): Static analysis, complexity, documentation
  - **Completeness** (20 pts): All deliverables, no stubs
  - **Project Structure** (15 pts): Organization, file count
  - **Documentation** (12 pts): README, API docs, setup instructions
  - **Usability** (8 pts): Single-command startup, examples

**Output** (JSON):
```json
{
  "project_name": "datetime-api",
  "total_score": 82.5,
  "total_possible": 100,
  "percentage": 82.5,
  "categories": [...]
}
```

---

#### `llm_score_project.py`
**Purpose**: LLM-based intelligent project scoring
**Usage**:
```bash
python scoring/llm_score_project.py \
  --project-dir ~/experiments/test1/implementation \
  --output-file llm_score.json
```

**What it does**:
- Uses LLM (via Marcus LLMAbstraction) to evaluate code
- Same categories as `score_project.py` but with qualitative reasoning
- Provides strengths, weaknesses, and recommendations
- More nuanced understanding of architecture and design

**Advantages over rule-based**:
- Understands code intent and design patterns
- Evaluates architecture quality
- Provides actionable feedback
- More accurate for complex projects

**Output** (JSON):
```json
{
  "project_name": "datetime-api",
  "total_score": 85.0,
  "categories": [
    {
      "category": "Functionality",
      "points_earned": 22.0,
      "reasoning": "...",
      "strengths": ["..."],
      "weaknesses": ["..."]
    }
  ],
  "overall_assessment": "...",
  "recommendations": ["..."]
}
```

---

#### `compare_scores.py`
**Purpose**: Generate comprehensive comparison reports
**Usage**:
```bash
python scoring/compare_scores.py \
  --marcus marcus_score.json \
  --single single_score.json \
  --time-marcus 6.5 \
  --time-single 42.0 \
  --output comparison_report.md
```

**What it does**:
- Compares Marcus vs Single Agent scores
- Category-by-category breakdown
- Identifies strengths and weaknesses
- Quality/time ratio analysis
- Winner determination

**Output**: Markdown report with tables, analysis, and recommendations.

---

### 🧪 Testing (`testing/`)

Tools for testing and validating Marcus functionality.

#### `test_optimal_agents.py`
**Purpose**: Calculate optimal agent count BEFORE running experiments
**Usage**:
```bash
python testing/test_optimal_agents.py ~/my-experiments/podcast-generator
```

**What it does**:
1. Reads experiment configuration
2. Creates project in Marcus/Planka
3. Calls `get_optimal_agent_count` MCP tool
4. Analyzes task dependency graph
5. Calculates maximum parallelism
6. Shows optimal agent configuration
7. Offers to update `config.yaml`

**Output**:
```
📊 Project Analysis:
   Total tasks: 87
   Critical path: 12.50 hours
   Max parallelism: 12 tasks can run simultaneously
   Efficiency gain: 85.2% vs single agent

✅ RECOMMENDED: 12 agents

⚙️  Current config.yaml: 18 total agents
   - Backend Dev: 5 subagents + 1 main = 6 total
   - Frontend Dev: 3 subagents + 1 main = 4 total
   ...

⚠️  WARNING: You have 6 more agents than needed
   Extra agents will be idle, wasting resources
```

**Why This Matters**:
- Prevents spawning too many agents (wasted resources)
- Prevents spawning too few agents (missed parallelism)
- Avoids agents dying while waiting for dependencies
- Optimizes cost and performance

---

#### `create_and_analyze_project.py`
**Purpose**: Direct API approach for project creation & analysis
**Usage**:
```bash
python testing/create_and_analyze_project.py ~/my-experiments/test1
```

**What it does**:
- Bypasses Claude Code CLI
- Directly uses Marcus HTTP API (Inspector client)
- Creates project and gets optimal agent count
- Faster for testing/debugging
- Same output as `test_optimal_agents.py`

**Use when**:
- Testing Marcus API directly
- Debugging project creation
- Automating batch project analysis
- Claude Code CLI issues

---

#### `analyze_100_tasks.py`
**Purpose**: Test scheduler with 100 independent tasks
**Usage**:
```bash
python testing/analyze_100_tasks.py
```

**What it does**:
- Loads `100_independent_tasks.json`
- Runs `calculate_optimal_agents` scheduler
- Verifies sweep-line algorithm correctness
- Ensures truly independent tasks all run in parallel

**Expected Result**:
```
✅ SWEEP-LINE ALGORITHM WORKS CORRECTLY!
   All 100 tasks detected as running in parallel.
   This proves the bug fix handles truly independent tasks.
```

**Use for**:
- Validating scheduler fixes
- Testing edge cases
- Performance benchmarking

---

#### `100_independent_tasks.json`
**Purpose**: Test data for `analyze_100_tasks.py`
**Format**: JSON array of Task objects with no dependencies

---

### 🛠️ Utils (`utils/`)

Utility scripts for experiment management.

#### `clean_experiment.sh`
**Purpose**: Clean experiment directory for re-runs
**Usage**:
```bash
./utils/clean_experiment.sh ~/my-experiments/podcast-generator
```

**What it does**:
- Deletes all contents of `prompts/`
- Deletes `project_info.json`
- Deletes all contents of `implementation/` (including `.git`)
- Prompts for confirmation before deletion

**Use before**:
- Re-running an experiment
- Testing configuration changes
- Resetting experiment state

**Warning**: Destructive! Make sure you've saved any important results.

---

### 📄 Templates (`templates/`)

Configuration and prompt templates for experiments.

#### `config.yaml.template`
Full-featured multi-agent configuration template with:
- Multiple agent definitions
- Skill specifications
- Subagent counts
- MLflow tracking options
- Timeouts

#### `config_single_agent.yaml`
Configuration template for single-agent experiments.

#### `seed_tasks.example.yaml`
A two-task seed for `run_experiment.py --seed`: an implement task and a CLI task that depends on it. Copy and edit it for a live check.

#### `session_prompt.md`
The per-chore session header every worker is handed. It is not a second contract: the contract is `prompts/Agent_prompt.md`, written into the workspace as `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`. The header adds only:
- The session identity and its worktree
- One chore per invocation, and the two markers (`.marcus_session_done`, `.marcus_chore_done`) that tell the harness what happened
- The exit rules for this invocation (`BOARD_DRAINED`, retry, chore done)

#### `single_agent_prompt.template.md`
Single-agent prompt template (deprecated, now generated dynamically).

---

### 📖 Examples (`examples/`)

Example experiment configurations.

#### `task_management_api.yaml`
Complete example: REST API with auth, users, projects, tasks, comments.

**Agents**:
- Foundation (5 subagents): Database models, migrations
- Auth (3 subagents): JWT, bcrypt, endpoints
- API (3 subagents): CRUD endpoints
- Integration (2 subagents): E2E tests, validation

Copy and adapt:
```bash
cp examples/task_management_api.yaml ~/experiments/my-api/config.yaml
```

---

### 📝 Docs (`docs/`)

Additional documentation and guides.

#### `TIMING-INSTRUCTIONS.md`
Detailed instructions for time tracking in experiments.

#### `experiment-tracking-sheet.md`
Template for manually tracking experiment progress and results.

---

### 🧪 Test Projects (`test_projects/`)

Test project specifications for experiments.

Organized by complexity:
- `01_simple_calculator/` - Simple projects (< 10 tasks)
- `02_medium_api/` - Medium projects (10-30 tasks)
- `03_complex_platform/` - Complex projects (> 30 tasks)

Each contains:
- `project_description.txt` - What to build
- `config_*.yaml` - Multiple configurations to test
  - `config_single_agent.yaml` - Baseline
  - `config_multi_agent.yaml` - Standard multi-agent
  - `config_multi_agent_4.yaml` - 4 agents
  - `config_multi_agent_8.yaml` - 8 agents

---

## 🔬 Common Workflows

### Workflow 1: Run Single Experiment

```bash
# 1. Create experiment
python runners/run_experiment.py --init ~/experiments/my-project

# 2. Edit config and spec
vim ~/experiments/my-project/config.yaml
vim ~/experiments/my-project/project_spec.md

# 3. Test optimal agents
python testing/test_optimal_agents.py ~/experiments/my-project

# 4. Run experiment
python runners/run_experiment.py ~/experiments/my-project

# 5. Score results
python scoring/score_project.py \
  --project-dir ~/experiments/my-project/implementation \
  --output-file ~/experiments/my-project/score.json
```

---

### Workflow 2: Compare Marcus vs Single Agent

```bash
# 1. Setup test project with both configs
mkdir -p test_projects/my_test/
# Create config_single_agent.yaml and config_multi_agent.yaml

# 2. Run comparison experiment
python runners/run_comparison_experiment.py \
  --projects my_test

# 3. Results automatically saved to results/ with:
#    - comparison_report.json
#    - plots/
```

---

### Workflow 3: Validate Reliability Decay Theory

```bash
# 1. Create reliability test projects (1, 5, 10 agent configs)
# 2. Run experiments
python runners/run_comparison_experiment.py \
  --projects reliability_1 reliability_5 reliability_10

# 3. Analyze results
python analysis/analyze_reliability_decay.py \
  --experiments "Reliability: 1 Agent" "Reliability: 5 Agents" "Reliability: 10 Agents" \
  --output-dir results/reliability/

# 4. View plot: results/reliability/reliability_comparison.png
```

---

### Workflow 4: Batch Testing Multiple Configurations

```bash
# Run all test projects in parallel (requires 3 Marcus instances)
python runners/run_comparison_experiment.py \
  --parallel \
  --max-parallel 3 \
  --marcus-instances marcus_instances.json
```

---

## 📊 MLflow Integration

All experiments are automatically tracked in MLflow.

### View Experiment Results

```bash
# Start MLflow UI
cd dev-tools/experiments
mlflow ui

# Open browser to http://localhost:5000
```

### Tracked Metrics

**Multi-Agent Experiments**:
- `num_agents` - Total agent count
- `duration_seconds` - Experiment duration
- `tasks_created` - Total tasks
- `tasks_completed` - Completed tasks
- `blockers_reported` - Blockers encountered
- `artifacts_logged` - Artifacts created
- `decisions_logged` - Decisions made

**Single-Agent Experiments**:
- `total_duration_seconds` - Total time
- `checkpoints_completed` - Checkpoint count
- `subtasks_completed` - Subtasks done
- Individual checkpoint durations

---

## 🎯 Prerequisites

### Required

- **Marcus MCP Server**: Running on `http://localhost:4298`
  ```bash
  cd /Users/lwgray/dev/marcus
  ./marcus start
  ```

- **Claude Code CLI**: Installed and configured
  ```bash
  claude --version
  ```

- **Python 3.11+**: With dependencies
  ```bash
  pip install pyyaml mlflow
  ```

- **Planka Board**: For Marcus task management
  - Configure in Marcus settings
  - Get board ID from Planka UI

### Optional

- **tmux**: For multi-agent sessions (usually pre-installed on macOS)
- **Analysis Tools**: For visualizations
  ```bash
  pip install -r analysis/requirements.txt
  ```

---

## 🚨 Troubleshooting

### "Marcus MCP server not running"

```bash
# Check status
curl http://localhost:4298/mcp

# Start Marcus
cd /Users/lwgray/dev/marcus
./marcus start

# Configure Claude Code
claude mcp add marcus -t http http://localhost:4298/mcp
```

### "No suitable tasks" loops

- Agents waiting for dependencies
- Check other terminal windows - other agents may be working
- Marcus assigns tasks as dependencies complete
- Use `get_task_context` to debug

### Import errors after reorganization

If you see import errors like `ModuleNotFoundError: No module named 'runners'`:

```bash
# Make sure you're running scripts from the experiments directory
cd /Users/lwgray/dev/marcus/dev-tools/experiments

# Run with correct paths
python runners/run_experiment.py <experiment_dir>
python testing/test_optimal_agents.py <experiment_dir>
python analysis/parse_single_agent_timing.py <log_file>
```

### Merge conflicts on main

- Rare - Marcus coordinates to prevent this
- If it happens: manually resolve and commit
- Agents continue from resolved state

---

## 📖 Additional Resources

- **Marcus Documentation**: `/Users/lwgray/dev/marcus/docs/`
- **MCP Tools**: See Marcus MCP server docs for tool reference
- **Planka Setup**: See Marcus SETUP.md
- **Claude Code Docs**: https://docs.anthropic.com/claude/claude-code

---

## 🤝 Contributing

When adding new tools to this framework:

1. **Place in appropriate directory**:
   - Runners: `runners/`
   - Analysis: `analysis/`
   - Scoring: `scoring/`
   - Testing: `testing/`
   - Utilities: `utils/`

2. **Update this README** with:
   - Purpose and usage
   - What it does
   - Example output
   - When to use it

3. **Follow import conventions**:
   - Use relative imports for same directory
   - Use `from <subdir>.<module> import` for cross-directory
   - Add project root to `sys.path` if importing from `src/`

4. **Add docstrings**: Numpy-style for all functions

5. **Test import paths**: After moving files, verify imports still work

---

## 📜 License

Part of the Marcus Multi-Agent System (MIT License).

---

## 🗂️ Legacy Files

**Root Directory Files** (kept for compatibility):
- `single-agent-datetime-api-prompt.md` - Old single-agent prompt (v1)
- `single-agent-datetime-api-prompt-v2.md` - Old single-agent prompt (v2)
- `marcus_instances.example.json` - Example parallel instance config

These files are kept at root level for backwards compatibility but may be moved to `examples/` in the future.

---

**Last Updated**: 2026-03-17
**Version**: 2.0 (Reorganized Structure)
