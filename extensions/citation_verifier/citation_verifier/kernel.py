"""
The kernel surface this extension depends on, in one place.

ADR-0013 K1/K2: an extension pulls work through the board and never has the
kernel call into it.
This module is the *only* place the citation verifier imports Marcus's own
code, so the dependency is auditable: ``tests/unit/extensions/citation_verifier/
test_kernel_surface.py`` fails if any other module imports ``src.`` or if this
one imports anything beyond the list below.

What each symbol is for, and its MCP equivalent where one exists today:

- ``ProjectRegistry`` and ``register_sqlite_board``: register a directly
  seeded SQLite board as a project (issue #741, GH-306).
  MCP equivalent: ``add_project(provider="sqlite", config={project_id, db_path})``
  (#888), minus the legacy UUID-key migration.
- ``SQLiteKanban``: create the seeded cards with their final ids, inputs,
  task types and dependencies in one in-process pass (issue #844).
  MCP equivalent: ``create_project(options.tasks=[...])`` seed specs, which
  today carry name, labels, depends_on, estimated_hours and priority but not
  ``task_type``, ``inputs`` or a caller-supplied id.
- ``write_task_metadata_rows``: the sanctioned write of the ``task_metadata``
  rows Cato renders (issue #741 step 3). No MCP equivalent.
- ``Inspector``: the MCP client the workers and the exporter use to talk to a
  running server. Client code, not kernel internals.
- ``extract_text_from_result``: a private helper of that client the exporter
  borrows to read tool replies. Named here as a smell, not hidden.
- ``REPO_ROOT``: the Marcus checkout, for the seeder's default metadata path.

See ``docs/architecture/extension-contract.md`` for the written contract.
"""

import sys
from pathlib import Path

# The extension runs from a Marcus checkout without ``marcus-ai`` installed,
# so the checkout root must be importable. This is the one path edit in the
# package; every other module imports the kernel through here.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.persistence import write_task_metadata_rows  # noqa: E402
from src.core.project_registry import (  # noqa: E402
    ProjectRegistry,
    register_sqlite_board,
)
from src.integrations.providers.sqlite_kanban import SQLiteKanban  # noqa: E402
from src.worker.inspector import Inspector  # noqa: E402
from src.worker.inspector import (  # noqa: E402
    _extract_text_from_result as extract_text_from_result,
)

__all__ = [
    "REPO_ROOT",
    "Inspector",
    "ProjectRegistry",
    "SQLiteKanban",
    "extract_text_from_result",
    "register_sqlite_board",
    "write_task_metadata_rows",
]
