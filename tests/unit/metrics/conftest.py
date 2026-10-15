"""
Shared fixtures for dev-tools metrics tests.

The metrics scripts live under ``dev-tools/metrics/``.
The hyphenated parent ``dev-tools`` is not a valid Python package
name, so this conftest inserts ``dev-tools/`` onto ``sys.path`` once,
mirroring ``tests/unit/experiments/conftest.py``, and tests import
``from metrics.snapshot_traffic import ...``.

The insert is idempotent — repeated test runs do not stack entries.
"""

import sys
from pathlib import Path

_DEV_TOOLS = Path(__file__).parent.parent.parent.parent / "dev-tools"
_dev_tools_str = str(_DEV_TOOLS)
if _dev_tools_str not in sys.path:
    sys.path.insert(0, _dev_tools_str)
