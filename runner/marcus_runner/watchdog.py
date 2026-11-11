"""
The cost watchdog: stop a run the moment its spend reaches the approved line.

Lifted from the Minecraft plumbing's ``cost_watchdog.py`` (issue #815 there),
with the defect the numerical verifier's run found fixed: the stop line is
read fresh on every poll when it comes from a file, so a raised cap takes
effect without a restart (``haiku-run-v2/REPORT.md``, section 8, item 4).

The comparison is ``>=``: a stop line is the ceiling the human approved, and
a run that has reached it has reached it. The watchdog returns on the poll
that breaches and never polls again; the run it watched is expected to be
gone.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - stopping our own run's processes, fixed argv, no shell
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

from .usage import total_dollars

StopLine = Union[float, Callable[[], float]]


@dataclass
class PollResult:
    """What one poll found and did."""

    dollars: float
    stop_line: float
    killed: bool


class StopLineFile:
    """
    A stop line read from a file on every poll.

    Parameters
    ----------
    path : Path
        A file holding one number. A missing or unreadable file keeps the
        last value read, and the first read must succeed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._last = float(path.read_text().strip())

    def __call__(self) -> float:
        """Return the stop line, re-read from the file."""
        try:
            self._last = float(self.path.read_text().strip())
        except (OSError, ValueError):
            pass
        return self._last


def _line(stop_line: StopLine) -> float:
    return float(stop_line()) if callable(stop_line) else float(stop_line)


def watch_once(
    state_dir: Path,
    stop_line: StopLine,
    kill: Callable[[], None],
    model_hint: str = "sonnet",
) -> PollResult:
    """
    Read accumulated cost once and kill the run if it has reached the stop line.

    Parameters
    ----------
    state_dir : Path
        The run's state directory.
    stop_line : float or callable
        The approved cap, or a callable that returns it fresh.
    kill : Callable[[], None]
        Called exactly once, synchronously, on breach.
    model_hint : str
        The price list for rows that do not name their model.

    Returns
    -------
    PollResult
    """
    line = _line(stop_line)
    dollars = total_dollars(state_dir, model_hint)
    breached = dollars >= line
    if breached:
        kill()
    return PollResult(dollars=dollars, stop_line=line, killed=breached)


def watch(
    state_dir: Path,
    stop_line: StopLine,
    kill: Callable[[], None],
    poll_seconds: float = 30.0,
    max_polls: Optional[int] = None,
    model_hint: str = "sonnet",
    on_poll: Optional[Callable[[PollResult], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[PollResult]:
    """
    Poll until the run is killed or ``max_polls`` is exhausted.

    Parameters
    ----------
    state_dir : Path
        The run's state directory.
    stop_line : float or callable
        The approved cap, or a callable that returns it fresh each poll.
    kill : Callable[[], None]
        The stop action.
    poll_seconds : float
        Sleep between polls.
    max_polls : Optional[int]
        Stop after this many polls without a breach; ``None`` is unbounded.
    model_hint : str
        The price list for rows that do not name their model.
    on_poll : Optional[Callable[[PollResult], None]]
        Called after every poll, for logging.
    sleep : Callable[[float], None]
        The sleep function; tests pass a stub.

    Returns
    -------
    Optional[PollResult]
        The last poll, or ``None`` if none ran.
    """
    polls = 0
    last: Optional[PollResult] = None
    while max_polls is None or polls < max_polls:
        last = watch_once(state_dir, stop_line, kill, model_hint)
        if on_poll:
            on_poll(last)
        polls += 1
        if last.killed:
            return last
        if max_polls is not None and polls >= max_polls:
            return last
        sleep(poll_seconds)
    return last


def kill_action(
    pid_files: Sequence[Path] = (), docker_name_pattern: Optional[str] = None
) -> Callable[[], None]:
    """
    Build the stop action: stop matching containers, then kill each pid in the files.

    Each step is wrapped on its own, so one failing half does not block the
    other: a half-stopped run still beats an unstopped one. Containers go
    first, because they are what spends.

    Parameters
    ----------
    pid_files : Sequence[Path]
        Files holding one pid each (the server under test, the agents).
    docker_name_pattern : Optional[str]
        A ``docker ps --filter name=`` substring for the run's containers.

    Returns
    -------
    Callable[[], None]
    """

    def _kill() -> None:
        if docker_name_pattern:
            try:
                names = subprocess.run(  # nosec B603 B607
                    [
                        "docker",
                        "ps",
                        "--filter",
                        f"name={docker_name_pattern}",
                        "--format",
                        "{{.Names}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.split()
                for name in names:
                    subprocess.run(
                        ["docker", "stop", "-t", "5", name],
                        capture_output=True,
                        timeout=15,
                    )  # nosec B603 B607
            except Exception as error:  # noqa: BLE001
                print(f"[watchdog] docker stop failed: {error}", file=sys.stderr)
        for pid_file in pid_files:
            try:
                pid = int(Path(pid_file).read_text().strip())
                subprocess.run(
                    ["kill", str(pid)], capture_output=True, timeout=5
                )  # nosec B603 B607
            except Exception as error:  # noqa: BLE001
                print(
                    f"[watchdog] kill from {pid_file} failed: {error}", file=sys.stderr
                )

    return _kill


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: the background watchdog a run script starts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="the run's state directory")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stop-line", type=float, help="approved hard dollar cap")
    group.add_argument(
        "--stop-line-file", help="a file holding the cap, re-read every poll"
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument(
        "--model", default="sonnet", help="price list for rows that do not name a model"
    )
    parser.add_argument(
        "--pid-file",
        action="append",
        default=[],
        help="a file holding a pid to kill on breach (repeatable)",
    )
    parser.add_argument("--docker-name-pattern", default=None)
    parser.add_argument("--max-minutes", type=float, default=None)
    args = parser.parse_args(argv)

    stop_line: StopLine = (
        StopLineFile(Path(args.stop_line_file))
        if args.stop_line_file
        else float(args.stop_line)
    )
    max_polls = (
        int(args.max_minutes * 60 / args.poll_seconds) if args.max_minutes else None
    )

    def log(result: PollResult) -> None:
        tag = "BREACH -- stopping the run" if result.killed else "ok"
        print(
            f"[watchdog] ${result.dollars:.2f} of ${result.stop_line:.2f}"
            f" stop line -- {tag}",
            flush=True,
        )

    watch(
        Path(args.state),
        stop_line,
        kill_action([Path(p) for p in args.pid_file], args.docker_name_pattern),
        poll_seconds=args.poll_seconds,
        max_polls=max_polls,
        model_hint=args.model,
        on_poll=log,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
