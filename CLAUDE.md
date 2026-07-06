SESSION_START:
  - At session start, read ~/.simon/memory-bank/activeContext.md for current focus and recent state.
  - Also read ~/.simon/memory-bank/progress.md for what shipped / what's left / critical reminders.
  - Optionally read the other four files in ~/.simon/memory-bank/ (projectbrief, productContext, techContext, systemPatterns) when you need project-wide context — they change rarely.
  - When the user says "update simon memory bank", review the recent conversation and rewrite activeContext.md + progress.md to match the new state. Leave the other four files alone unless the stack or patterns actually changed.

SIMON_DECISION_LOGGING:
  Automatically capture decisions, blockers, and concerns to Simon as we work on Marcus.
  Use the `simon` CLI via Bash tool. No need to ask permission for routine logging — just do it
  at the moment a decision/blocker/concern surfaces. Full automation guidelines:
  ~/.claude/skills/simon/SKILL.md

  WHAT TO LOG:
  - DECISION: significant architectural choices with rationale + alternatives
    `simon log decision "what we chose" --rationale "why" --alternatives "A,B,C" --project marcus [--issue N]`
  - BLOCKER: external dependencies or critical uncertainties stopping progress
    `simon log blocker "what is blocked" --by "what blocks it" --needs "what unblocks" --project marcus`
  - THOUGHT: risks, observations, concerns (not full blockers)
    `simon log thought "concern text" --project marcus --urgency high|medium|low [--issue N]`
  - IMPETUS: major driving forces or deadlines that shape priorities
    `simon log impetus "name" --date YYYY-MM-DD --description "..." --drives "task1,task2"`

  WHEN TO LOG:
  - At the moment of decision, not at session end (capture in-the-moment reasoning)
  - When user says "let's go with X" or "we should do Y instead of Z" — that's a decision
  - When user identifies a blocker or you discover one during work — that's a blocker
  - When risks or concerns surface during analysis — that's a thought
  - When a deadline, funding milestone, or strategic commitment is mentioned that
    shapes priorities across many decisions — that's an impetus (broader scope than
    a decision; an impetus drives multiple decisions)
  - Don't log routine Q&A, code edits, or obvious choices — only architecturally
    significant moments

  EDITING / BACKFILLING ENTRIES:
  - `simon edit <id> --issue N` — set or update issue number on an existing entry
  - `simon edit <id> --add-tag X` — add a tag (preserves existing tags)
  - `simon edit <id> --drives "1,2,3"` — replace drives list on an impetus
  - `simon edit <id> --description "..."` — update description
  - Also: --urgency, --complexity, --project, --due, --status, --reason
  - Use this to backfill issue numbers on old entries that mention #NNN in text
    but lack the issue field, so `simon find --issue N` queries work

  STATE TRANSITIONS (resolve/defer):
  Closing the loop is half the value. A logged blocker/thought that's never resolved
  becomes noise. Detect resolution moments and act on them.

  RESOLUTION TRIGGERS:
  - PR merged that addresses a logged concern
  - User says "that's done" / "we shipped X" / "fixed it" / "issue #N closed"
  - A test passes that was previously failing (logged as blocker)
  - Agent completes a task tied to a logged decision
  - Bug fix lands that resolves a logged thought

  RESOLUTION WORKFLOW:
  1. Search for related open entries:
     `simon find "<topic keywords>" --status open --ids`
  2. If a match exists, resolve with cross-reference:
     `simon resolve <id> --with "PR=#467"` or `--with "issue #463"`
  3. If multiple matches or ambiguous, surface them to the user before resolving
  4. If no match, the work wasn't pre-logged — that's fine, just continue

  EXAMPLE — Reactive resolution:
  User: "Just merged PR #467 that fixes the auto-advance bug"
  Me: [run] simon find "auto-advance" --status open --ids
       [returns: a3f2b7c1]
       simon resolve a3f2b7c1 --with "PR=#467"

  EXAMPLE — Multi-match (ask first):
  User: "Closed issue #463"
  Me: [run] simon find --issue 463 --status open
       [returns 3 entries]
       "Three open entries match issue #463. Resolve all three?"

  DEFER (push work back):
  - `simon defer <id> --reason "waiting on #469"`
  - Triggers: dependency blocker discovered, scope cut, deprioritized

  AUDIT TRAIL:
  - All resolve/defer events append to events.jsonl automatically
  - No separate command needed; state history is preserved

  IMPETUS VS DECISION:
  - Decision: "We chose X over Y" (specific choice at a point in time)
  - Impetus: "Deadline/goal Z is driving these decisions" (strategic frame, recurring influence)
  - Examples of impetus: YC application deadline, paper submission, v1.0 release,
    user research milestone, funding round close
  - When you log an impetus, also link decisions it drives via the --drives flag

  WHEN TO SYNTHESIZE:
  - End of session: `simon digest --since 24h` to summarize what happened
  - User asks "what have we been working on?": `simon find --since 7d --status open`
  - Before re-deciding something: `simon find "topic" --type decision` to check prior reasoning
  - Memory bank update: `simon digest --since 7d --update-memory-bank` (proposes diffs to activeContext.md + progress.md)

  PROJECT TAG:
  - Default to `--project marcus` for all Marcus work
  - Use `--project simon` for Simon CLI itself, `--project mini` for marcus-mini work
  - GitHub issue numbers go in `--issue` (omit the # prefix)

  WORKFLOW EXAMPLE:
  User: "We should rework agent assignment for topology awareness"
  Me: [run] simon log decision "rework agent assignment for topology awareness" \
              --rationale "loosely-coupled tasks fail without coupling graph" \
              --alternatives "stay with current,explicit deadlock prevention" \
              --project marcus --issue 449
  Me: [continue with the actual work]
  [Logging happens transparently — don't announce every log entry, just record and move on]

FILE_MANAGEMENT:
  - Don't ever version files...just change the original file.  No "_fixed", "_v2", "_patched", etc.
  - When modifying files, always overwrite the original file. Never create new versions with suffixes like _fixed, _v2, _new, _updated, _patched, or similar naming patterns.

  GIT_WORKFLOW:
  - Only merge PRs into the develop branch, NEVER into main
  - When creating pull requests, always target the develop branch as the base
  - Main branch is protected and reserved for production releases only

  COMMUNICATION_GUIDELINES:
  - Always tell me what you are going to do and why

  CODE_DOCUMENTATION:
  - Always document code with numpy-style docstrings

  ISSUE_AND_PR_WRITING_STYLE:
  All GitHub issues AND pull request descriptions you write must be readable
  by a college student who has zero prior context for this codebase. Treat
  every reader as if it is their first day on the project. This applies to
  new artifacts and to rewrites of existing ones. When in doubt, write MORE
  explanation, not less.

  ALWAYS:
  - Open with a 1-2 sentence "What is this system, briefly" paragraph that
    explains the project in plain English BEFORE introducing any internal
    concepts (Marcus, Cato, Posidonius, MCP, etc.)
  - Define EVERY internal term (PlannerContext, decomposer, blackboard,
    run_id, kanban board, etc.) the first time it is used
  - State the problem in user-facing language BEFORE the technical
    explanation. Example: "a user cannot tell which decomposer was used
    from the dashboard" BEFORE "decomposer_path is not stamped on
    token_events rows"
  - Include explicit file paths and table names with backticks
    (e.g. `src/cost_tracking/cost_store.py`, table `token_events`)
  - Show concrete worked examples with real numbers when the topic is cost,
    performance, or any measurable quantity (e.g. "5,000 tokens × 10 agents
    × $3.75/M = ~$0.19 per project")
  - Include a "Where to look in the code first" table with file → purpose
    pairs
  - Include a Glossary table if more than ~3 internal terms appear
  - Provide an explicit verification procedure ("How to verify it works")
    with numbered steps and expected results
  - End with a "Related" section listing Simon entries, sibling PRs, and
    related issues by number
  - Use numbered steps for implementation work, prose for explanation
  - Keep sentences short. Plain English first; jargon second

  NEVER:
  - Assume the reader knows what Marcus, Cato, Posidonius, MCP, blackboard
    architecture, agent invariants, or any internal concept is
  - Use unexplained acronyms or codebase-specific jargon
  - Reference "the usual pattern" or "as we did before" without naming the
    specific file or PR
  - Write architectural notes as if for a coworker who lives in this
    codebase
  - Skip the worked numerical example when the topic involves a measurable
    quantity (cost, latency, token count, row count, etc.)

  PR DESCRIPTIONS specifically must also include:
  - A "Why" section explaining the user-visible motivation before the
    "What changed" technical details
  - An explicit test plan with checkboxes the reviewer can verify

  EXEMPTIONS:
  - Trivial PRs (typo fixes, single-line config bumps, dependency upgrades)
    may use shorter descriptions but must still name the affected file(s)

  DEVELOPMENT_BEST_PRACTICES:
  - Always use TDD when developing - no exceptions
  - Always Make sure all tests pass and you have 80% coverage
  - Always fix the mypy errors for your code changes

  DATABASE_SAFETY:
  Never perform destructive database operations without explicit user confirmation.

  DESTRUCTIVE OPERATIONS (Require User Confirmation):
  - DROP TABLE/DATABASE/COLUMN/INDEX
  - TRUNCATE TABLE
  - DELETE/UPDATE without WHERE clause
  - Bulk operations affecting >100 rows
  - Schema migrations that drop/alter columns
  - Any operation on production databases

  SAFE PRACTICES:
  - Always use WHERE clauses for DELETE/UPDATE
  - Wrap multi-step operations in transactions
  - Use soft deletes (is_deleted flag) over hard deletes
  - Count affected rows before bulk operations
  - Create backups before schema changes
  - Use test databases for testing, never production


  TEST_WRITING_INSTRUCTIONS:
  When writing tests for Marcus, follow this systematic approach:

  1. Test Placement Decision:
     ```
     Does the test require external services (DB, API, network, files)?
     → NO: Write a UNIT test in tests/unit/
     → YES: Is this testing unimplemented/future features (TDD)?
            → YES: Place in tests/future_features/
            → NO: Write INTEGRATION test in tests/integration/
     ```

  2. Unit Test Placement:
     - AI/ML logic → tests/unit/ai/test_*.py
     - Core models/logic → tests/unit/core/test_*.py
     - MCP protocol → tests/unit/mcp/test_*.py
     - UI/Visualization → tests/unit/visualization/test_*.py

  3. Integration Test Placement:
     - End-to-end workflow → tests/integration/e2e/test_*.py
     - API endpoints → tests/integration/api/test_*.py
     - External services → tests/integration/external/test_*.py
     - Debugging/diagnostics → tests/integration/diagnostics/test_*.py
     - Performance → tests/performance/test_*.py

  4. Test Writing Rules:
     ALWAYS:
     - Mock ALL external dependencies in unit tests
     - Use descriptive test names: test_[what]_[when]_[expected]
     - Include docstrings explaining what each test verifies
     - Follow Arrange-Act-Assert pattern
     - One logical assertion per test
     - Unit tests must run in < 100ms

     NEVER:
     - Use real services in unit tests
     - Test implementation details
     - Create test files in root tests/ directory
     - Mix unit and integration tests
     - Leave hardcoded values - use fixtures

  5. Test Structure:
     ```python
     """
     [Unit/Integration] tests for [ComponentName]
     """
     import pytest
     from unittest.mock import Mock, AsyncMock, patch

     class TestComponentName:
         """Test suite for ComponentName"""

         @pytest.fixture
         def mock_dependency(self):
             """Create mock for dependency"""
             mock = Mock()
             mock.method = AsyncMock(return_value="expected")
             return mock

         def test_successful_operation(self, component):
             """Test component handles normal operation"""
             # Arrange
             # Act
             # Assert
     ```

  6. Test Markers:
     - @pytest.mark.unit - Fast, isolated unit test
     - @pytest.mark.integration - Requires external services
     - @pytest.mark.asyncio - Async test
     - @pytest.mark.slow - Takes > 1 second
     - @pytest.mark.kanban - Requires Kanban server

  7. Response Format:
     - State test location and reasoning
     - Explain test strategy and what will be tested
     - Show complete test file with all imports
     - Explain key decisions (mocking strategy, assertions)

  ERROR_HANDLING_FRAMEWORK:
  Use Marcus Error Framework for ALL user/agent-facing errors. Use regular Python exceptions only for internal programming errors.

  WHEN TO USE MARCUS ERRORS:
  ✅ External service calls (Kanban, AI providers, APIs)
  ✅ Agent task operations (assignment, execution, progress)
  ✅ Configuration issues (missing credentials, invalid config)
  ✅ Security violations (unauthorized access, permission denied)
  ✅ Resource problems (memory exhaustion, database failures)
  ✅ Business logic violations (workflow errors, validation failures)

  WHEN TO USE REGULAR EXCEPTIONS:
  ✅ Programming errors (ValueError, TypeError for internal validation)
  ✅ Library-specific exceptions (let them bubble up)
  ✅ Internal logic errors (KeyError for missing dict keys)

  ERROR CREATION PATTERNS:
  ```python
  # External service errors
  from src.core.error_framework import KanbanIntegrationError, ErrorContext

  try:
      await kanban_client.create_task(data)
  except httpx.TimeoutException:
      raise KanbanIntegrationError(
          board_name="project_board",
          operation="create_task",
          context=ErrorContext(
              operation="task_creation",
              agent_id=agent_id,
              task_id=task_id
          )
      )

  # Use error context manager for automatic context injection
  from src.core.error_framework import error_context

  with error_context("sync_tasks", agent_id=agent_id):
      await sync_with_kanban()  # Errors automatically get context

  # Configuration errors
  from src.core.error_framework import MissingCredentialsError

  if not os.getenv('API_KEY'):
      raise MissingCredentialsError(
          service_name="kanban",
          credential_type="API key"
      )
  ```

  RETRY AND RESILIENCE PATTERNS:
  ```python
  # Add retries for network operations
  from src.core.error_strategies import with_retry, RetryConfig

  @with_retry(RetryConfig(max_attempts=3, base_delay=1.0))
  async def call_external_service():
      return await service.call()

  # Add circuit breakers for external dependencies
  from src.core.error_strategies import with_circuit_breaker

  @with_circuit_breaker("kanban_service")
  async def sync_with_kanban():
      return await kanban.sync()

  # Add fallbacks for critical operations
  from src.core.error_strategies import with_fallback

  async def use_cached_data():
      return load_from_cache()

  @with_fallback(use_cached_data)
  async def get_live_data():
      return await fetch_from_api()
  ```

  ERROR RESPONSE PATTERNS:
  ```python
  # For MCP tool responses
  from src.core.error_responses import handle_mcp_tool_error

  async def mcp_tool_function(arguments):
      try:
          result = await operation(arguments)
          return {"success": True, "result": result}
      except Exception as e:
          return handle_mcp_tool_error(e, "tool_name", arguments)

  # For API responses
  from src.core.error_responses import create_error_response, ResponseFormat

  try:
      result = await api_operation()
      return {"success": True, "data": result}
  except Exception as e:
      return create_error_response(e, ResponseFormat.JSON_API)
  ```

  ERROR MONITORING:
  ```python
  # Record errors for monitoring and pattern detection
  from src.core.error_monitoring import record_error_for_monitoring

  try:
      await critical_operation()
  except MarcusBaseError as e:
      record_error_for_monitoring(e)
      raise
  ```

  QUICK DECISION TREE:
  - External service call → Use Marcus Integration Error + Circuit Breaker + Retry
  - Agent operation → Use Marcus Business Logic Error + Context
  - Configuration issue → Use Marcus Configuration Error (no retry)
  - Security violation → Use Marcus Security Error (no retry, alert)
  - Programming error → Use regular Python exception (ValueError, TypeError)
  - Library error → Let bubble up, convert to Marcus if user-facing

  NEVER:
  - Use generic Exception() for anything user/agent-facing
  - Retry authentication failures or validation errors
  - Skip error context for agent operations
  - Use regular exceptions for external service failures

  ITERATIVE_TESTING_APPROACH:
  1. Write ONE simple test first to understand actual behavior
  2. Run it and fix any issues before expanding
  3. Use findings to inform remaining test design
  4. Don't assume error message formats - discover them

  DISCOVERY_OVER_ASSUMPTION:
  - Run simple tests to discover actual error behavior
  - Check inheritance chains and constructor patterns
  - Verify message formats and context structure
  - Test framework quirks before building complex scenarios


  AI_ARCHITECT_PARTNER:
    Dr. Kaia Chen is personal advisory tooling provided by the /kaia skill,
    which is intentionally NOT tracked in this repository (.gitignore:
    skills/kaia/). It exists only on machines where it has been installed
    locally.
    IF (and only if) the /kaia skill appears in your available-skills list:
    when the user mentions "Kaia", "Dr. Chen", "ask Kaia", "what would Kaia
    think", or wants architectural advice, mentorship, or a second opinion,
    invoke the /kaia skill.
    Modes: /kaia <question>, /kaia --review, /kaia --research <topic>, /kaia --reflect, /kaia --chat
    If the skill is NOT available, do not attempt to invoke /kaia — provide
    the architectural advice yourself and note that the Kaia advisor is not
    installed in this environment.

  MINI_RED_LINE:
    marcus-mini is the proof-of-concept and research instrument. Marcus is the product.
    Keep mini lean. Apply this test before adding anything to mini:

    THE TEST: "Does coordination break without this feature?"
    YES = allowed.  NO = red line violation. Stop.

    WHAT THE RED LINE RULES OUT (never add to mini):
    - Observability beyond `mini status` — no dashboards, metrics pipelines, telemetry
    - Resilience infrastructure — no retry logic, circuit breakers, fallback chains
    - Rich configuration — no profiles, environments, per-agent tuning; one flat JSON file
    - External integrations — no Slack, GitHub, webhooks; mini has no outside-world opinions
    - Agent capability management — no skills, tools, or specializations; mini is capability-blind
    - Scheduling — no cron, recurring tasks, or triggers; mini runs one build at a time
    - Any feature whose primary value is demo aesthetics — if coordination works without it, stop

    WHAT STAYS, EVEN IF IT ADDS LOC:
    - Correctness fixes — stall detection, proper exit codes, accurate liveness checks
    - Coordination primitives — task claiming, dependency resolution, agent spawning
    - Measurement — bench, timing, coordination tax (this IS the point of mini as a research instrument)

    NOTE: LOC is a symptom, not the rule. Judge by the test, not the line count.
    If a feature would be at home in Marcus, it doesn't belong in mini.

  MULTIAGENCY_PROCLAMATION:
    Marcus is a board-mediated, blackboard-architecture Multi-Agent System.
    The kanban board is the shared environment. Marcus manages that environment.
    Agents operate within it independently.

    THE THREE AGENT INVARIANTS — never violate these:
    1. Agents self-select work. Agents pull tasks via request_next_task. Marcus
       never pushes work, never assigns without request, never forces a specific
       agent onto a specific task.
    2. Agents make all implementation decisions. Marcus says WHAT to build and
       WHY it matters. Marcus never dictates implementation HOW — no library
       choices, no patterns, no internal code structure. Two agents given the
       same task must be able to produce legitimately different implementations.

       Scope of "implementation" (v2 clarification): the code an agent writes
       to satisfy the task's acceptance criteria. EXCLUDES self-verification:
       how Marcus checks the work is correct is part of the task contract,
       authored by Marcus's setup-time pipeline, not the agent's discretion.
       The agent owns the bid (implementation); the contract specifies both
       the acceptance criteria AND how those criteria will be verified. This
       is the contract-net protocol pattern from 1980s MAS literature.
    3. Agents communicate exclusively through the board. No agent-to-agent
       direct communication. No Marcus-to-agent push outside task assignment.
       The board is the only channel.

    WHAT MARCUS CAN DO (coordination, not control):
    - Structure the task graph (DAG) — environment design
    - Synthesize shared foundation pre-fork tasks before agents spawn
    - Provide artifacts (design docs, contracts, scope annotations) as board state
    - Make setup-time LLM calls (domain discovery, contract generation, decomposition)
    - Observe, measure, and log coordination quality

    THE BRIGHT LINE TEST — apply to every new feature:
    "Could an agent, given the same board state, choose to do something
    meaningfully different from what Marcus suggests?"
    YES = coordination. Build it.
    NO  = control. Stop.

    RESEARCH CLAIM:
    Marcus is a legitimate MAS with: autonomous task selection, independent
    parallel execution, emergent coordination through shared board state,
    measurable contribution distribution, and fully observable board-mediated
    communication. Honest limitation: agents are reactive within a task, not
    continuously goal-pursuing across tasks (not a BDI architecture).

    VERSION HISTORY:

    v2 — 2026-05-24
    - Scope of Invariant #2 clarified. "HOW" applies to implementation only,
      not self-verification. Verification commands are part of the task
      contract Marcus authors at setup time; agents run them but don't
      author them.
    - Rationale: agent-authored verifications were gameable under retry
      pressure. Project test58 shipped an unplayable snake game because
      the integration verifier iterated verification commands until one
      passed (after multiple retries) without actually testing the
      user-facing behavior (issue #636). The contract-net-protocol framing
      preserves the MAS research claim while making the smoke gate
      reliable.
    - Driven by: issues #636 (smoke-gate ownership), #637 (decomposer
      redundant gap-fill), #638 (composition orphan detection).
    - Reviewed by: Kaia (/kaia --chat session, 2026-05-24).
    - Simon decision: see ~/.simon/decisions.jsonl for the immutable record
      stamped with this version's commit SHA.

    v1 — 2026-04 (original)
    - Three Agent Invariants established as part of the Multi-Agency
      Proclamation.
    - Driven by: original Marcus design.
