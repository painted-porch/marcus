"""
Integration Verification & Remediation Task Generation for Marcus.

Adds an integration task to projects that runs after implementation
completes. The integration agent inspects the project, figures out
how to build and start it, verifies it works end-to-end, and — critically —
FIXES any issues it finds before re-verifying.

This is not a report-only task. The integration agent is the last line of
defense: it glues components together, fills gaps left by task decomposition,
and ensures the product actually works as a whole.

See: https://github.com/lwgray/marcus/issues/296
Related: #271, #267, #257
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.core.models import Priority, Task, TaskStatus
from src.integrations.behavior_evidence import behavior_evidence_contract
from src.integrations.nlp_task_utils import TaskType

if TYPE_CHECKING:
    # TYPE_CHECKING-only to keep this module decoupled from the
    # outcome-extractor module at runtime.  ``UserOutcome`` is a plain
    # dataclass with no heavy imports, but we only ever annotate with
    # it (never instantiate here), so deferring the import keeps the
    # integration layer free of upward dependencies on
    # ``src.ai.advanced.prd``.
    from src.ai.advanced.prd.outcome_extractor import UserOutcome

logger = logging.getLogger(__name__)


class IntegrationTaskGenerator:
    """Generate integration verification tasks for projects.

    Creates a task that runs after all implementation and testing tasks
    complete. The assigned agent inspects the project structure, figures
    out how to build/start it (regardless of language or framework),
    verifies whether the product works, and fixes any issues found.

    Parameters
    ----------
    None

    Examples
    --------
    >>> generator = IntegrationTaskGenerator()
    >>> task = generator.create_integration_task(existing_tasks, "Dashboard")
    >>> if task:
    ...     tasks.append(task)
    """

    @staticmethod
    def create_integration_task(
        existing_tasks: List[Task],
        project_name: str = "Project",
        contract_file: Optional[str] = None,
        outcomes: Optional[List["UserOutcome"]] = None,
        structural_category: str = "unknown",
    ) -> Optional[Task]:
        """
        Create an integration verification task.

        Depends on all non-documentation, non-integration tasks.
        Returns None if no implementation tasks exist.

        Parameters
        ----------
        existing_tasks : List[Task]
            List of all project tasks created so far.
        project_name : str
            Name of the project.
        contract_file : Optional[str]
            Path to the shared contract artifact when contract-first
            decomposition is active (GH-320 PR 2). When set, the
            integration task description explicitly names this file
            and instructs the integration agent to treat it as
            authoritative — fix implementations that diverge, do NOT
            modify the contract. Without this instruction, an
            integration agent could silently "fix" a mismatch by
            editing the contract, breaking the invariant that made
            contract-first decomposition work in the first place.
        outcomes : Optional[List[UserOutcome]]
            Issue #523 Slice B: in-scope user outcomes the integration
            task must verify.  When provided:

            * Each outcome's ``id`` is stored on
              ``task.source_context["in_scope_outcome_ids"]`` so the
              smoke gate's coverage check can require a matching
              ``VerificationSpec`` per outcome at completion time.
            * The task description grows a "Verifications required"
              section listing each outcome's ``id``,
              ``success_signal``, and ``action`` so the agent knows
              what to declare in the ``verifications`` field of
              ``report_task_progress``.

            Out-of-scope outcomes are filtered out — they exist in
            the extractor output for audit only and must not gate
            completion.

            When ``None`` or empty after filtering, the integration
            task behaves like the legacy single-``start_command``
            contract (no coverage requirement, no description section).

        Returns
        -------
        Optional[Task]
            Integration verification task, or None if no
            implementation tasks exist.
        """
        # Check for implementation tasks using the classifier
        # (not hardcoded labels, which AI-generated tasks may not have)
        from src.integrations.enhanced_task_classifier import (
            EnhancedTaskClassifier,
        )

        classifier = EnhancedTaskClassifier()
        implementation_tasks = classifier.filter_by_type(
            existing_tasks, TaskType.IMPLEMENTATION
        )

        if not implementation_tasks:
            logger.info(
                "No implementation tasks found, " "skipping integration verification"
            )
            return None

        # Depend on ALL non-documentation, non-integration tasks
        dependencies = [
            t.id
            for t in existing_tasks
            if "documentation" not in t.labels and "type:integration" not in t.labels
        ]

        logger.info(
            f"Integration verification task will depend on "
            f"{len(dependencies)} tasks"
        )

        # Filter to in-scope outcomes — out-of-scope outcomes are
        # retained by the extractor for audit purposes only and must
        # not gate completion or grow the description.
        in_scope_outcomes: List["UserOutcome"] = (
            [o for o in outcomes if o.scope == "in_scope"] if outcomes else []
        )

        description = IntegrationTaskGenerator._generate_integration_description(
            project_name,
            contract_file=contract_file,
            in_scope_outcomes=in_scope_outcomes,
            structural_category=structural_category,
        )

        acceptance_criteria = [
            "Tests run with full terminal output captured",
            "Project dependencies installed successfully",
            "Project builds without errors",
            "Application actually starts (startup output captured)",
            "Key endpoints hit with curl, full response captured",
            "Missing components detected AND fixed",
            "Orphan scan complete: every source file either reachable "
            "from entry point OR explicitly documented as intentionally standalone",
            "Cross-agent interface contracts verified "
            "(identifiers, data shapes, config values "
            "match across boundaries)",
            "All results include raw command output as evidence",
            "integration_verification.json artifact logged",
            "integration_remediation.json artifact logged if fixes applied",
            "Re-verification passes after fixes",
        ]

        # Slice B: stash the in-scope outcome IDs on source_context so
        # the smoke gate's coverage check at completion time can verify
        # the agent declared at least one VerificationSpec per outcome.
        # Empty list when no outcomes — distinguishes "outcomes wired
        # but none in scope" from "wiring not present at all," which
        # matters for the gate's decision to apply the coverage rule.
        # Issue #677: always stash the structural category so the product
        # smoke gate can judge submitted behavior evidence against the
        # per-type bar (web=rendered DOM, pipeline=output, …), even when
        # no UserOutcomes were extracted.
        source_context: Dict[str, Any] = {
            "structural_category": structural_category,
        }
        if outcomes is not None:
            source_context["in_scope_outcome_ids"] = [o.id for o in in_scope_outcomes]

        task = Task(
            id=f"integration_verify_{uuid.uuid4().hex[:8]}",
            name=(f"Integration verification for {project_name}"),
            description=description,
            status=TaskStatus.TODO,
            priority=Priority.URGENT,
            labels=["integration", "verification", "type:integration"],
            dependencies=dependencies,
            estimated_hours=1.0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            assigned_to=None,
            due_date=None,
            acceptance_criteria=acceptance_criteria,
            source_context=source_context,
        )

        return task

    @staticmethod
    def should_add_integration_task(
        project_description: str,
    ) -> bool:
        """
        Determine if an integration task should be added.

        Skips for prototypes, demos, and POCs. Explicitly does NOT
        skip for the word "experiment" — Marcus experiments need
        integration verification so we can measure whether multi-
        agent coordination changes actually catch integration bugs
        at the merged-product level. See GH-320 PR 1 for context.

        The ``test`` POC marker is distinguished from testing
        *infrastructure* mentions (``test suite``, ``unit tests``,
        ``test-driven``, ``testing framework``, etc.) by scrubbing
        compound phrases before the word-boundary search so real
        projects that happen to mention their test strategy do not
        get their integration task suppressed.

        Parameters
        ----------
        project_description : str
            Natural language project description.

        Returns
        -------
        bool
            True if integration verification should be added.
        """
        description_lower = project_description.lower()

        # Unambiguous POC markers — matched with word boundaries so
        # ``contest``/``democracy``/etc. don't trip the rule, and with
        # optional plural ``s`` so ``pocs``/``demos``/``proof of
        # concepts`` still skip as expected (Codex P1 on PR #333).
        poc_patterns = [
            r"\bpocs?\b",
            r"\bproof of concepts?\b",
            r"\bdemos?\b",
        ]
        for pattern in poc_patterns:
            if re.search(pattern, description_lower):
                return False

        # ``test`` is ambiguous: "Quick test of the new encoder" is
        # POC intent, but "Build an app with a test suite" is not.
        # Scrub compound phrases that describe testing infrastructure
        # before running the word-boundary check.
        test_compound_patterns = [
            # "test suite", "test cases", "test coverage", etc.
            r"\btests?\s+"
            r"(suites?|cases?|coverage|harness|plan|plans|"
            r"frameworks?|beds?|runners?|infrastructure|infra|"
            r"strategy|strategies|approach|approaches)\b",
            # "unit tests", "integration test", "e2e tests", etc.
            r"\b(unit|integration|smoke|regression|acceptance|"
            r"e2e|end[- ]to[- ]end|functional|performance|load|"
            r"stress|api|contract|property|mutation|snapshot)"
            r"\s+tests?\b",
            # "test-driven", "tests-driven"
            r"\btests?[-\s]driven\b",
            # "testing framework/library/etc." — "testing" with a
            # compound noun is infrastructure, not POC intent.
            r"\btesting\s+"
            r"(framework|frameworks|library|libraries|"
            r"infrastructure|suite|suites|strategy|strategies|"
            r"approach|approaches|harness)\b",
        ]
        scrubbed = description_lower
        for pattern in test_compound_patterns:
            scrubbed = re.sub(pattern, " ", scrubbed)

        if re.search(r"\btest\b", scrubbed):
            return False

        return True

    @staticmethod
    def _generate_integration_description(
        project_name: str,
        contract_file: Optional[str] = None,
        in_scope_outcomes: Optional[List["UserOutcome"]] = None,
        structural_category: str = "unknown",
    ) -> str:
        """
        Generate the integration task description.

        The description instructs the agent to inspect the project
        and figure out how to verify it works, regardless of the
        language, framework, or project type.

        Parameters
        ----------
        project_name : str
            Name of the project.
        contract_file : Optional[str]
            Path to the shared contract artifact when contract-first
            decomposition is active. When set, a contract-authority
            preamble is prepended to the description so the integration
            agent treats the contract as read-only.
        in_scope_outcomes : Optional[List[UserOutcome]]
            Issue #523 Slice B: in-scope user outcomes the agent must
            declare verifications for at completion.  When non-empty,
            a "Verifications required (#523)" section is appended
            naming each outcome's ``id``, ``action``, and
            ``success_signal`` along with a worked example of the
            ``verifications`` payload to pass to
            ``report_task_progress``.

        Returns
        -------
        str
            Detailed task description with verification steps.
        """
        preamble = ""
        if contract_file:
            preamble = (
                "**CONTRACT-FIRST PROJECT**: This project was decomposed "
                "using contract-first decomposition (GH-320). The shared "
                f"contract lives at:\n\n"
                f"    {contract_file}\n\n"
                "**The contract is AUTHORITATIVE**. Agents built their "
                "implementations against it. If any implementation "
                "diverges from the contract during your verification, "
                "FIX THE IMPLEMENTATION — do NOT modify the contract "
                "file. The contract was the agreed-upon interface "
                "boundary; silently editing it to match a broken "
                "implementation defeats the purpose of contract-first "
                "decomposition and will cause future regressions.\n\n"
                "`Read` the contract file first so you know the "
                "authoritative interface shapes, identifiers, and "
                "configuration values, and use it as your reference when "
                "you fix mismatches between the pieces.\n\n---\n\n"
            )
        # Issue #677 (self-verify, skeptic framing): the integration agent is
        # a full-capability Claude Code harness — it can run the assembled
        # product with whatever tools it needs (start a server, load a
        # browser, drive a CLI), observe whether it actually works, and FIX
        # what is broken in the same loop.  Two live e2e runs showed a
        # *closer* incentive defeats a neutral "verify it works" prompt: the
        # agent grabbed the cheapest success-shaped artifact (test97: the
        # static index.html shell; test98: a green vitest/pytest run) and
        # reported done while the running game was blank / stuck idle.  The
        # fix is to frame the agent as a SKEPTIC — a MAS built this and always
        # leaves integration mistakes; the job is to find and fix them — and
        # to close the unit-test loophole explicitly (tests pass in isolation
        # while the assembled product is dead).  The in-scope outcomes are the
        # spine; HOW to run/observe stays the agent's (Invariant #2).  Marcus's
        # only hard floor is the build (mechanically detected, run by Marcus,
        # no browser needed); proving the outcomes behave is the agent's
        # self-verification.
        if in_scope_outcomes:
            bullets = "\n".join(
                f"- {o.action} — {o.success_signal}" for o in in_scope_outcomes
            )
            outcomes_block = (
                '\n\n**"Done" means a user can actually do each of these:'
                f"**\n\n{bullets}\n"
            )
        else:
            outcomes_block = (
                '\n\n**"Done" means the product actually does what the '
                "project description asks for** when a user runs it.\n"
            )

        return (
            preamble + "A multi-agent system built this project. Each piece was built "
            "by a different agent working in isolation, and they ALWAYS leave "
            "integration mistakes — components that don't connect, a UI that "
            "renders but never starts, input wired to nothing. You are not "
            "here to confirm it works. You are here to FIND what they got "
            "wrong and FIX it, until the product actually starts, runs, and "
            "performs every outcome below.\n\n"
            "The pieces were built separately. Wire them together at the "
            "entry point, fill any gaps, and fix the mismatches between them "
            "so the whole thing works as one product."
            + outcomes_block
            + "\nVerify by actually RUNNING the product the way a user "
            "would — use whatever tools you need (start its server, load it "
            "in a real browser, drive its CLI, call its API; install a tool "
            "if you have to). Then PERFORM each outcome yourself and watch "
            "the real result happen: do the user action and confirm the "
            "actual behavior occurs (e.g. press the keys and confirm the "
            "thing actually moves, watch the output appear, watch the value "
            "change).\n\n"
            "A passing unit-test suite is NOT proof and does not count. "
            "Unit tests check pieces in isolation with fake inputs — they go "
            "green even when the assembled product is dead on arrival. If you "
            "have not observed the real behavior happen with your own eyes in "
            "the running product, the outcome is NOT done: find why, fix it, "
            "and run it again. Keep going until every outcome genuinely "
            "works.\n\n"
            "You are a full-capability agent: write code, install tools, do "
            "whatever it takes. Marcus does NOT run any build, test, or check "
            "for you — there is no safety net behind you. YOU must build it, "
            "run it, and confirm every outcome above actually happens before "
            "you mark this complete. If you have not watched it work, it is "
            "not done.\n\n"
            "**Wrong vs mis-wired:** glue fixes (imports, type alignment, "
            "boundary adapters) are yours. For wrong LOGIC in another "
            "agent's completed task, call `request_task_redo(agent_id, "
            "task_id, reason)` instead of rewriting their lane, then "
            "re-verify; after 3 redos, fix in place."
        )

    @staticmethod
    def _render_behavior_evidence_section(structural_category: str) -> str:
        """Render the per-app-type behavior-evidence block (issue #677).

        The integration agent verifies the product *builds and starts*;
        this section additionally requires it to RUN the assembled
        product and capture *behavior evidence* (a rendered DOM, produced
        output, stdout, …) proving it actually works.  Marcus's product
        smoke gate judges the submitted evidence against the per-type bar
        — a build that exits 0 and a server that returns 200 do not pass.

        Marcus authors WHAT evidence proves the outcome (tool-agnostic);
        the agent picks HOW to capture it (Invariant #2, and the
        ``VerificationSpec`` "coordination, not a tooling registry"
        principle).  Fuzzy types (``other``, ``automation``, unknown)
        have no behavior contract and get an empty section — they fall
        back to the legacy build/serve verification only.

        Parameters
        ----------
        structural_category : str
            Marcus's setup-time classification (e.g. ``"web app"``).

        Returns
        -------
        str
            Markdown-formatted section, or ``""`` when this app type has
            no behavior contract.
        """
        contract = behavior_evidence_contract(structural_category)
        if not contract:
            return ""
        return f"""

---

## Behavior evidence required (#677)

Building and starting the product is NOT enough — a build that exits 0
and a server that returns 200 can still ship a blank page or empty
output (the snake-pr667-5 / #463 / #636 failure mode).  After you have
the assembled product running, capture evidence that it actually
**behaves**, and submit it in the ``evidence`` field of
``report_task_progress``.  Marcus judges the evidence you submit, not a
command's exit code.

{contract}

**CRITICAL — WHERE the evidence goes.** Marcus reads ONLY the structured
``evidence`` argument. It does NOT read your ``message`` text for
verification. Pasting the rendered HTML / output into ``message`` (or
just describing it in prose) will be treated as "no evidence submitted"
and your completion will be rejected. Put the captured proof in
``evidence``, not in ``message``.

**How to submit** (alongside any ``verifications`` you declare):

```python
report_task_progress(
    task_id=task_id,
    status="completed",
    progress=100,
    message="short human summary only — NOT the proof",
    evidence={{
        # Use exactly the keys the contract above names for THIS app
        # type — paste the real captured values here, do not describe
        # them. Do not invent keys for other app types.
    }},
)
```

If the captured evidence does not meet the bar, FIX the composition
until it does before reporting complete.  Marcus's product smoke gate
will reject a completion whose evidence is empty or shows errors.
"""

    @staticmethod
    def _render_outcomes_section(
        outcomes: List["UserOutcome"],
    ) -> str:
        """Render the per-outcome "Verifications required" description block.

        Issue #523 Slice B: when in-scope user outcomes exist, the
        integration task description ends with this section so the
        agent knows exactly which outcomes Marcus's smoke gate will
        require ``VerificationSpec`` entries for at completion time.

        Each outcome is listed with its ``id`` (the ``signal_id`` the
        agent must reference), the user-facing ``action``, and the
        observable ``success_signal`` the verification command must
        prove was satisfied.  A worked ``report_task_progress`` call
        example shows the structure of the ``verifications`` payload.

        Parameters
        ----------
        outcomes
            In-scope outcomes only (caller has filtered).  Empty list
            is unexpected here — callers should skip the section.

        Returns
        -------
        str
            Markdown-formatted section appended to the description.
        """
        bullets = []
        for o in outcomes:
            bullets.append(
                f"- **`{o.id}`** — {o.action}\n"
                f"  - Success signal: {o.success_signal}"
            )
        bullets_block = "\n".join(bullets)

        first_id = outcomes[0].id
        return f"""

---

## Verifications required (#523 Slice B)

Marcus's smoke gate at completion time runs every ``VerificationSpec``
you declare in the ``verifications`` field of ``report_task_progress``,
and **rejects the completion** if any in-scope user outcome below has
no matching spec.  You must declare at least one spec per outcome.

**In-scope outcomes for this project:**

{bullets_block}

**Each spec is a shell command** whose exit code reflects whether the
outcome's ``success_signal`` is observable in the running deliverable.
Pick the tool that fits the deliverable shape — `curl` for HTTP,
`npx playwright test` for browser UI, `pytest` for CLI assertions,
etc.  Marcus does not care which tool you use; only that the command
exits 0 when the signal is satisfied.

**Worked example** (matching the first outcome above):

```python
report_task_progress(
    task_id=task_id,
    status="completed",
    progress=100,
    message="...",
    verifications=[
        {{
            "signal_id": "{first_id}",
            "command": "<the shell command you wrote>",
            "description": "<short human label>",
            # Optional, only when the command is a long-running server:
            # "readiness_probe": "curl -f http://localhost:5173",
        }},
        # ... one entry per in-scope outcome above ...
    ],
)
```

Coverage check: every ``signal_id`` listed above must appear in your
``verifications`` list, OR the smoke gate will reject your completion
with a missing-coverage blocker.  See ``report_task_progress``
documentation for the full schema.
"""


def enhance_project_with_integration(
    tasks: List[Task],
    project_description: str,
    project_name: str = "Project",
    contract_file: Optional[str] = None,
    functional_requirements: Optional[List[Dict[str, Any]]] = None,
    outcomes: Optional[List["UserOutcome"]] = None,
    structural_category: str = "unknown",
) -> List[Task]:
    """
    Add integration verification task to project if appropriate.

    Should be called BEFORE enhance_project_with_documentation()
    so that the documentation task depends on the integration task.

    Parameters
    ----------
    tasks : List[Task]
        Original project tasks.
    project_description : str
        Project description.
    project_name : str
        Name of the project.
    contract_file : Optional[str]
        Path to the shared contract artifact when contract-first
        decomposition is active (GH-320 PR 2). Forwarded to the
        integration task generator so the verification agent treats
        the contract as read-only authoritative.
    functional_requirements : Optional[List[Dict[str, Any]]]
        PRD functional requirements from ``PRDAnalysis``. When
        provided (contract-first path, GH-320 task #64), each
        requirement's ``name`` is appended to the integration task's
        ``acceptance_criteria`` so the integration agent verifies
        the user's original intent was realized — not just that the
        code compiles and tests pass. This was the gap in Experiment
        4 v2 where both agents built clean plumbing but no visible
        UI because the integration agent had no reference back to
        the user's "display weather" ask.
    outcomes : Optional[List[UserOutcome]]
        Issue #523 Slice B: extracted ``UserOutcome`` records from
        the outcome extractor.  Forwarded to
        :meth:`IntegrationTaskGenerator.create_integration_task` which
        filters to in-scope, stores ``in_scope_outcome_ids`` on
        ``Task.source_context``, and appends a "Verifications
        required" section to the description so the agent knows what
        the smoke gate's coverage check will demand at completion.

    Returns
    -------
    List[Task]
        Task list with integration task added if appropriate.
    """
    if not IntegrationTaskGenerator.should_add_integration_task(project_description):
        logger.info("Skipping integration verification for this project")
        return tasks

    task = IntegrationTaskGenerator.create_integration_task(
        tasks,
        project_name,
        contract_file=contract_file,
        outcomes=outcomes,
        structural_category=structural_category,
    )

    if task:
        # Intent preservation (GH-320 task #64): append functional
        # requirements as acceptance criteria so the integration agent
        # verifies the user's original ask, not just code correctness.
        if functional_requirements:
            for req in functional_requirements:
                req_name = req.get("name", "")
                if req_name:
                    task.acceptance_criteria.append(
                        f"User requirement verified: {req_name}"
                    )
            logger.info(
                f"Enriched integration task with "
                f"{len(functional_requirements)} functional "
                f"requirement(s) as acceptance criteria"
            )

        # Issue #680: surface enumerated gotchas to the skeptic. The
        # gotcha-enumeration pass stamped known failure modes onto the
        # implementation tasks' acceptance_criteria. The integration
        # (skeptic) agent verifies the assembled product, so it must
        # also know which failure modes to actively test for — not just
        # that the code compiles. Aggregate every distinct gotcha
        # criterion from the implementation tasks onto the integration
        # task's checklist so the reversal-is-a-no-op class of bug gets
        # explicitly exercised end-to-end.
        from src.marcus_mcp.coordinator.outcome_coverage import (
            GOTCHA_CRITERION_PREFIX,
        )

        existing = set(task.acceptance_criteria)
        gotchas_propagated = 0
        for src_task in tasks:
            for criterion in src_task.acceptance_criteria or []:
                if (
                    criterion.startswith(GOTCHA_CRITERION_PREFIX)
                    and criterion not in existing
                ):
                    task.acceptance_criteria.append(criterion)
                    existing.add(criterion)
                    gotchas_propagated += 1
        if gotchas_propagated:
            logger.info(
                "Propagated %d gotcha criterion(s) onto integration "
                "task's skeptic checklist (#680)",
                gotchas_propagated,
            )

        logger.info(f"Added integration verification task: {task.name}")
        return tasks + [task]

    return tasks
