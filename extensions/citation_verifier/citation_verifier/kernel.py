"""
The kernel surface this extension depends on, in one place.

ADR-0013 K1/K2: an extension pulls work through the board and never has the
kernel call into it.
This module is the *only* place the citation verifier imports Marcus's own
code, so the dependency is auditable: ``tests/unit/extensions/citation_verifier/
test_kernel_surface.py`` fails if any other module imports ``src.`` or if this
one imports anything beyond the list below.

Since 2026-11-13 (ADR-0013 K2 decided: extensions write their cards over MCP
only; issue #911 closed the seed-spec gaps) the surface is the MCP client and
nothing else. The seeder creates the project and its cards with one
``create_project`` call, the workers pull and report through the same client,
and the exporter reads bundles back through it. No board, registry or
persistence code is imported any more.

- ``Inspector``: the MCP client the seeder, the workers and the exporter use
  to talk to a running server. Client code, not kernel internals.
- ``extract_text_from_result``: the client's public reader of a tool reply's
  text (issue #910).
- ``REPO_ROOT``: the Marcus checkout, so the client is importable when the
  extension runs from a checkout without ``marcus-ai`` installed.

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

from src.worker.inspector import Inspector, extract_text_from_result  # noqa: E402

__all__ = [
    "REPO_ROOT",
    "Inspector",
    "extract_text_from_result",
]
