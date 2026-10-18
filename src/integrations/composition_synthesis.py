"""
Composition task synthesis for multi-domain contract-first projects.

Issue #463 — Marcus's task decomposer can produce a multi-domain
project where every domain ships correctly but no task owns wiring
them into the application's composition root.  v38 audit case
(snake-game-v38, 2026-04-29): three domain implementations
(engine, bus, renderer) shipped clean but ``App.tsx`` returned
``null``.  Unit tests passed.  The bundle built.  But the rendered
DOM was empty — the integration verification catch-all rescued it
at the cost of ~15 min cleanup absorbed by an agent on top of
their other work.

Fix (Variant V3 per Kaia review checkpoint #1): synthesize a
dedicated composition task when ``len(impl_tasks) >= 2``.  Marcus
says WHAT (a wiring task with explicit deliverables — log_decision
+ log_artifact); the agent picks HOW (which file is the entry
point, which wiring strategy).  Multiple framework examples are
included in the description so Marcus is not picking a single
file.

Bright-line check: two foundation agents handed this task can
produce legitimately different wirings — different file choices,
different mounting strategies.  Coordination, not control.

Layering with ``enhance_project_with_integration``: composition
is **narrow scope** (entry-point wiring only), assigned **early**
when impls complete.  Integration verification is the **broad
catch-all** (orphan scan, missing components, contract
verification), assigned **late**.  Intentional layered safety,
not redundant — composition makes wiring an explicit deliverable
with explicit ownership; IV catches anything composition missed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from src.core.models import Priority, Task, TaskStatus

__all__ = ["build_composition_task"]


def _has_existing_composition_task(impl_tasks: List[Task]) -> bool:
    """Return True if a composition task is already present in the list.

    Idempotency guard: skip synthesis if the caller already has a
    composition task in the input list.  Two detection routes:

    1. ``"composition"`` label present (canonical Marcus tag)
    2. ``source_type == "composition_synthesis"`` (canonical source
       marker — survives kanban round-trips that strip labels)
    """
    for task in impl_tasks:
        labels = task.labels or []
        if "composition" in labels:
            return True
        if getattr(task, "source_type", None) == "composition_synthesis":
            return True
    return False


def _build_composition_description(project_name: str) -> str:
    """Render the composition task description (Variant V3).

    The description must:

    - List multiple framework entry-point examples (so Marcus is not
      picking one — agents legitimately use different framework
      conventions)
    - Tell the agent to **discover** the entry point from the scaffold
    - Require ``log_decision`` titled ``"Entry point wired"`` so
      downstream tools can verify wiring happened
    - Require ``log_artifact`` for the wired file (file-level surface
      for the structured-decision metadata)
    - Mandate a build-verification gate (bug #649 root cause 2): the
      composed product must actually build before the task is
      reported complete.  Marcus says WHAT must be true (build exits
      0, dev server probe returns 2xx); the agent picks HOW to
      satisfy.  This is Invariant #2 v2 applied at the composition
      task — verification belongs to Marcus, agents own only the
      implementation HOW.

    Bright-line guard: the description must NOT name a specific entry
    point file (e.g., ``"the entry point is App.tsx"``).  Multiple
    examples are listed; the agent picks which applies to their
    scaffold.  Similarly the build-verification step lists multiple
    stack-keyed commands so Marcus does not prescribe one.

    Issue #677 (self-verify) — a build that exits 0 and a server that
    returns 200 do NOT prove the product works (it can render a blank
    page or produce empty output).  A final step tells the agent to
    actually RUN the composed product with whatever tools it needs and
    confirm it behaves, fixing the wiring if it does not.  This is
    self-verification by the agent (a full-capability harness), not
    proof authored for Marcus to judge — consistent with the integration
    task's self-verify prompt.  Marcus runs NO independent build/behavior
    check on the composition task (issue #677: that floor was tech-specific
    and gridlock-prone); the completion is accepted on the agent's
    self-report and stamped as such.

    Parameters
    ----------
    project_name : str
        Project name, surfaced in the description for agent context.
    """
    selfverify_step = (
        "\n7. RUN IT — don't just build it.  A build that exits 0 and a "
        "server that returns 200 do NOT prove the product works: it can "
        "render a blank page or produce empty output.  After wiring, "
        "actually RUN the composed product with whatever tools you need "
        "(load it in a browser, drive the CLI, call the API) and watch it "
        "behave.  If it shows a blank screen, an empty result, or a "
        "console/runtime error, FIX the wiring and run it again before "
        "reporting complete."
    )
    return (
        f"Wire {project_name}'s implementation domains into a working "
        f"composition root.  The entry point is the file that boots "
        f"the project — discover it from the scaffold (e.g. App.tsx "
        f"for Vite/CRA, main.py for Python, index.ts for Node — "
        f"whichever your scaffold uses).\n\n"
        f"Required deliverables:\n"
        f"1. Wire all domain implementations into the entry point so "
        f"the composed product is functionally end-to-end.\n"
        f"   WIRE, DO NOT REIMPLEMENT (issue #691): each domain already "
        f"built and exported what it is responsible for. If a domain "
        f"exports something that performs its job — a renderer, a "
        f"handler, a service, a validator — you MUST import and CALL that "
        f"export. Do NOT rewrite that behavior yourself in the entry "
        f"point. Observed failure (test109): the composition agent "
        f"imported the presentation domain's GameBoardRenderer, then "
        f"ignored it and hand-wrote its own canvas drawing — leaving the "
        f"domain agent's renderer as dead code and shipping a different "
        f"implementation than the one that was built and reviewed. The "
        f"only code you author is glue: imports, instantiation, wiring, "
        f"and the run loop. If a domain's export seems unusable for "
        f"wiring, report a blocker — do not silently replace it.\n"
        f"2. Call log_decision titled 'Entry point wired' with the "
        f"actual file path you chose, the domains you composed, and "
        f"the wiring approach (DI container, direct mounting, etc.).\n"
        f"3. Call log_artifact for the entry-point file you modified "
        f"so downstream verification can locate it.\n"
        f"4. MANDATORY BUILD VERIFICATION (bug #649 root cause 2): "
        f"Before reporting this task at 100%, run a build command "
        f"appropriate to the project's stated tech stack and confirm "
        f"it exits 0.  Examples (pick the one that matches your "
        f"scaffold's package manifest — do not run all of them):\n"
        f"   - JavaScript/TypeScript with Vite/Webpack: ``npm run "
        f"build``\n"
        f"   - Python project with build module: ``python -m build``\n"
        f"   - Rust: ``cargo build``\n"
        f"   - Other stacks: the equivalent command for the build "
        f"tool your scaffold actually uses.\n"
        f"   If the build fails with unresolved imports, missing "
        f"modules, or path-alias mismatches (the verify-snake-3 "
        f"failure mode), FIX the imports before reporting done.  "
        f"Reconcile competing config files (e.g., do not leave both "
        f"vite.config.js and vite.config.ts in the project when only "
        f"one is in use).\n"
        f"5. MANDATORY DEV-SERVER PROBE when applicable: For "
        f"projects with a runnable dev server (vite, webpack-dev-"
        f"server, python http.server, etc.), boot the server in the "
        f"background, ``curl -f`` the root URL to confirm 2xx, then "
        f"kill the server.  Composition is not done if the running "
        f"app fails to boot or returns 5xx.\n"
        f"6. DO NOT MARK THIS TASK COMPLETE on a broken build.  If "
        f"the build or dev-server probe cannot pass after a "
        f"reasonable fix attempt, report a blocker instead — the "
        f"smoke gate downstream will reject the project anyway, and "
        f"reporting complete-with-broken-build is the failure mode "
        f"bug #649 was filed to prevent." + selfverify_step
    )


def build_composition_task(
    *,
    project_name: str,
    impl_tasks: List[Task],
    structural_category: str = "unknown",
) -> Optional[Task]:
    """Synthesize a composition task when ``len(impl_tasks) >= 2``.

    Issue #463 — multi-domain projects need an explicit wiring task
    or domains ship correctly while ``App.tsx`` returns ``null``.

    The synthesized task carries hard dependencies on every input
    impl task; the agent that picks it up must wait until all impls
    complete before wiring.  Foundation deps are NOT direct — they
    flow transitively via impl deps already wired at
    ``nlp_tools.py:332``.

    Parameters
    ----------
    project_name : str
        Project name, used in the task name and description so the
        agent has context.
    impl_tasks : List[Task]
        Contract-first implementation tasks the caller has already
        produced.  Caller filters to impl tasks (excluding foundation,
        design ghosts, etc.) before passing.  This list is **not
        mutated** — the helper is a pure function.
    structural_category : str
        Marcus's setup-time classification (issue #677).  Selects the
        behavior-evidence contract appended to the composition task
        description so the agent knows what evidence to capture and
        submit.  Defaults to ``"unknown"`` (no contract — legacy
        build/probe wording only).

    Returns
    -------
    Optional[Task]
        A new composition task with hard deps on every input impl
        task, or ``None`` when:

        - ``len(impl_tasks) < 2`` (no need for explicit composition;
          single-impl projects compose naturally)
        - A composition task is already present in ``impl_tasks``
          (idempotency guard)

    Notes
    -----
    Bright-line: Marcus says WHAT must be produced (a coordination
    task with explicit deliverables); agent picks HOW (which file,
    which wiring strategy).  Two agents handed this task can produce
    legitimately different implementations.  Coordination, not
    control.
    """
    if len(impl_tasks) < 2:
        return None
    if _has_existing_composition_task(impl_tasks):
        return None

    now = datetime.now(timezone.utc)
    return Task(
        id=f"composition_{uuid4().hex[:12]}",
        name=f"Compose {project_name} entry point",
        description=_build_composition_description(project_name),
        status=TaskStatus.TODO,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=1.5,
        # Hard deps on every impl task — composition must wait until
        # all implementations complete (nothing to wire otherwise).
        dependencies=[t.id for t in impl_tasks],
        labels=["composition", "marcus_synthesized"],
        source_type="composition_synthesis",
        # Issue #677: stash the structural category so the product smoke
        # gate can judge the submitted behavior evidence against the
        # per-type bar (web=rendered DOM, pipeline=output, …).
        source_context={"structural_category": structural_category},
        # responsibility surfaces in build_tiered_instructions as the
        # CONTRACT RESPONSIBILITY layer so the agent prompt frames
        # this as a coordination boundary, not a prescriptive spec.
        responsibility="Wires the application entry point",
    )
