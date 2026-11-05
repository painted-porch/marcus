"""The interim-cost stop check for a condition A/C run.

Condition A's runner is a single sequential loop: one case at a time,
one ``claude -p`` writer pass and one self-check pass per round, up to
three rounds, before moving to the next case. That shape means the
stop check belongs inline, before launching each next pass, not as a
separate process watching from outside the way Minecraft's parallel
Docker containers needed (#815, ``cost_watchdog.py``). There is only
one process spending money here, and it can simply decline to spend
more of it.

The comparison is ``>=``, not ``>``: a stop line is the ceiling a human
approved, and a run that has reached it has reached it, independent of
whether the next token would have pushed it strictly past.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for _entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from runners.numeric_cost_lib import total_dollars  # noqa: E402


@dataclass
class StopCheck:
    """The result of checking accumulated spend before the next pass.

    Parameters
    ----------
    spent : float
        Total dollars accumulated across every transcript checked so far.
    stop_line : float
        The approved ceiling for the whole run.
    should_stop : bool
        True when ``spent`` has reached ``stop_line`` and no further
        pass should be launched.
    """

    spent: float
    stop_line: float
    should_stop: bool


def check_before_next_pass(transcript_paths: List[Path], stop_line: float) -> StopCheck:
    """Decide whether to launch the next pass, from real spend so far.

    Call this immediately before every ``claude -p`` invocation in the
    condition A loop -- before each case, and before each self-check
    round within a case -- passing every transcript path written by
    the run up to that point, including the one just finished.

    Parameters
    ----------
    transcript_paths : List[Path]
        Every transcript this run has written so far.
    stop_line : float
        The approved ceiling for the whole run.

    Returns
    -------
    StopCheck
        ``should_stop`` is the answer a caller must act on by not
        launching the next pass, not merely a value to log.
    """
    spent = total_dollars(transcript_paths)
    return StopCheck(spent=spent, stop_line=stop_line, should_stop=spent >= stop_line)
