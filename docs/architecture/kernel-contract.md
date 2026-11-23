# The kernel contract

**Contract version: 1.0.0**

## What this is, briefly

Marcus is an open-source coordinator where independent AI agents pull tasks from a shared task board instead of being told what to do by a central scheduler.
It is being split into a small **kernel**, meaning the board and its rules, and **extensions**, meaning separate programs that use the board for one purpose, such as verifying citations, reviewing clinical advice or running agents in Minecraft.
Every extension reaches the kernel the same way: over MCP (Model Context Protocol, the wire protocol for the kernel's tools), usually through `marcus-toolkit`.

This document is the **door** between the two: the exact list of what an extension may rely on.
It names the tools, their arguments, their replies, the shape of a seeded card, the task-type declaration, the rules the kernel enforces on cards, how cost reaches the record, and what a reader such as the Cato dashboard may read from a kernel's data directory.
Anything not written here is not part of the door, even when a server happens to serve it today.

It was written for issue #939 (Track A1 of the modularization program, `docs/program/MODULARIZATION-PLAN.md`).
`docs/architecture/extension-contract.md` explains why the door has this shape and what the first extension cut taught; this document says what the door is.

## Who should read this

Read it if you are writing an extension, a worker for one, a dashboard that reads a kernel's files, or a kernel change.
An extension author needs sections 2 to 8.
A kernel author needs section 1 first, because a change to anything here changes the version.

## Glossary

| Term | Meaning |
| --- | --- |
| Kernel | the part of Marcus every project needs: the board, task claiming, leases, eligibility, registration and the MCP server |
| Extension | a program in its own repository that seeds cards on the board and runs its own agents against them; the kernel never calls into it |
| Door | everything in this document |
| Card | one task on the board |
| Seed spec | one entry of `create_project(options.tasks)` or `create_tasks(options.tasks)`: a card put on the board exactly as written |
| Task type | a label on a card, such as `find` or `verify`, that selects which kernel rules apply to it |
| Task-type declaration | a project's list of the card types it uses and the evidence each must carry, `options.task_type_registrations` |
| Evidence | the structured result an agent attaches when it closes a card |
| Verify card | a card whose type checks another card's work; the kernel never offers it to the agent, or the vendor, that did that work |
| Lease | the time-limited claim an agent holds on a card while it works |
| Lease epoch | a counter on a card's lease that rises each time the card is claimed again, so a report from an earlier claim can be recognised |
| Registry id | the id Marcus gives a project; agents register against it and it is the `project_id` in every reply |
| Run | one traversal of a project in the cost ledger, opened by `create_project` and closed when the board drains |
| Cato | the dashboard that reads a kernel's data directory |
| Track C | the extraction of software-building code into `painted-porch/marcus-software` (epic #924) |

## 1. How the contract is versioned

The version is the line at the top of this document.
The same number is `KERNEL_CONTRACT_VERSION` in `src/marcus_mcp/kernel_contract.py`, and a unit test fails if the two differ.

The version follows semantic versioning, applied to the door.

| Change | Bump |
| --- | --- |
| A contract tool, an argument or a reply field is removed or renamed | MAJOR |
| An optional argument becomes required, or a field's meaning changes | MAJOR |
| A rule in sections 4 to 8 becomes stricter | MAJOR |
| A contract tool, an optional argument or a reply field is added | MINOR |
| A rule in sections 4 to 8 becomes more permissive | MINOR |
| This document's wording is corrected without changing behaviour | PATCH |

Three rules sit on top of the table.

1. A pull request that changes anything in this document changes the version in the same pull request, here and in `kernel_contract.py`.
2. A kernel release states, in its release notes, the contract version it implements.
3. A proposal that adds to the door, such as views as first-class board entries (the Minecraft deliberation item on the Program board), is written as a change to this document and goes through the usual review; it is not added to the server first and documented later.

What is outside the door can change in any release without a version bump: the operator tools and the tools leaving with Track C (section 3.2 and 3.3), any reply field not listed here, and every internal module.
An extension that depends on something outside the door is depending on an accident.

## 2. Transport

**The contract surface is the HTTP server started with `--all-tools`:** `python -m src.marcus_mcp.server --http --all-tools`, which serves MCP at `/mcp/`.
Every extension in the program starts it this way, through `marcus_toolkit.server.ServerUnderTest` or the toolkit's Node client.

The stdio server (`python -m src.marcus_mcp.server` with no `--http`) is a second transport for interactive use.
It serves a subset of the tools, only after the client calls `authenticate`, and for every tool it serves it advertises the same argument names and the same required set as HTTP.
`tests/unit/marcus_mcp/test_kernel_contract_surface.py` holds stdio to that.

A reply is the tool's return value as JSON.
Over HTTP it arrives as the MCP result's `structuredContent`; a tool that returns a list arrives wrapped as `{"result": [...]}`.
A refusal is an ordinary reply with `"success": false` and a reason in `error`, `message` or both; the tool call itself still succeeds at the MCP level.
A reply may carry fields this document does not list; they are outside the door.

## 3. The tools

Every tool the HTTP server registers is in exactly one of three tiers.
The tiers are `CONTRACT_TOOLS`, `OPERATOR_TOOLS` and `LEAVING_WITH_TRACK_C` in `src/marcus_mcp/kernel_contract.py`, and a unit test fails if a registered tool is in none of them.

| Tier | Count | What it means for an extension |
| --- | --- | --- |
| Contract | 20 | Rely on it. Specified below; a change bumps the version. |
| Operator | 30 | Kernel code a person running a server uses. Do not depend on it. |
| Leaving with Track C | 5 | Software-building tools moving to `marcus-software`. Do not depend on it. |

### 3.1 Contract tools

The tools are grouped by what an extension does with them.
Each lists its arguments, exactly as the server advertises them, then its reply and the rules that apply.

#### Connectivity

##### `ping`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `echo` | string | no | `""` |

**Reply:** `success`, `status` (`"online"`), `provider` (the board provider), `echo`, `timestamp`, `server_info.instance_id`.

#### Registration and the pull loop

##### `register_agent`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `name` | string | yes |  |
| `role` | string | yes |  |
| `project_id` | string | yes |  |
| `skills` | array of string | no | `[]` |
| `vendor` | string | no | `""` |
| `principal` | string | no | `"agent"` |

**Reply:** `success`, `agent_id`, `project_id`, `message`.

**Rules.**
`project_id` is the registry id of an existing project; a registration without one is refused, and an agent that never registered is offered nothing by `request_next_task`.
The registration is the only source of the project for every later write by this agent (ADR-0013 K6): decisions, opinions, artifacts, cost and the conversation log are all filed under it.
`vendor` names who runs the agent's model; offer-time eligibility (section 6) compares it.
`principal` is `"agent"` or `"human"`; an approval card is offered only to a human, and every other card only to an agent.
`role` and `skills` are recorded and never enforced.

##### `get_agent_status`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |

**Reply:** `success` and `agent` with `id`, `name`, `role`, `skills`, `status`, `current_tasks`, `total_completed` and `performance_score`.

##### `request_next_task`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |

**Reply when a card is offered:** `success: true`, `task`, `lease_epoch` and `frontier` (`tasks_claimable`, `tasks_in_flight`, `tasks_stranded`).
The `task` carries `id`, `name`, `description`, `task_type`, `inputs`, `labels`, `project_id`, `project_name`, `priority`, `completion_criteria`, `acceptance_criteria`, `injected_checks`, `output_schema` and `instructions`.

**Reply when nothing is offered:** `success: false`, `message`, `retry_after_seconds`, `retry_reason` and `drain` (`drained`, `reason`, `tasks_total`, `tasks_done`, `tasks_blocked`, `tasks_claimable`, `tasks_in_flight`, `tasks_unclaimable`, `tasks_stranded`, `stranded_task_ids`).
`drain.drained: true` means the project's board has nothing left that any agent could ever claim, and a worker may exit.

**Rules.**
The agent chooses to ask; the kernel never pushes a card (Agent Invariant 1).
`task.inputs` is the only place a worker receives what the board holds for it: the seed spec's `inputs`, plus what upstream cards handed off when they closed (section 6).
`lease_epoch` identifies this claim; send it back on `report_task_progress` so a report from an earlier claim is recognised.
An eligibility refusal is indistinguishable from an empty board, by design (section 6).
`task.instructions` currently carries software-building text that is outside the door and leaves with Track C (steps 8b.2 and 8b.3); read `description` and `inputs` instead.

##### `report_task_progress`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `task_id` | string | yes |  |
| `status` | string | yes |  |
| `progress` | integer | no | `0` |
| `message` | string | no | `""` |
| `start_command` | string | no | `null` |
| `readiness_probe` | string | no | `null` |
| `verifications` | array | no | `null` |
| `evidence` | object | no | `null` |
| `lease_epoch` | integer | no | `null` |
| `subject` | string | no | `null` |
| `observation` | string | no | `null` |
| `cost` | array | no | `null` |

**Reply when accepted:** `success: true`, `message`, plus `marker` when a `subject` was given and `cost` when a `cost` list was.

**Reply when refused:** `success: false`, `error`, `message`, and for a missing-evidence refusal `status: "evidence_rejected"`, `error: "typed_evidence_missing"`, `task_type`, `missing_fields` and `blocker` (what to send instead); `cost` is present when a `cost` list was.

**Rules.**
`status` is `"in_progress"`, `"completed"` or `"blocked"`.
A `completed` report on a card of a declared type must carry `evidence` with every field the type requires (section 5); otherwise it is refused and the card stays with the agent.
`subject` turns the report into a counted opinion marker on that subject as well, with `observation` (or `message`) as its text; a refused report records no marker.
`cost` is specified in section 7.
`start_command`, `readiness_probe` and `verifications` are software-building checks that leave with Track C (steps 6.3 and 6.4); a non-software extension never sends them.

##### `report_blocker`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `task_id` | string | yes |  |
| `blocker_description` | string | yes |  |
| `severity` | string | no | `"medium"` |

**Reply:** `success`, `advisory`, `suggestions` and `message`.

**Rules.**
`severity` is `"low"`, `"medium"` or `"high"`.
At `low` or `medium` the report is advisory: the agent keeps the card and gets suggestions back, and a fourth advisory report on the same card is treated as `high`.
At `high`, or any other value, the agent hands the card back; it is offered to up to two further agents, within a budget of three hand-backs per card, before it stays blocked.
A blocker never means "this cannot be done"; that is `request_task_decline`.

##### `request_task_redo`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `task_id` | string | yes |  |
| `reason` | string | yes |  |
| `defect_type` | string | no | `null` |

**Reply:** `success`, `task_id`, `redo_count`, `recovery_info` and `message`.

**Rules.**
The caller must currently hold a claimed card, typically the verify card that found the defect.
The target must be done; it is reset to be claimed again by whoever asks next.
`defect_type` is `"implementation_gap"`, `"model_gap"` or `"requirement_gap"`.
A card can be sent back at most three times; the fourth request is refused with `error: "max_redo_count_exceeded"`.

##### `request_task_decline`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `task_id` | string | yes |  |
| `reason` | string | yes |  |

**Reply:** `success`, `task_id`, `status: "declined"`, `persisted` and `message`.

**Rules.**
Only the card's current holder may decline it.
Declined is terminal and distinct from done (ADR-0013 K5).
A card that depends on a declined card is never offered, except a best-effort integration card, which treats declined like blocked.

#### Reading the board and writing to it

##### `get_task_context`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `task_id` | string | yes |  |

**Reply:** `success` and `context` with `task_id`, `dependent_tasks`, `previous_implementations`, `architectural_decisions`, `artifacts`, `related_patterns`, `is_subtask` and `project_contract`.

**Rules.**
It describes the card's neighbourhood: the cards that depend on it, and the decisions and artifacts logged against related cards.
It does not carry the card's `inputs`; those arrive on `request_next_task`.

##### `get_project_status`

No arguments.

**Reply:** `success`, `project` (`id`, `name`, `provider`, `total_tasks`, `completed`, `in_progress`, `blocked`, `unfinished_tasks`, `completion_percentage`, `stranded_assignments`, `epoch_collisions`) and `workers` (`total`, `active`, `available`).

**Rules.**
It reports the server's active project, not the caller's (see section 9).

##### `log_decision`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `task_id` | string | yes |  |
| `decision` | string | yes |  |
| `subject` | string | no | `null` |

**Reply:** `success`, `decision_id`, `message`.

**Rules.**
Write the decision as "I chose X because Y. This affects Z."
`subject` names the board subject the decision answers and is normalised exactly as an opinion's subject is.
A decision never counts as an opinion or as support for proposed work.

##### `give_opinion`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `agent_id` | string | yes |  |
| `subject` | string | yes |  |
| `what` | string | yes |  |
| `why` | string | no | `""` |
| `impact` | string | no | `""` |
| `task_id` | string | no | `null` |

**Reply:** `success`, `decision_id`, `subject` (as normalised), `kind: "assessment"`, `task_id`, `project_id`, `message`.

**Rules.**
The subject is normalised to a lowercase dotted slug; only the characters `a` to `z`, `0` to `9` and `.` survive, and it is cut at 80 characters.
Opinions from different agents on the same subject are counted together by `query_project_history(query_type="opinions")`.

##### `log_artifact`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `task_id` | string | yes |  |
| `filename` | string | yes |  |
| `content` | string or object or array | yes |  |
| `artifact_type` | string | yes |  |
| `project_root` | string | no | `null` |
| `description` | string | no | `""` |
| `location` | string | no | `null` |

**Reply:** `success` and `data` with `task_id`, `filename`, `artifact_type`, `location`, `full_path`, `is_default_location` and `description`.

**Rules.**
`artifact_type` is one of `api`, `architecture`, `design`, `documentation`, `reference`, `specification` or `temporary`.
With `project_root` (an absolute path that exists on the server) the artifact is written under it, at `docs/<filename>` for documentation; without one, the content is stored on the record, for a runtime with no filesystem the server can reach (ADR-0013 K8, issue #870).
`content` may be a string or JSON.

#### Seeding

##### `create_project`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `description` | string | yes |  |
| `project_name` | string | yes |  |
| `options` | object | no | `null` |

**Reply:** `success`, `project_id` (the registry id), `project_name`, `task_ids`, `tasks_created`, `task_count`, `created_at`, `dependencies_mapped` and `actual_decomposer` (`"explicit"` for a seeded board).

**Rules.**
An extension seeds a board by sending `options.tasks`, a list of seed specs (section 4), and `options.task_type_registrations`, its task-type declaration (section 5).
With `options.tasks` the kernel creates exactly those cards and makes no model call.
`options.provider` is `"sqlite"`, the one supported provider.
`options.project_root` is required, an absolute path, even for a board with no code (see section 9).
`options.mode: "new_project"` creates a new project rather than adding to an existing one.
Every project gets one extra finished card named `About: <project name>`, so `tasks_created` is the number of seed specs plus one.
The other options (`complexity`, `deployment`, `team_size`, `tech_stack`, `deadline`) steer the software-building decomposer and leave with Track C.
The whole call is validated before anything is written; a refusal names every problem.

##### `create_tasks`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `task_description` | string | yes |  |
| `board_name` | string | no | `null` |
| `project_name` | string | no | `null` |
| `options` | object | no | `null` |

**Reply:** `success`, `tasks_created`, `tasks` (`id` and `name` of each new card), `board` (`project_id`, `board_id`, `board_name`) and `seeded`.

**Rules.**
With `options.tasks` it adds seed specs to a running board, with no model call.
A `depends_on` may name an existing card on the board, by name or id, scoped to the project; an ambiguous name is refused naming every candidate (ADR-0013 K10).
It never rewires an existing card, leased or not.
The project it writes to is the one `project_name` names, or the server's active project when `project_name` is omitted (see section 9).

##### `add_project`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `name` | string | yes |  |
| `provider` | string | yes |  |
| `config` | object | yes |  |
| `tags` | array of string | no | `[]` |
| `make_active` | boolean | no | `true` |

**Reply:** `success` and `project` (`id`, `name`, `provider`, `tags`, `is_active`).

**Rules.**
It registers a board that already exists.
For `provider: "sqlite"`, `config` must carry `project_id` and `db_path`, and may carry `project_root`.
An extension that seeds with `create_project` does not need it.

#### The record

##### `export_case`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `case_id` | string | yes |  |

**Reply:** `case_id`, `exported_at`, `summary` (`status`, `opened_at`, `closed_at`, `answer`), `tasks`, `approvals`, `refusals`, `identity_changes` and `costs`.

**Rules.**
A case is every card labelled `case:<case_id>`.
Each entry of `tasks` carries `task_id`, `name`, `task_type`, `status`, `inputs`, `evidence`, `worked_by`, `dependencies`, `created_at` and `updated_at`.
Each entry of `costs` carries `agent_id`, `task_id`, `provider`, `model`, `input_tokens`, `output_tokens`, `total_tokens`, `timestamp`, `basis`, `reported_by` and `reported_usd` (section 7).
The bundle keeps each card's `raw` evidence field, which is never handed to another agent, so it is meant for a person or an auditor, not a worker.

##### `query_project_history`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `project_id` | string | yes |  |
| `query_type` | string | yes |  |
| `status` | string | no | `null` |
| `agent_id` | string | no | `null` |
| `task_id` | string | no | `null` |
| `artifact_type` | string | no | `null` |
| `affecting_task_id` | string | no | `null` |
| `event_type` | string | no | `null` |
| `keyword` | string | no | `null` |
| `start_time` | string | no | `null` |
| `end_time` | string | no | `null` |
| `subject` | string | no | `null` |
| `kind` | string | no | `null` |
| `since` | string | no | `null` |
| `window_minutes` | integer | no | `null` |
| `limit` | integer | no | `null` |
| `offset` | integer | no | `null` |

**Reply:** `success`, `data` (a list), `count` and `pagination` (`limit`, `offset`); the `opinions` query adds `total_subjects`.

**Rules.**
`query_type` is one of `summary`, `tasks`, `blocked_tasks`, `task_dependencies`, `decisions`, `opinions`, `artifacts`, `agent_history`, `agent_metrics`, `timeline` or `conversations`.
A `decisions` entry carries `decision_id`, `agent_id`, `task_id`, `what`, `why`, `impact`, `subject`, `kind`, `affected_tasks`, `confidence` and `timestamp`.
An `opinions` entry is one subject: `subject`, `count`, `opinion_count`, `agents`, `task_ids`, `first_seen`, `last_seen` and the `opinions` themselves.
An `artifacts` entry carries `artifact_id`, `task_id`, `agent_id`, `filename`, `artifact_type`, `description`, `content` and `absolute_path`.

##### `get_cost_summary`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `run_id` | string | no | `null` |
| `project_id` | string | no | `null` |

**Reply for a project:** `project_id`, `totals` (`runs`, `events`, `total_tokens`, `total_cost_usd`) and `runs`, one summary per run.
**Reply for a run:** `summary` and the run broken down `by_role`, `by_agent`, `by_task`, `by_operation` and `by_model`.

**Rules.**
Give one of `run_id` or `project_id`; with neither the call is refused.
`total_cost_usd` is Marcus's own price, from token counts and its price table; a worker's reported `usd` is kept as provenance and never overrides it (section 7).

#### Measurement

##### `get_desired_agent_count`

| Argument | Type | Required | Default |
| --- | --- | --- | --- |
| `max_agents` | integer | no | `null` |

**Reply:** `success`, `desired_agent_count`, `unclaimed_tasks`, `in_flight_tasks`, `max_layer_width`, `total_tasks` and `max_agents`.

**Rules.**
It is the kernel's live signal for how many workers the current board could use, so a launcher can add workers as the frontier widens.

### 3.2 Operator tools, outside the door

These are kernel code that a person running a server uses.
They are served and they work, but they can change in any release, and an extension must not depend on them.

| Group | Tools |
| --- | --- |
| Access | `authenticate` |
| Project administration | `list_projects`, `select_project`, `switch_project`, `get_current_project`, `remove_project`, `update_project`, `sync_projects`, `discover_planka_projects` |
| Board administration and health | `unassign_task`, `list_registered_agents`, `get_all_board_tasks`, `check_task_dependencies`, `check_assignment_health`, `check_board_health`, `get_usage_report`, `get_optimal_agent_count`, `list_project_history_files` |
| Diagnostics | `diagnose_project`, `capture_stall_snapshot`, `replay_snapshot_conversations` |
| Board analytics | `predict_completion_time`, `predict_task_outcome`, `predict_blockage_probability`, `predict_cascade_effects`, `get_task_assignment_score`, `get_system_metrics`, `get_agent_metrics`, `get_project_metrics`, `get_task_metrics` |

The board analytics tools read only the board, so they stay kernel for now; issue #948 classifies the packages they live in.

### 3.3 Leaving with Track C

These five are software building and move to `painted-porch/marcus-software` with the extraction (epic #924).

| Tool | Why it is software building |
| --- | --- |
| `add_feature` | adds a feature to a project through the software decomposer |
| `get_code_metrics` | reads commits and pull requests from a code repository |
| `get_repository_metrics` | the same, for a whole repository |
| `get_code_review_metrics` | the same, for code review |
| `get_code_quality_metrics` | the same, for code quality |

## 4. The seed spec

A seed spec is one entry of `options.tasks` on `create_project` or `create_tasks`.
The kernel puts the card on the board exactly as written.
The validator is `validate_seed_specs` in `src/integrations/seeded_tasks.py`.

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes | the card's name, unique in the call |
| `description` | string | no | what the card asks for |
| `id` | string over `[A-Za-z0-9_-]` | no | the card's final id; unique in the call and not already on the board |
| `task_type` | non-empty string | no | which kernel rules apply to the card (sections 5 and 6) |
| `inputs` | object | no | what the board hands the worker that claims the card |
| `depends_on` | list of strings | no | other cards, by name or id, in the call or already on the board |
| `labels` | list of strings | no | free labels; `case:<id>` groups cards for `export_case` |
| `completion_criteria` | list of strings | no | what done means, in words |
| `estimated_hours` | positive number | no | default `1.0` |
| `priority` | `low`, `medium`, `high` or `urgent` | no | default `medium` |

Any other key is refused.
A dependency is resolved against names in the same call first, then ids in the same call, then the board.
A cycle is refused, naming its path.

Example, from a citation case:

```json
{"id": "tow-0001-find", "name": "Find the source of tow-0001", "task_type": "find",
 "inputs": {"case_id": "tow-0001", "query": "..."}, "labels": ["case:tow-0001"]}
```

## 5. The task-type declaration

A project declares the card types it uses in `create_project(options.task_type_registrations)`, keyed by type name (ADR-0013 K2).
It is validated before anything is written, by `validate_task_type_registrations` in `src/core/project_registry.py`, and stored on the project's record.

| Key | Type | Meaning |
| --- | --- | --- |
| `evidence_schema` | object | `{"type": "object", "required": [...]}`; a completed card of this type must report evidence with every field in `required` |
| `verifies_task_type` | string | this type checks cards of the named type; it turns on offer-time eligibility (section 6) |
| `checks_injectable` | boolean | whether a verification extension may inject checks into this type (ADR-0013 K4) |
| `injected_checks` | list of `{name, description}` | the injected checks; allowed only when `checks_injectable` is `true` |
| `requires_source_tree` | boolean | `false` says the type's work produces no source files (ADR-0013 K3) |

The citation verifier's declaration is the worked example, `TASK_TYPE_REGISTRATIONS` in `extensions/citation_verifier/citation_verifier/runner.py`:

```json
{"find":   {"evidence_schema": {"type": "object", "required": ["headline", "publisher", "date", "url"]}},
 "verify": {"evidence_schema": {"type": "object", "required": ["verdict", "checks"]}, "verifies_task_type": "find"}}
```

**Declare every type you use.**
Today a project with no declaration falls back to three built-in names, `find`, `verify` and `synthesize`, hard-coded in the kernel.
That fallback is not part of the door, and Track A3 (#941) removes it.

## 6. The rules the kernel enforces on cards

These are the guarantees an extension builds on.
Each is a mechanism in the kernel; the policy it applies comes from the project's declaration.

**The evidence gate checks shape, never meaning.**
At close, a card of a declared type must report every field its `evidence_schema` requires, or the close is refused with the missing fields named.
The kernel never reads what a field says, so a verify card whose verdict is "rejected" closes exactly as a passing one does.
Anything that should happen only on a passing verdict is the extension's own action.

**Handoff.**
When a card closes with evidence, its evidence and its own inputs, minus the `raw` field, are copied into the `inputs` of every card that depends on it.
A dependent's seeded inputs are never overwritten.
When a card is sent back and closes again, only the keys it contributed last time are replaced (ADR-0013 K7).
The handoff is the only way upstream results reach a downstream worker.

**Offer-time eligibility.**
A card whose type verifies another type is never offered to the agent that closed the card it checks, nor to any agent registered with that agent's vendor (ADR-0013 K12).
The refused agent sees the ordinary no-task reply, so the rule cannot be probed.
An approval card is offered only to an agent registered with `principal: "human"`, and every other card only to `principal: "agent"`.

**Dependencies.**
A card is offered only when every card it depends on is done.
A declined or blocked dependency keeps it unclaimable, except for a best-effort integration card.

**Leases and fencing.**
A claim is a lease that expires when the holder goes silent; the card is then offered again with a higher `lease_epoch`.
A completion carrying an epoch older than the card's current one is refused as stale (ADR-0012 D11).
A report carrying no epoch cannot be fenced this way and falls back to the check that the reporter holds the card, so always send the epoch you were given.

**Every write is stamped from the registration.**
The project of every decision, opinion, artifact, cost row and conversation line comes from the writing agent's `register_agent`, never from the server's active project (ADR-0013 K6).

## 7. Cost on task completion

A worker that calls a model with its own key is the only party that knows what its card cost.
It reports those calls on the progress report it already sends, `report_task_progress(cost=[...])`, and the kernel records them in the cost ledger at once, so a dashboard shows spend while the run is still going (issue #742).

Each entry of `cost` is one model call.

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `seq` | string or integer | yes | the call's id, stable if the same call is sent again |
| `at` | ISO-8601 string | yes | when the call was made |
| `provider` | string | yes | for example `anthropic` |
| `model` | string | yes | for example `claude-haiku-4-5` |
| `input_tokens`, `output_tokens` | non-negative integer | no | default 0 |
| `cache_read_tokens`, `cache_creation_tokens` | non-negative integer | no | default 0 |
| `reasoning_tokens` | non-negative integer | no | when the provider counts them separately |
| `usd` | non-negative number | no | the worker's own price for the call |
| `priced_by` | string | no | whose price table produced `usd` |
| `api` | string | no | the call's endpoint label |
| `basis` | `attested` or `vendor_usage_block` | no | default `attested`; `vendor_usage_block` says the numbers are the provider's usage block, relayed untouched |
| `reported_by` | `agent` or `harness` | no | default `agent`; `harness` says a process other than the model relayed the numbers |

**What the kernel stamps.**
The agent is the caller, the card is the report's `task_id`, the project is the agent's registration, the run is the project's open run (or `"unassigned"` when none is open), and the attribution is `held`.
A record that tries to set any of these is refused.

**The receipt.**
The reply's `cost` key is `{"recorded": true, "run_id", "batch_id", "accepted", "already_present", "rejected"}`, where each `rejected` entry is `{"index", "reason"}` and `index` is the record's position in the list you sent.
A malformed record is refused by itself; the rest still land.

**When nothing is recorded.**
The receipt is `{"recorded": false, "reason"}` when the board refused the report, when the agent is not registered to a project, or when the server has no cost store.
A refused report records nothing, the same rule the opinion marker follows.
Send the same records again with your next report: records are deduplicated on run, agent and `seq`, so a resend is never counted twice and appears as `already_present`.

**What the numbers mean.**
Every reported row is labelled testimony: `basis` and `reported_by` travel with it into `get_cost_summary` and the `costs` of `export_case`.
Marcus prices each row itself, from its token counts and Marcus's price table; your `usd` is stored as `reported_usd` beside it and never overrides it.

Worked example, from the live check for this document: a finder reported one call (3,012 input and 410 output tokens, its own price $0.0382) on an in-progress report, then sent that call again with a second one (100 and 20 tokens) on its completion.
The ledger held one row after the first report and two after the second, the receipt said `accepted: 1, already_present: 1`, and `get_cost_summary` priced the two rows at $0.005262 with Marcus's Haiku 4.5 prices.

## 8. What a reader may read from a kernel's data directory

A reader such as Cato does not call the kernel; it reads the files one kernel instance writes.
One kernel instance writes everything under one directory, its **data directory**, chosen by `MARCUS_DATA_DIR` (issue #792, resolved in `src/core/paths.py`).

**Point the reader at the data directory itself.**
A reader configured with a path must read that path, never its parent with `data` appended; a data directory need not be named `data` (Cato issue #54).

| Path, relative to the data directory | What it holds | Format |
| --- | --- | --- |
| `kanban.db` | the board: cards, dependencies, labels, comments | SQLite; tables `tasks`, `task_dependencies`, `task_labels`, `comments`, `projects` |
| `marcus.db` | the kernel's records: `task_metadata`, `events`, `project_config`, `task_outcomes`, `decisions`, `artifacts` | SQLite; one table `persistence(collection, key, data, stored_at)` with JSON in `data` |
| `marcus_state/projects.json` | the project registry: registry id, name, provider, provider config | JSON object keyed by registry id, plus `active_project` |
| `project_history/<project_id>/decisions.json`, `artifacts.json` | each project's decisions and artifacts | JSON |
| `costs.db` | the cost ledger, when `MARCUS_DATA_DIR` is set | SQLite; tables `token_events`, `runs`, `model_prices`, `project_names`, `task_names`, `project_budgets`; views `v_event_cost`, `v_event_cost_inclusive` |

**Where the cost ledger is.**
With `MARCUS_DATA_DIR` set, each instance writes its own `costs.db` inside its data directory.
With it unset, every instance on the machine shares `~/.marcus/costs.db`.
A reader watching several instances therefore reads one `costs.db` per configured data directory, and keeps each row tagged with the instance it came from (Cato issue #52).

**Read only.**
A reader opens these files read-only and never writes them.
Every other file under the data directory (`assignments/`, `audit_logs/`, `backups/`, `cache/`, `server.pid`) is kernel-internal and outside the door.
Column and key names inside these files are part of the door only where this table names them; adding a column is a MINOR change and removing one is a MAJOR change.

## 9. Known gaps in version 1.0.0

Writing this contract against a live server found the following.
Each is a real limit of the door today, and each has a follow-up.

1. **`create_tasks` and `get_project_status` act on the server's active project, not the caller's.**
   Neither takes an `agent_id` or a `project_id`, so on a server holding several projects, a card added without `project_name` lands on whichever project was created or switched to last.
   This is the one place the door still breaks ADR-0013 K6.
2. **The name `create_project` replies with does not find the project.**
   A project created as `contract-chain` is registered as `contract-chain - Main Board`, and `create_tasks(project_name="contract-chain")` answers "did you mean" instead of adding the card.
   Use the registered name, or omit it on a single-project server.
3. **`create_project` requires `options.project_root` even for a board with no code.**
   It is a software-building requirement and leaves with Track C (step 8a); until then, pass any absolute directory.
4. **`request_next_task`'s `instructions` carry software-building text** (a test-driven workflow, a commit duty).
   It leaves with Track C (steps 8b.2 and 8b.3).
5. **The built-in task-type fallback** (section 5) is removed by Track A3, #941.

## 10. How to verify this document

1. `pytest tests/unit/marcus_mcp/test_kernel_contract_surface.py -q`: every served tool is in one tier, stdio and HTTP agree on every shared tool, and this document's version and tool headings match `src/marcus_mcp/kernel_contract.py`.
2. `python ~/marcus-experiments/kernel-contract-live-check/live_check.py <checkout> <out-dir>`: a real server with the model off calls every contract tool across four small projects, reads the cost ledger while the run is open, and compares stdio with HTTP; it must print `verdict: PASS` and record zero model-provider calls.
3. Track A2 (#940) adds the contract test suite that fails when anything here changes without a version bump.

## Related

- Issue #939 (this document), epic #938, Track A2 #940, Track A3 #941, Track A4 #942.
- Issue #742 (cost on task completion), Cato issues #52 and #54 (readers of a data directory), #889 (schema drift guard), PR #919 (the MCP-only write path), PR #867 (the usage-ingestion API).
- `docs/architecture/extension-contract.md`, `docs/architecture/building-an-extension.md`, `docs/architecture/adr/0013-kernel-and-extensions.md`.
- `src/marcus_mcp/kernel_contract.py`, `tests/unit/marcus_mcp/test_kernel_contract_surface.py`.
