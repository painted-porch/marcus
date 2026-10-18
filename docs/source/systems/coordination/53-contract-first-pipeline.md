# Contract-First Pipeline

## Status

| Field | Value |
|-------|-------|
| Status | Implemented |
| Version | 1.0 |
| Date | 2026-04-11 |
| Issue | GH-320 (PRs #330-#335) |

## Problem

Feature-based decomposition produces Single-Author Products on tightly-coupled projects. One agent absorbs shared infrastructure and the contribution split skews to 90/10 or worse. See [Contract-First Decomposition](../../concepts/contract-first-decomposition.md) for the conceptual background.

## Solution

Contract-first decomposition generates interface contracts between functional domains before splitting the project into tasks. The pipeline lives in `_try_contract_first_decomposition` in `src/integrations/nlp_tools.py` and integrates with the existing design autocomplete system via a `pre_generated_content` parameter on `_run_design_phase`.

## Pipeline Stages

```
_try_contract_first_decomposition()
  ├── 1. Domain discovery from PRD analysis
  ├── 2. _generate_contracts_by_domain()
  │     └── one LLM call per domain, FORBIDDEN PATTERNS enforced
  ├── 3. check_contract_cross_file_consistency()  [Invariant 5]
  │     ├── FAIL → fallback to feature-based
  │     └── PASS → continue
  ├── 4. decompose_by_contract()
  │     └── splits requirements into domain-scoped tasks
  ├── 5. Ghost synthesis
  │     └── one DONE design task per domain
  └── 6. Return tasks + pre_generated_content for Phase B
```

### Stage 1: Domain Discovery

The PRD analysis identifies functional domains in the project. Each domain represents a coherent area of responsibility (e.g., "weather-service", "time-widget", "dashboard-layout"). Domain boundaries are derived from the project requirements, not from file structure or technology choices.

### Stage 2: Contract Generation

`_generate_contracts_by_domain` makes one LLM call per domain. Each call produces an interface contract document specifying:

- Data types shared with other domains
- API surface (endpoints, function signatures, event schemas)
- Integration points (what this domain consumes from others)

**Scope clamping (PR #330):** The `_ARTIFACT_PROMPT` and `_INTERFACE_CONTRACTS_PROMPT` templates include FORBIDDEN PATTERNS that prevent the LLM from generating contracts outside the domain's scope. Without this, the LLM would routinely generate contracts for adjacent domains, causing duplication and divergence.

The generated contracts are stored as an artifacts dict keyed by domain name on the `NaturalLanguageProjectCreator` instance.

### Stage 3: Invariant 5 Gate

`check_contract_cross_file_consistency` (in `src/integrations/contract_validation.py`) scans all generated contracts for type contradictions. A type contradiction occurs when two domains define the same named type with incompatible field types (e.g., domain A says `WidgetPosition.x: int` while domain B says `WidgetPosition.x: string`).

- **If contradictions are found**: the entire contract-first attempt is abandoned and the system falls back to feature-based decomposition. The rationale is that type contradictions are correctness failures that no amount of downstream work can fix.
- **If no contradictions**: the pipeline continues.

The filter matches contracts by filename pattern (`"interface-contracts" in filename`), not by `artifact_type`. This is important because the live generator emits `artifact_type="specification"` despite the contracts being interface contracts by content. This mismatch was caught by Codex P1 review.

For full specification, see [Contract Validation](../quality/contract-validation.md).

### Stage 4: Decomposition

`decompose_by_contract` receives the validated contracts and splits the project requirements into domain-scoped implementation tasks. Each task:

- Owns one domain
- References its domain's contract as the source of truth for interfaces
- Has a dependency on its domain's design ghost (Stage 5)

### Stage 5: Ghost Synthesis

For each usable contract domain, Marcus synthesizes one DONE design ghost task:

```python
{
    "name": f"Design {domain}",
    "assigned_to": "Marcus",
    "labels": ["design", "auto_completed"],
    "source_type": "contract_first_design",
    "status": "done"
}
```

Implementation tasks are linked to their domain's ghost via `source_context["contract_file"]`. This provides:

- **Cato observability**: ghosts appear in the DAG, showing that contract generation happened
- **Artifact discovery**: implementation agents call `get_task_context`, which walks dependencies and finds contract artifacts on the ghost
- **Provenance**: `source_type="contract_first_design"` identifies ghosts as contract-generated rather than manually created

**Label design (Codex P2 fix):** An earlier design used a shared `contract_first` label on all ghosts. This caused `SafetyChecker._find_related_tasks` to over-link every implementation task to every ghost (because label matching is set intersection). The fix was to drop the shared label entirely. Provenance lives on `source_type`, not labels.

### Stage 6: Phase B Handoff

The generated contracts are passed to `_run_design_phase` via the `pre_generated_content` parameter. When this parameter is supplied:

- **Phase A is skipped** entirely (no additional LLM calls needed; contracts already exist)
- **Phase B runs normally**, registering the pre-generated contracts as artifacts via `log_artifact` and `log_decision`

This reuses the existing design autocomplete infrastructure without modification. See [Design Autocomplete](52-design-autocomplete.md) for the Phase A/B lifecycle.

## Fallback Behavior

If any stage fails, the system falls back gracefully:

| Failure | Behavior |
|---------|----------|
| Domain discovery returns no domains | Feature-based decomposition |
| LLM call fails during contract generation | Feature-based decomposition |
| Invariant 5 detects type contradiction | Feature-based decomposition |
| `decompose_by_contract` fails | Feature-based decomposition |

Fallback is always to feature-based decomposition, never to an error state. The experiment continues regardless of which decomposition strategy runs.

## Supporting Fixes (PR #331, #333)

Two pre-existing bugs were fixed as part of the GH-320 work because they affected contract-first experiment evaluation.

### Validator Parser Rewrite (PR #331)

`_parse_text_response` in `src/ai/validation/work_analyzer.py` was silently dropping evidence and remediation fields for every validation issue. The parser assumed each `### CRITERION` header started a new block, but `### Evidence` and `### Remediation` subheadings were also being treated as block terminators.

The fix is a two-pass block-based parser:
1. First pass: split text into blocks at `### CRITERION` headers only (not generic markdown subheadings)
2. Second pass: within each block, case-insensitive keyword scan extracts evidence and remediation

A prose fallback handles LLM responses that do not follow the expected heading structure.

### Integration Task Suppression Fix (PR #333)

`should_add_integration_task` in `src/integrations/integration_verification.py` used substring match on `"test"` to detect test-only projects. This suppressed integration tasks for any project description mentioning "test suite", "unit tests", "test coverage", etc.

The fix uses word-boundary regex plus compound phrase scrubbing: known compound phrases like "test suite" and "unit tests" are stripped before checking for standalone "test" as a keyword.

### Task Type Breakdown Fix (PR #333)

The task type breakdown in experiment logging always showed `{'unknown': N}` because `getattr(task, "task_type", "unknown")` read a non-existent attribute. The fix extracts a `_task_type_breakdown` helper that uses `EnhancedTaskClassifier` to infer task types from task metadata.

## Skill Integration (PR #332)

The `/marcus` skill accepts a `--decomposer contract_first` flag:

- `skills/marcus/SKILL.md` documents the flag
- `dev-tools/experiments/templates/config.yaml.template` includes a `decomposer` field
- `spawn_agents.py` forwards `project_options` (including `decomposer`) via `json.dumps` to `create_project`
- No runner code changes were needed because the `project_options` forwarding path already existed

## Test-Mode Event Suppression

`MarcusServer.publish_event` and `Memory.__init__` use fire-and-forget `asyncio.create_task` calls that produce "Task was destroyed but it is pending" warnings at test teardown. These are suppressed in test mode to eliminate noise in the test suite. This is not specific to contract-first but was fixed as part of the GH-320 test health work (PR #323).

## Implementation Files

| File | Purpose |
|------|---------|
| `src/integrations/nlp_tools.py` | Pipeline orchestration, contract generation, ghost synthesis, Phase B handoff |
| `src/integrations/contract_validation.py` | Invariant 5 cross-contract consistency check |
| `src/integrations/integration_verification.py` | Integration task generation with "test" substring fix |
| `src/ai/validation/work_analyzer.py` | Validator parser (two-pass block-based) |
| `src/marcus_mcp/server.py` | Test-mode event suppression |
| `src/core/memory.py` | Test-mode persistence task suppression |
| `skills/marcus/SKILL.md` | `--decomposer` flag documentation |
| `dev-tools/experiments/templates/config.yaml.template` | Decomposer config field |

## Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `tests/unit/integrations/test_contract_first_fallback.py` | 14 | Decomposition fallback, ghost synthesis, safety checker |
| `tests/unit/integrations/test_contract_validation.py` | 7 | Invariant 5 type contradiction detection |
| `tests/unit/integrations/test_design_autocomplete.py` | 3 | `pre_generated_content` Phase A skip |
| `tests/unit/integrations/test_nlp_tools.py` | 3 | Task type breakdown helper |
| `tests/unit/ai/validation/test_work_analyzer.py` | 18 | Validator parser block splitting and extraction |
| `tests/unit/integrations/test_integration_verification.py` | 43 | Integration task generation edge cases |

## See Also

- [Contract-First Decomposition](../../concepts/contract-first-decomposition.md) -- Conceptual overview, decisions, and experiment results
- [Design Autocomplete](52-design-autocomplete.md) -- Phase A/B lifecycle that contract-first builds on
- [Contract Validation](../quality/contract-validation.md) -- Invariant 5 specification
- [Task Dependency System](36-task-dependency-system.md) -- How implementation tasks depend on design ghosts
- GitHub Issues:
  - [#320](https://github.com/painted-porch/marcus/issues/320) -- Contract-first task decomposition
  - [#267](https://github.com/painted-porch/marcus/issues/267) -- Topology-aware decomposition (predecessor)
  - [#64](https://github.com/painted-porch/marcus/issues/64) -- Upstream intent preservation (next step)
