"""Shared task-type classification (implementation / design / testing).

A single source of truth for "what kind of task is this?" so every
consumer agrees. Two callers rely on it today:

* ``src/marcus_mcp/tools/task.py`` — instruction layering and workflow
  enforcement (the mandatory-workflow prompt only fires for
  ``implementation`` tasks).
* ``src/marcus_mcp/coordinator/outcome_coverage.py`` — #680 gotcha
  placement, which must land failure-mode criteria on the tasks whose
  code can actually break the outcome (implementation tasks), not on
  design/testing tasks.

Living in ``src.core`` (which depends on neither ``tools`` nor
``coordinator``) keeps both importers free of the import cycle that a
``coordinator -> tools.task`` dependency would create.

The classification mirrors the logic in
``src/integrations/ai_analysis_engine.py`` so instruction generation and
every downstream consumer make the same call for the same task.
"""

from __future__ import annotations

from typing import Any

TASK_TYPE_IMPLEMENTATION = "implementation"
TASK_TYPE_DESIGN = "design"
TASK_TYPE_TESTING = "testing"


def get_task_type(task: Any) -> str:
    """Determine a task's type from its parent hint, name, and labels.

    Parameters
    ----------
    task : Any
        A task-like object exposing ``name`` and (optionally) ``labels``
        and ``_parent_task_type``.

    Returns
    -------
    str
        One of ``"implementation"``, ``"design"``, or ``"testing"``.
        Defaults to ``"implementation"`` when nothing more specific
        matches.

    Notes
    -----
    Priority order:

    1. ``_parent_task_type`` attribute (subtasks inherit their parent's
       type so a subtask of a design task is treated as design).
    2. Inference from the task name or ``type:design`` / ``type:testing``
       labels.
    3. Default ``"implementation"``.
    """
    # Subtasks inherit their parent's type (same as ai_analysis_engine).
    if hasattr(task, "_parent_task_type"):
        return str(getattr(task, "_parent_task_type"))

    task_labels = getattr(task, "labels", []) or []
    name_lower = task.name.lower()

    if "design" in name_lower or "type:design" in task_labels:
        return TASK_TYPE_DESIGN
    if "test" in name_lower or "type:testing" in task_labels:
        return TASK_TYPE_TESTING
    return TASK_TYPE_IMPLEMENTATION
