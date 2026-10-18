"""Deterministic decomposition dump for #664 / #680 validation.

What this is: Marcus turns a plain-English project spec into a graph of
tasks for AI agents to build. Two recent changes:

* #664 — ``request_next_task`` now delivers each task's
  ``acceptance_criteria`` to the agent (it used to be dropped).
* #680 — at decomposition time, one LLM call enumerates known failure
  modes ("gotchas", e.g. "reversing the snake into itself must be
  ignored") and writes them into those same ``acceptance_criteria``.

This script runs the REAL decomposition pipeline (real LLM calls) on a
snake-game spec and prints every task's ``acceptance_criteria``,
flagging the gotcha-stamped ones. It needs no kanban board, no agents,
and no experiment runner — so it validates #680 without depending on
the #632/#634 runner-liveness fix.

Run:  python scripts/dump_gotcha_criteria.py
Needs: the same LLM provider env (e.g. ANTHROPIC_API_KEY) Marcus uses.
"""

import asyncio

from src.ai.advanced.prd.advanced_parser import (
    AdvancedPRDParser,
    ProjectConstraints,
)
from src.core.task_classification import TASK_TYPE_IMPLEMENTATION, get_task_type
from src.marcus_mcp.coordinator.outcome_coverage import GOTCHA_CRITERION_PREFIX

SNAKE_SPEC = """\
Build a browser-based Snake game.

The player controls a snake with the arrow keys. The snake moves
continuously around a grid. Eating food makes the snake grow longer and
increases the score. The game ends if the snake runs into a wall or into
its own body. Show the current score, and a game-over screen with a
restart option.
"""


async def main() -> None:
    """Decompose the snake spec and print each task's acceptance criteria."""
    parser = AdvancedPRDParser()
    constraints = ProjectConstraints(complexity_mode="standard")

    print("Decomposing snake spec (real LLM calls)...\n")
    result = await parser.parse_prd_to_tasks(SNAKE_SPEC, constraints)
    tasks = result.tasks

    total_gotchas = 0
    misplaced = []  # gotchas on non-implementation tasks (should be none)
    for task in tasks:
        criteria = list(task.acceptance_criteria or [])
        gotchas = [c for c in criteria if c.startswith(GOTCHA_CRITERION_PREFIX)]
        total_gotchas += len(gotchas)
        task_type = get_task_type(task)
        if gotchas and task_type != TASK_TYPE_IMPLEMENTATION:
            misplaced.append((task.name, task_type, len(gotchas)))

        print(f"── [{task_type}] {task.name}")
        if not criteria:
            print("     (no acceptance_criteria)")
        for c in criteria:
            marker = "🪤" if c.startswith(GOTCHA_CRITERION_PREFIX) else "  "
            print(f"   {marker} {c}")
        print()

    print("=" * 70)
    print(f"{len(tasks)} task(s); {total_gotchas} gotcha criterion(s) stamped.")
    if total_gotchas == 0:
        print(
            "NO gotchas found. Either outcome coverage is disabled "
            "(MARCUS_OUTCOME_COVERAGE), no outcomes were extracted, or the "
            "enumeration LLM returned none for this spec."
        )
        return

    # Kaia's validation assertion (a): every gotcha must sit on an
    # implementation task — never a design/testing task.
    if misplaced:
        print("❌ PLACEMENT VIOLATION — gotchas on non-implementation tasks:")
        for name, ttype, n in misplaced:
            print(f"   - {name} [{ttype}]: {n} gotcha(s)")
    else:
        print(
            "✅ Every gotcha sits on an implementation task. #680 places "
            "failure modes where the code that can break them is written; "
            "#664 delivers them to that agent via request_next_task."
        )
    print(
        "(Watch the logs above for 'decomposition gap' warnings — outcomes "
        "with gotchas but no implementation task to host them.)"
    )


if __name__ == "__main__":
    asyncio.run(main())
