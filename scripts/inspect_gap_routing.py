"""Empirical check: where do gap-filled outcomes' criteria land? (#680 finding)

#607-step-4 rolls each uncovered outcome ("gap") onto an existing anchor
task's ``completion_criteria`` instead of synthesizing a new
implementation task. This script runs the real decomposition pipeline on
the snake spec and reports, for every task that received a gap-fill
criterion (stamped with OUTCOME_GAP_CRITERION_PREFIX), what TYPE of task
the anchor is. The question it answers: are gap-fills landing on
non-implementation tasks?

Run:  python scripts/inspect_gap_routing.py
"""

import asyncio

from src.ai.advanced.prd.advanced_parser import (
    AdvancedPRDParser,
    ProjectConstraints,
)
from src.core.task_classification import get_task_type
from src.marcus_mcp.coordinator.outcome_coverage import (
    OUTCOME_GAP_CRITERION_PREFIX,
)

SNAKE_SPEC = """\
Build a browser-based Snake game.

The player controls a snake with the arrow keys. The snake moves
continuously around a grid. Eating food makes the snake grow longer and
increases the score. The game ends if the snake runs into a wall or into
its own body. Show the current score, and a game-over screen with a
restart option.
"""


async def main() -> None:
    """Decompose the snake spec and report gap-fill anchor task types."""
    parser = AdvancedPRDParser()
    result = await parser.parse_prd_to_tasks(
        SNAKE_SPEC, ProjectConstraints(complexity_mode="standard")
    )

    print("\nTasks carrying gap-fill criteria (#607-step-4 rollup):\n")
    impl_count = 0
    nonimpl_count = 0
    for task in result.tasks:
        gap_criteria = [
            c
            for c in (task.completion_criteria or [])
            if c.startswith(OUTCOME_GAP_CRITERION_PREFIX)
        ]
        if not gap_criteria:
            continue
        ttype = get_task_type(task)
        if ttype == "implementation":
            impl_count += 1
        else:
            nonimpl_count += 1
        print(f"── [{ttype}] {task.name}")
        for c in gap_criteria:
            print(f"     • {c}")
        print()

    print("=" * 70)
    print(
        f"gap-fill anchors: {impl_count} implementation, "
        f"{nonimpl_count} non-implementation"
    )
    if nonimpl_count:
        print(
            "→ YES: gap-filled outcomes ARE landing on completion_criteria "
            "of non-implementation tasks."
        )
    else:
        print("→ NO: all gap-fill anchors were implementation tasks this run.")


if __name__ == "__main__":
    asyncio.run(main())
