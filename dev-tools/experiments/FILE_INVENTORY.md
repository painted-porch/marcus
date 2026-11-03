# Complete File Inventory - Marcus Experiments Framework

**Generated**: 2026-03-18
**Location**: `/Users/lwgray/dev/marcus/dev-tools/experiments/`
**Total Files**: 52 (excluding cache/build files)

---

## 📋 Quick Status Overview

| Category | Total Files | ✅ Functional | 📚 Documentation | 🗂️ Data/Config |
|----------|-------------|---------------|------------------|----------------|
| **Runners** | 5 | 4 scripts + 1 init | - | - |
| **Analysis** | 6 | 4 scripts + 1 init | 1 requirements.txt | - |
| **Scoring** | 4 | 3 scripts + 1 init | - | - |
| **Testing** | 5 | 3 scripts + 1 init | - | 1 data file |
| **Utils** | 2 | 1 script + 1 init | - | - |
| **Templates** | 5 | - | - | 5 templates |
| **Examples** | 1 | - | - | 1 example |
| **Docs** | 7 | - | 7 guides | - |
| **Root Level** | 5 | - | 3 docs | 2 legacy |
| **Test Projects** | 12 | - | 6 docs | 6 configs |

**Status Legend:**
- ✅ **Functional** - Tested and working
- 🔧 **Fixed** - Was broken, now fixed
- 📚 **Documentation** - Reference/guide files
- 🗂️ **Config/Data** - Configuration or data files
- 🗃️ **Legacy** - Kept for backwards compatibility
- 📁 **Directory** - Container for files

---

## 📁 Directory Structure

```
experiments/
├── runners/          # Experiment execution scripts
├── analysis/         # Analysis and visualization tools
├── scoring/          # Project evaluation scripts
├── testing/          # Testing and validation tools
├── utils/            # Utility scripts
├── templates/        # Configuration templates
├── examples/         # Example configurations
├── docs/             # Documentation and guides
├── test_projects/    # Test project specifications
├── benchmarks/       # (empty) Reserved for future use
└── mlruns/           # MLflow tracking data (not inventoried)
```

---

# 📂 DETAILED FILE INVENTORY

---

## 🏃 RUNNERS/ - Experiment Execution Scripts

**Purpose**: Main scripts for running Marcus experiments

### `__init__.py`
- **Type**: Python package marker
- **Status**: ✅ Functional
- **Purpose**: Makes `runners/` a Python package for imports
- **Size**: 0 bytes (empty file)
- **Created**: 2026-03-17 (during reorganization)

---

### `run_experiment.py`
- **Type**: Executable Python script
- **Status**: 🔧 Functional (Fixed)
- **Purpose**: **Main multi-agent experiment launcher**
- **Lines**: ~283
- **Usage**:
  ```bash
  # Initialize new experiment
  python runners/run_experiment.py --init ~/experiments/my-project

  # Validate configuration
  python runners/run_experiment.py --validate ~/experiments/my-project

  # Run experiment
  python runners/run_experiment.py ~/experiments/my-project
  ```
- **What It Does**:
  - Creates experiment directory structure
  - Copies configuration templates
  - Hands the run to `session_launcher.py`, which creates the project over MCP
  - Starts N persistent worker sessions in tmux, one worktree each
  - Each session registers with Marcus and does one chore per invocation
  - Sessions go home on `BOARD_DRAINED`; nothing restarts them
- **Key Features**:
  - Template-based setup
  - Multi-agent coordination
  - Tmux session management
  - Git repository initialization
  - Marcus MCP integration
- **Dependencies**:
  - `session_launcher.py` and `worker_host.py` (same directory)
  - `templates/` directory (parent level)
  - Marcus MCP server (must be running)
  - Claude Code CLI
- **Fixed Issues**:
  - ✅ Import path (added parent to sys.path)
  - ✅ Template directory path (now looks in parent)
- **Last Modified**: 2026-03-18

---

### `run_single_agent_experiment.py`
- **Type**: Executable Python script
- **Status**: 🔧 Functional (Fixed)
- **Purpose**: **Single-agent controlled experiments for comparison**
- **Lines**: ~1,116
- **Usage**:
  ```bash
  # Initialize single-agent experiment
  python runners/run_single_agent_experiment.py --init ~/experiments/test

  # Run experiment
  python runners/run_single_agent_experiment.py ~/experiments/test

  # Log results to MLflow after completion
  python runners/run_single_agent_experiment.py --log-results log.txt ~/experiments/test
  ```
- **What It Does**:
  - Runs same project with single Claude Code instance (no Marcus)
  - Two modes:
    - **Structured**: Uses Marcus task breakdown with checkpoints
    - **Unstructured**: Raw project description (pure baseline)
  - Time tracking with checkpoint markers
  - MLflow integration for comparison
  - Tmux session management
  - Automatic or manual result logging
- **Key Features**:
  - Checkpoint-based progress tracking
  - Timing instrumentation
  - MLflow metrics logging
  - Structured vs unstructured modes
  - Template-based prompt generation
- **Configuration**:
  - `experiment_type: "single_agent"`
  - `single_agent.mode: "structured" | "unstructured"`
  - `single_agent.checkpoint_mode: true/false`
  - `single_agent.time_tracking: true/false`
- **Dependencies**:
  - `parse_single_agent_timing.py` (analysis directory)
  - `templates/config_single_agent.yaml`
  - MLflow (optional)
  - tmux
- **Fixed Issues**:
  - ✅ Import path for parse_single_agent_timing
  - ✅ Template directory path
- **Last Modified**: 2026-03-18

---

### `run_comparison_experiment.py`
- **Type**: Executable Python script
- **Status**: 🔧 Functional (Fixed)
- **Purpose**: **Automated batch runner for comparing configurations**
- **Lines**: ~683
- **Usage**:
  ```bash
  # Dry run
  python runners/run_comparison_experiment.py --dry-run

  # Run all test projects
  python runners/run_comparison_experiment.py

  # Run specific projects
  python runners/run_comparison_experiment.py --projects test1 test2

  # Parallel mode (requires multiple Marcus instances)
  python runners/run_comparison_experiment.py --parallel --max-parallel 3
  ```
- **What It Does**:
  - Automatically runs multiple experiment configurations
  - Compares single-agent vs multi-agent performance
  - Sequential OR parallel execution modes
  - Generates comparison reports and visualizations
  - Full MLflow tracking
  - Calls analysis scripts automatically
- **Key Features**:
  - Batch experiment execution
  - Parallel execution support (multiple Marcus instances)
  - Automatic result analysis
  - Configuration detection (single vs multi-agent)
  - Progress tracking and reporting
- **Parallel Mode**:
  - Requires multiple Marcus instances on different ports
  - Each connected to separate Planka boards
  - Configuration via `marcus_instances.json`
- **Dependencies**:
  - `run_experiment.py` (same directory)
  - `run_single_agent_experiment.py` (same directory)
  - `analysis/compare_experiments.py`
  - `analysis/visualize_results.py`
  - Multiple Marcus instances (for parallel mode)
- **Fixed Issues**:
  - ✅ Import path
  - ✅ Script paths (now finds scripts in subdirectories)
- **Last Modified**: 2026-03-18

---

### `session_launcher.py`
- **Type**: Python module
- **Status**: ✅ Functional
- **Purpose**: **Start N worker sessions and wait for them to go home**
- **Usage**: Called by `run_experiment.py`
- **What It Does**:
  - Initialises the implementation repository on `main`
  - Writes the one worker contract (`prompts/Agent_prompt.md`) into `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`
  - Calls `create_project` over MCP (no project-creator agent) and commits the scaffold
  - Starts one session per identity in `worktrees/<agent_id>`, one chore per harness invocation; fresh worktree and branch from `main` per finished chore, worktree removed on exit or kill (#628)
  - Waits for every session to exit on `BOARD_DRAINED`, prints the board counts, tears down tmux
  - Mirrors progress to `logs/runner.log`
- **Key Classes**: `SessionLauncher`
- **Dependencies**: `worker_host.py`, `harness.py`, `marcus_client.py`, `templates/session_prompt.md`, tmux, the Marcus MCP server
- **Last Modified**: 2026-11-03

---

### `worker_host.py`
- **Type**: Python module
- **Status**: ✅ Functional
- **Purpose**: **Experiment config, tmux, worktrees, harness command**
- **Usage**: Used by `session_launcher.py` (not run directly)
- **What It Does**:
  - Defines `ExperimentConfig` (reads `config.yaml`, resolves `sessions`, harness, and agent model)
  - Defines `WorkerHost` (tmux session, one detached window per session, per-session worktree, harness command, Claude trust-file seeding)
  - Starts nothing on its own; it has no loop, watchdog, or notion of a run
- **Key Classes**: `ExperimentConfig`, `WorkerHost`
- **Dependencies**: `harness.py`, tmux, git
- **Last Modified**: 2026-11-03

---


## 📊 ANALYSIS/ - Analysis and Visualization Tools

**Purpose**: Tools for analyzing experiment results and generating insights

### `__init__.py`
- **Type**: Python package marker
- **Status**: ✅ Functional
- **Purpose**: Makes `analysis/` a Python package
- **Size**: 0 bytes

---

### `analyze_reliability_decay.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Compare Marcus vs multiplicative reliability decay model**
- **Lines**: ~393
- **Usage**:
  ```bash
  python analysis/analyze_reliability_decay.py \
    --experiments "Test: 5 Agents" "Test: 10 Agents" \
    --output-dir results/reliability/
  ```
- **What It Does**:
  - Tests the claim that multi-agent systems suffer from multiplicative error propagation (p^n)
  - Compares actual Marcus performance vs predicted decay
  - Generates plots showing Marcus prevents error cascade
  - Proves board-mediated coordination advantage
  - Creates comprehensive analysis report
- **Key Insight**: Marcus's board-mediated architecture prevents reliability decay that plagues pipeline-style multi-agent systems
- **Output**:
  - `reliability_comparison.png` - Visual comparison graph
  - `reliability_comparison_table.csv` - Data table
  - `RELIABILITY_ANALYSIS.md` - Comprehensive report
- **Dependencies**:
  - MLflow (for loading experiment data)
  - matplotlib, pandas, seaborn
  - Experiment results in MLflow
- **Mathematical Model**:
  - Pipeline: P(success) = p₁ × p₂ × ... × pₙ
  - Marcus: P(success) ≈ constant (no cascade)
- **Last Modified**: 2026-03-17

---

### `compare_experiments.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Compare results across multiple MLflow experiments**
- **Lines**: ~500+ (estimated)
- **Usage**:
  ```bash
  python analysis/compare_experiments.py \
    --experiments "Exp1" "Exp2" \
    --report comparison_report.json
  ```
- **What It Does**:
  - Extracts metrics from MLflow runs
  - Generates comparison reports (JSON)
  - Statistical analysis of performance
  - Identifies best configurations
  - Aggregates results across runs
- **Output**: JSON comparison report with metrics
- **Dependencies**:
  - MLflow
  - pandas
- **Called By**: `run_comparison_experiment.py`
- **Last Modified**: 2026-03-16

---

### `visualize_results.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Create visualizations from experiment results**
- **Lines**: ~400+ (estimated)
- **Usage**:
  ```bash
  python analysis/visualize_results.py \
    --experiments "Exp1" "Exp2" \
    --output-dir plots/
  ```
- **What It Does**:
  - Creates time comparison charts
  - Quality score distributions
  - Agent utilization plots
  - Performance trend analysis
  - Saves plots to output directory
- **Output**: PNG/SVG plots in specified directory
- **Dependencies**:
  - MLflow
  - matplotlib, seaborn
  - pandas
- **Called By**: `run_comparison_experiment.py`
- **Last Modified**: 2026-03-16

---

### `parse_single_agent_timing.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Extract timing data from single-agent experiment logs**
- **Lines**: ~374
- **Usage**:
  ```bash
  # Display summary
  python analysis/parse_single_agent_timing.py logs/experiment.log

  # Output JSON
  python analysis/parse_single_agent_timing.py logs/experiment.log --json

  # Save metrics
  python analysis/parse_single_agent_timing.py logs/experiment.log --output metrics.json
  ```
- **What It Does**:
  - Parses checkpoint timestamps from logs
  - Calculates total duration
  - Extracts subtask completion times
  - Formats for MLflow logging
  - Handles both structured and unstructured timing formats
- **Expected Log Format**:
  ```
  START: 2025-10-23 10:15:32
  SUBTASK 1.1 COMPLETE: 10:18:45 (3:13 elapsed)
  SUBTASK 1.2 COMPLETE: 10:22:30 (6:58 elapsed)
  TASK 1 COMPLETE: 10:25:00
  END: 2025-10-23 10:47:18
  TOTAL: 31 minutes 46 seconds
  ```
- **Output**: JSON metrics or human-readable summary
- **Called By**: `run_single_agent_experiment.py`
- **Last Modified**: 2026-03-16

---

### `requirements.txt`
- **Type**: Python dependencies file
- **Status**: ✅ Functional
- **Purpose**: **Analysis tool dependencies**
- **Contents**:
  ```
  matplotlib
  seaborn
  pandas
  numpy
  ```
- **Installation**:
  ```bash
  pip install -r analysis/requirements.txt
  ```
- **Used By**: All analysis and visualization scripts
- **Last Modified**: 2026-03-16

---

## 🎯 SCORING/ - Project Evaluation Tools

**Purpose**: Automated project scoring and comparison

### `__init__.py`
- **Type**: Python package marker
- **Status**: ✅ Functional
- **Purpose**: Makes `scoring/` a Python package
- **Size**: 0 bytes

---

### `score_project.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Rule-based automated project scoring**
- **Lines**: ~671
- **Usage**:
  ```bash
  python scoring/score_project.py \
    --project-dir ~/experiments/test1/implementation \
    --output-file score.json
  ```
- **What It Does**:
  - Evaluates projects across 6 categories (100 points total)
  - Rule-based heuristic analysis
  - Fast, deterministic scoring
  - No external API calls required
- **Scoring Categories** (100 points total):
  1. **Functionality (25 pts)**: Does it run? Do tests pass?
  2. **Code Quality (20 pts)**: Static analysis, complexity, documentation
  3. **Completeness (20 pts)**: All deliverables, no stubs/TODOs
  4. **Project Structure (15 pts)**: Organization, file count
  5. **Documentation (12 pts)**: README, API docs, setup instructions
  6. **Usability (8 pts)**: Single-command startup, examples
- **Scoring Method**:
  - File pattern matching
  - Code heuristics (docstring ratio, file size, etc.)
  - Content keyword search
  - No code execution
- **Output**: JSON with scores, percentages, and details
- **Advantages**:
  - Fast execution
  - No cost (no API calls)
  - Deterministic
  - Good for batch scoring
- **Limitations**:
  - Heuristic-based (may miss nuances)
  - Cannot evaluate actual functionality
  - No understanding of code logic
- **Last Modified**: 2026-03-16

---

### `llm_score_project.py`
- **Type**: Executable Python script
- **Status**: 🔧 Functional (Fixed)
- **Purpose**: **LLM-based intelligent project scoring**
- **Lines**: ~600+ (estimated)
- **Usage**:
  ```bash
  python scoring/llm_score_project.py \
    --project-dir ~/experiments/test1/implementation \
    --output-file llm_score.json
  ```
- **What It Does**:
  - Uses LLM (via Marcus LLMAbstraction) to evaluate code
  - Same categories as `score_project.py` but with qualitative reasoning
  - Provides strengths, weaknesses, and recommendations
  - More nuanced understanding of architecture and design
- **Scoring Method**:
  - Sends code to LLM for analysis
  - Asks for category-specific evaluation
  - Extracts structured JSON responses
  - Includes reasoning and feedback
- **Output**: JSON with scores + reasoning + strengths + weaknesses + recommendations
- **Advantages**:
  - Understands code intent and design patterns
  - Evaluates architecture quality
  - Provides actionable feedback
  - More accurate for complex projects
- **Limitations**:
  - Requires API access (costs money)
  - Slower than rule-based
  - Non-deterministic
  - Requires Marcus LLMAbstraction
- **Dependencies**:
  - `src.ai.providers.llm_abstraction.LLMAbstraction`
  - LLM API access (OpenAI, Anthropic, etc.)
- **Fixed Issues**:
  - ✅ Marcus root path calculation
- **Last Modified**: 2026-03-18

---

### `compare_scores.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Generate comprehensive comparison reports**
- **Lines**: ~381
- **Usage**:
  ```bash
  python scoring/compare_scores.py \
    --marcus marcus_score.json \
    --single single_score.json \
    --time-marcus 6.5 \
    --time-single 42.0 \
    --output comparison_report.md
  ```
- **What It Does**:
  - Compares Marcus vs Single Agent scores
  - Category-by-category breakdown
  - Identifies strengths and weaknesses
  - Quality/time ratio analysis
  - Winner determination
  - Generates markdown report
- **Input**: Two score JSON files (from score_project.py or llm_score_project.py)
- **Output**: Comprehensive markdown comparison report with:
  - Overall scores table
  - Category breakdown
  - Marcus strengths
  - Single agent strengths
  - Detailed analysis
  - Recommendations
- **Optional**: Time tracking for quality/time ratios
- **Last Modified**: 2026-03-16

---

## 🧪 TESTING/ - Testing and Validation Tools

**Purpose**: Tools for testing Marcus functionality and optimal configurations

### `__init__.py`
- **Type**: Python package marker
- **Status**: ✅ Functional
- **Purpose**: Makes `testing/` a Python package
- **Size**: 0 bytes

---

### `test_optimal_agents.py`
- **Type**: Executable Python script
- **Status**: ✅ Functional
- **Purpose**: **Calculate optimal agent count BEFORE running experiments**
- **Lines**: ~384
- **Usage**:
  ```bash
  python testing/test_optimal_agents.py ~/experiments/my-project
  ```
- **What It Does**:
  1. Reads experiment configuration
  2. Creates project in Marcus/Planka
  3. Calls `get_optimal_agent_count` MCP tool
  4. Analyzes task dependency graph
  5. Calculates maximum parallelism
  6. Shows optimal agent configuration
  7. Offers to update `config.yaml`
- **Output Example**:
  ```
  📊 Project Analysis:
     Total tasks: 87
     Critical path: 12.50 hours
     Max parallelism: 12 tasks can run simultaneously

  ✅ RECOMMENDED: 12 agents

  ⚠️  WARNING: You have 6 more agents than needed
  ```
- **Why This Matters**:
  - Prevents spawning too many agents (waste)
  - Prevents spawning too few agents (missed parallelism)
  - Avoids agents dying while waiting
  - Optimizes cost and performance
- **Dependencies**:
  - Claude Code CLI
  - Marcus MCP server
  - Planka board
- **Method**: Uses Claude Code to call Marcus MCP tools
- **Last Modified**: 2026-03-16

---

### `create_and_analyze_project.py`
- **Type**: Executable Python script
- **Status**: 🔧 Functional (Fixed)
- **Purpose**: **Direct API approach for project creation & analysis**
- **Lines**: ~271
- **Usage**:
  ```bash
  python testing/create_and_analyze_project.py ~/experiments/test1
  ```
- **What It Does**:
  - Bypasses Claude Code CLI
  - Directly uses Marcus HTTP API (Inspector client)
  - Creates project and gets optimal agent count
  - Faster for testing/debugging
  - Same output as `test_optimal_agents.py`
- **Use Cases**:
  - Testing Marcus API directly
  - Debugging project creation
  - Automating batch project analysis
  - When Claude Code CLI has issues
- **Dependencies**:
  - `src.worker.inspector.Inspector`
  - Marcus HTTP API (localhost:4298)
  - asyncio
- **Method**: Direct HTTP API calls via Inspector client
- **Fixed Issues**:
  - ✅ Marcus root path for src imports
- **Last Modified**: 2026-03-18

---

### `analyze_100_tasks.py`
- **Type**: Executable Python script
- **Status**: 🔧 Functional (Fixed)
- **Purpose**: **Test scheduler with 100 independent tasks**
- **Lines**: ~158
- **Usage**:
  ```bash
  python testing/analyze_100_tasks.py
  ```
- **What It Does**:
  - Loads `100_independent_tasks.json`
  - Runs `calculate_optimal_agents` scheduler
  - Verifies sweep-line algorithm correctness
  - Ensures truly independent tasks all run in parallel
  - Tests edge cases
- **Expected Result**:
  ```
  ✅ SWEEP-LINE ALGORITHM WORKS CORRECTLY!
     All 100 tasks detected as running in parallel.
  ```
- **Use Cases**:
  - Validating scheduler fixes
  - Testing edge cases
  - Performance benchmarking
  - Regression testing
- **Dependencies**:
  - `src.core.models` (Task, Priority, TaskStatus)
  - `src.marcus_mcp.coordinator.scheduler`
  - `100_independent_tasks.json` (same directory)
- **Fixed Issues**:
  - ✅ Marcus root path for src imports
- **Last Modified**: 2026-03-18

---

### `100_independent_tasks.json`
- **Type**: JSON data file
- **Status**: ✅ Functional
- **Purpose**: **Test data for scheduler validation**
- **Size**: ~50KB (estimated)
- **Contents**: 100 Task objects serialized as JSON
- **Characteristics**:
  - All tasks are independent (no dependencies)
  - All should run in parallel
  - Used to test scheduler algorithm
- **Used By**: `analyze_100_tasks.py`
- **Format**: JSON array of task dictionaries with all Task fields
- **Last Modified**: Date unknown

---

## 🛠️ UTILS/ - Utility Scripts

**Purpose**: Helper scripts for experiment management

### `__init__.py`
- **Type**: Python package marker
- **Status**: ✅ Functional
- **Purpose**: Makes `utils/` a Python package
- **Size**: 0 bytes

---

### `clean_experiment.sh`
- **Type**: Bash script
- **Status**: ✅ Functional
- **Purpose**: **Clean experiment directory for re-runs**
- **Lines**: ~79
- **Usage**:
  ```bash
  ./utils/clean_experiment.sh ~/experiments/my-project
  ```
- **What It Does**:
  - Deletes all contents of `prompts/`
  - Deletes `project_info.json`
  - Deletes all contents of `implementation/` (including `.git`)
  - Prompts for confirmation before deletion
  - Validates directory exists
- **Use Before**:
  - Re-running an experiment
  - Testing configuration changes
  - Resetting experiment state
- **Warning**: ⚠️ Destructive! Make sure you've saved important results
- **Safety Features**:
  - Confirmation prompt
  - Verbose output
  - Path expansion (handles ~)
  - Error checking
- **Last Modified**: 2026-03-17 (made executable during reorganization)

---

## 📄 TEMPLATES/ - Configuration Templates

**Purpose**: Template files for creating new experiments

### `config.yaml.template`
- **Type**: YAML configuration template
- **Status**: ✅ Functional
- **Purpose**: **Full-featured multi-agent configuration template**
- **Size**: ~2.4KB
- **Contents**:
  - Multiple agent definitions example
  - Skill specifications
  - Subagent counts
  - MLflow tracking options
  - Timeout configurations
  - Project options (complexity, provider)
- **Used By**: `run_experiment.py --init`
- **Copied To**: `<experiment_dir>/config.yaml`
- **Example Agents**:
  - Backend developer (Python, FastAPI, PostgreSQL)
  - Frontend developer (React, TypeScript, Tailwind)
  - QA engineer (pytest, testing)
- **Last Modified**: 2026-03-16

---


### `config_single_agent.yaml`
- **Type**: YAML configuration template
- **Status**: ✅ Functional
- **Purpose**: **Single-agent experiment configuration**
- **Size**: ~1.6KB
- **Contents**:
  - `experiment_type: "single_agent"`
  - Single agent settings (model, mode, tracking)
  - Marcus baseline comparison settings
  - Timeout configurations
- **Modes**:
  - `structured`: Uses task breakdown with checkpoints
  - `unstructured`: Raw description only
- **Used By**: `run_single_agent_experiment.py --init`
- **Last Modified**: 2026-03-16

---

### `session_prompt.md`
- **Type**: Markdown template
- **Status**: ✅ Functional
- **Purpose**: **The per-chore session header handed to every worker; the contract itself is `prompts/Agent_prompt.md`**
- **Contents**:
  - Session identity and worktree, and a pointer to the contract in `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`
  - One chore per invocation and the two harness markers
  - The exit rules for this invocation (`BOARD_DRAINED`, retry, chore done)
- **Used By**: `session_launcher.py` (rendered per session and piped to the harness)
- **Variables Replaced**: `{agent_id}`, `{agent_name}`, `{agent_role}`, `{agent_skills}`, `{workspace}`, `{branch}`
- **Last Modified**: 2026-11-03

---


### `single_agent_prompt.template.md`
- **Type**: Markdown template
- **Status**: 🗃️ Legacy (now generated dynamically)
- **Purpose**: **Single-agent prompt template (deprecated)**
- **Size**: ~1.8KB
- **Status Note**: Now generated dynamically in `run_single_agent_experiment.py`
- **Historical**: Was used before dynamic prompt generation
- **Keep**: Yes, for reference/backwards compatibility
- **Last Modified**: 2026-03-16

---

## 📖 EXAMPLES/ - Example Configurations

**Purpose**: Real-world example configurations

### `task_management_api.yaml`
- **Type**: YAML configuration example
- **Status**: ✅ Functional
- **Purpose**: **Complete example for REST API project**
- **Size**: ~1.7KB
- **Project**: Task Management API with auth, users, projects, tasks, comments
- **Agents** (4 agents, 13 subagents total):
  - Foundation (5 subagents): Database models, migrations
  - Auth (3 subagents): JWT, bcrypt, endpoints
  - API (3 subagents): CRUD endpoints
  - Integration (2 subagents): E2E tests, validation
- **Use Case**: Copy and adapt for similar API projects
- **Command**:
  ```bash
  cp examples/task_management_api.yaml ~/experiments/my-api/config.yaml
  ```
- **Last Modified**: 2026-03-16

---

## 📝 DOCS/ - Documentation and Guides

**Purpose**: Reference documentation and guides

### `EXPERIMENT_GUIDE.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Comprehensive experiment design guide**
- **Contents**: (File exists, content not inventoried)
- **Use Case**: Learn how to design experiments
- **Last Modified**: Unknown

---

### `EXPERIMENT-PROTOCOL.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Experiment execution protocol**
- **Contents**: (File exists, content not inventoried)
- **Use Case**: Standard operating procedures
- **Last Modified**: Unknown

---

### `experiment-tracking-sheet.md`
- **Type**: Markdown template
- **Status**: 📚 Documentation
- **Purpose**: **Manual experiment tracking template**
- **Contents**: Template for recording experiment progress and results
- **Use Case**: Manual tracking when MLflow isn't available
- **Moved From**: Root directory (2026-03-17)

---

### `OPTIMAL_AGENTS.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Detailed explanation of optimal agent calculation**
- **Contents**: (File exists, content not inventoried)
- **Use Case**: Understanding the algorithm behind agent recommendations
- **Referenced By**: README.md
- **Last Modified**: Unknown

---

### `PROJECT-SCORING-RUBRIC.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Detailed scoring rubric for projects**
- **Contents**: (File exists, content not inventoried)
- **Use Case**: Understanding how projects are scored
- **Related**: `scoring/score_project.py` implements this rubric
- **Last Modified**: Unknown

---

### `SCORING-GUIDE.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Guide for using scoring tools**
- **Contents**: (File exists, content not inventoried)
- **Use Case**: How to score and compare projects
- **Last Modified**: Unknown

---

### `TIMING-INSTRUCTIONS.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Detailed time tracking instructions**
- **Contents**: Instructions for time tracking in experiments
- **Use Case**: Ensuring consistent timing methodology
- **Moved From**: Root directory (2026-03-17)
- **Last Modified**: 2026-03-16

---

## 🧪 TEST_PROJECTS/ - Test Project Specifications

**Purpose**: Pre-defined test projects for experiments

### `parallel_coordination_test/`
- **Type**: Test project directory
- **Status**: 🗂️ Test specification
- **Purpose**: **Test parallel agent coordination**
- **Files**:
  - `EXPERIMENT_DESIGN.md` - Design documentation
- **Use Case**: Testing agent coordination patterns
- **Last Modified**: Unknown

---

### `reliability_decay_test/`
- **Type**: Test project directory
- **Status**: 🗂️ Test specification
- **Purpose**: **Test reliability decay hypothesis**
- **Files**:
  - `config_5_stages_1_agent.yaml` - 1 agent baseline
  - `config_5_stages_5_agents.yaml` - 5 agents (5 stages)
  - `config_10_stages_10_agents.yaml` - 10 agents (10 stages)
  - `project_description.txt` - 5-stage project spec
  - `project_description_10_stages.txt` - 10-stage project spec
  - `EXPERIMENT_DESIGN.md` - Original design
  - `FAIRER_EXPERIMENT_DESIGN.md` - Improved design
  - `RESPONSE_TEMPLATE.md` - Response format
  - `README.md` - Project overview
- **Purpose**: Test whether Marcus avoids multiplicative reliability decay
- **Theory**: Pipeline systems: P(success) = p^n, Marcus should stay flat
- **Used By**: `analyze_reliability_decay.py`
- **Last Modified**: 2026-03-17

---

## 📄 ROOT LEVEL FILES

### `README.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation (Completely rewritten)
- **Purpose**: **Main comprehensive framework documentation**
- **Size**: ~21.8KB
- **Last Updated**: 2026-03-17 (complete rewrite)
- **Contents**:
  - Directory structure overview
  - Quick start guide
  - Detailed component documentation for EVERY script
  - Common workflows
  - MLflow integration guide
  - Troubleshooting
  - Prerequisites
  - Contributing guidelines
- **Sections**:
  - 📂 Directory Structure
  - 🚀 Quick Start (4 steps)
  - 📚 Detailed Component Documentation
    - Runners (4 scripts)
    - Analysis (5 scripts)
    - Scoring (3 scripts)
    - Testing (3 scripts)
    - Utils (1 script)
  - 📄 Templates
  - 📖 Examples
  - 📝 Docs
  - 🔬 Common Workflows
  - 📊 MLflow Integration
  - 🎯 Prerequisites
  - 🚨 Troubleshooting
- **Quality**: Comprehensive, well-organized, user-friendly

---

### `REORGANIZATION_SUMMARY.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Complete record of reorganization changes**
- **Size**: ~8.8KB
- **Created**: 2026-03-17
- **Contents**:
  - Before/after directory structure
  - Complete file movement log
  - Import changes made
  - Breaking changes documented
  - Verification checklist
  - Benefits of reorganization
  - Testing recommendations
- **Use Case**: Understanding what changed during reorganization

---

### `IMPORT_FIXES.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Record of import fixes applied**
- **Size**: ~3.6KB
- **Created**: 2026-03-18
- **Contents**:
  - Problem description
  - Root cause analysis
  - All fixes applied
  - Verification tests
  - Summary of changes
- **Use Case**: Understanding import issues and their solutions

---

### `ALL_FIXES_COMPLETE.md`
- **Type**: Markdown documentation
- **Status**: 📚 Documentation
- **Purpose**: **Comprehensive record of ALL fixes**
- **Size**: ~6.9KB
- **Created**: 2026-03-18
- **Contents**:
  - Complete testing results (12/12 passing)
  - All fixes applied across all directories
  - Path calculation reference
  - Verification commands
  - Before/after comparisons
  - Complete summary
- **Use Case**: Final verification that everything works

---

### `marcus_instances.example.json`
- **Type**: JSON configuration example
- **Status**: 🗂️ Configuration example
- **Purpose**: **Example parallel instance configuration**
- **Size**: ~255 bytes
- **Contents**: Example configuration for multiple Marcus instances
- **Format**:
  ```json
  [
    {"url": "http://localhost:4298", "board_id": "board_0"},
    {"url": "http://localhost:4299", "board_id": "board_1"},
    {"url": "http://localhost:4300", "board_id": "board_2"}
  ]
  ```
- **Used By**: `run_comparison_experiment.py --parallel`
- **Use Case**: Running experiments in parallel across multiple instances

---

### `single-agent-datetime-api-prompt.md`
- **Type**: Markdown prompt (legacy)
- **Status**: 🗃️ Legacy
- **Purpose**: **Old single-agent prompt (v1)**
- **Size**: ~10.8KB
- **Status**: Kept for backwards compatibility/reference
- **Note**: Now generated dynamically by `run_single_agent_experiment.py`
- **Keep**: Yes, for historical reference
- **Last Modified**: 2026-03-16

---

### `single-agent-datetime-api-prompt-v2.md`
- **Type**: Markdown prompt (legacy)
- **Status**: 🗃️ Legacy
- **Purpose**: **Old single-agent prompt (v2)**
- **Size**: ~11.2KB
- **Status**: Kept for backwards compatibility/reference
- **Note**: Superseded by dynamic prompt generation
- **Keep**: Yes, for historical reference
- **Last Modified**: 2026-03-16

---

## 📁 SPECIAL DIRECTORIES

### `benchmarks/`
- **Type**: Directory
- **Status**: 📁 Empty (reserved)
- **Purpose**: **Reserved for future benchmarking tools**
- **Contents**: Empty
- **Planned Use**: Performance benchmarking scripts

---

### `mlruns/`
- **Type**: Directory (MLflow)
- **Status**: 📁 Data storage (not inventoried)
- **Purpose**: **MLflow experiment tracking data**
- **Contents**: MLflow run data, metrics, artifacts
- **Size**: Varies (can be large)
- **Management**: Managed by MLflow
- **Not Included**: In version control (.gitignore)

---

## 📊 SUMMARY STATISTICS

### By Status
- ✅ **Functional**: 20 scripts (all tested and working)
- 📚 **Documentation**: 11 files (guides, templates, references)
- 🗂️ **Config/Data**: 9 files (templates, examples, data)
- 🗃️ **Legacy**: 2 files (kept for compatibility)
- 📁 **Directories**: 10 (including empty benchmarks/)

### By Category
- **Executable Scripts**: 20 (Python + 1 Bash)
- **Templates**: 5
- **Documentation**: 14
- **Configuration Files**: 4
- **Data Files**: 1
- **Python Package Markers**: 5 (__init__.py)

### Lines of Code (Estimated)
- **Total Python Code**: ~7,000+ lines
- **Largest Script**: `run_single_agent_experiment.py` (~1,116 lines)
- **Documentation**: ~50+ KB of markdown

### Recent Changes (2026-03-17 to 2026-03-18)
- 📁 Reorganized from flat to categorized structure
- 🔧 Fixed 6 import issues
- 📝 Completely rewrote README.md
- ✅ All 20 scripts tested and working
- 📚 Created 3 new documentation files

---

## ✅ FUNCTIONAL STATUS BY SCRIPT

All scripts are now **100% functional** after fixes applied on 2026-03-18.

### Runners (5/5 working)
- ✅ `run_experiment.py`
- ✅ `session_launcher.py`
- ✅ `worker_host.py`
- ✅ `run_single_agent_experiment.py`
- ✅ `run_comparison_experiment.py`

### Analysis (4/4 working)
- ✅ `analyze_reliability_decay.py`
- ✅ `compare_experiments.py`
- ✅ `visualize_results.py`
- ✅ `parse_single_agent_timing.py`

### Scoring (3/3 working)
- ✅ `score_project.py`
- ✅ `llm_score_project.py`
- ✅ `compare_scores.py`

### Testing (3/3 working)
- ✅ `test_optimal_agents.py`
- ✅ `create_and_analyze_project.py`
- ✅ `analyze_100_tasks.py`

### Utils (1/1 working)
- ✅ `clean_experiment.sh`

**Total: 15/15 executable scripts fully functional** ✅

---

## 🔍 QUICK REFERENCE

### Most Important Files
1. **`README.md`** - Start here for comprehensive guide
2. **`runners/run_experiment.py`** - Main multi-agent runner
3. **`runners/run_single_agent_experiment.py`** - Single-agent baseline
4. **`testing/test_optimal_agents.py`** - Find optimal config before running
5. **`scoring/score_project.py`** - Evaluate results

### Common Workflows
1. **Run single experiment**: `runners/run_experiment.py`
2. **Compare single vs multi**: `runners/run_comparison_experiment.py`
3. **Test reliability theory**: `analysis/analyze_reliability_decay.py`
4. **Score and compare**: `scoring/score_project.py` → `scoring/compare_scores.py`

### Documentation to Read
1. `README.md` - Overall framework guide
2. `docs/OPTIMAL_AGENTS.md` - Understanding agent optimization
3. `docs/PROJECT-SCORING-RUBRIC.md` - How scoring works
4. `ALL_FIXES_COMPLETE.md` - Recent fixes reference

---

**Last Updated**: 2026-03-18
**Maintained By**: Marcus Development Team
**Version**: 2.0 (Post-Reorganization)
