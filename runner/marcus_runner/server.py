"""
A Marcus server under test: private, model-free, and provably this run's.

What every extension's run script did by hand, once:

* a config with the SQLite provider, the model switched off and an empty key
  (a placeholder key still fires one real call on startup: issue #905 and
  ``~/marcus-experiments/numeric-verifier/RESULTS.md``, "Incident"), the
  context, memory and events features off, and its own HTTP port;
* an environment with every model-provider key stripped, its own
  ``MARCUS_DATA_DIR`` and ``MARCUS_CONFIG``, telemetry off, and
  ``PYTHONPATH`` at the pinned checkout;
* a refusal to start on a port someone else holds, a readiness probe, a
  check that the port's owner is this run's server, and a pid file;
* a count of model-provider hosts in the server log afterwards, which must be zero.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess  # nosec B404 - starting our own server, fixed argv
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

#: Keys stripped from the server's environment, so the server can make no paid call.
STRIPPED_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
)
#: Hosts whose appearance in a server log means a model-provider call was made.
MODEL_HOSTS = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "api.perplexity.ai",
)


def free_port() -> int:
    """Return a TCP port nothing is listening on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something accepts connections on ``host:port``."""
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def port_owner(port: int) -> Optional[int]:
    """Return the pid listening on ``port``, by ``lsof``, or ``None``."""
    try:
        out = subprocess.run(  # nosec B603 B607
            ["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out[0]) if out else None


def command_of(pid: int) -> str:
    """Return the command line of ``pid``, or an empty string."""
    try:
        return subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()  # nosec B603 B607
    except (OSError, subprocess.SubprocessError):
        return ""


def write_config(
    path: Path,
    *,
    port: int,
    project_name: str,
    sqlite_db_path: Optional[Path] = None,
    host: str = "127.0.0.1",
    single_project_mode: bool = False,
) -> Dict[str, object]:
    """
    Write the server's config: SQLite, model off with an empty key, own port.

    Parameters
    ----------
    path : Path
        Where to write the JSON.
    port : int
        The HTTP port.
    project_name : str
        The default project name the server starts with.
    sqlite_db_path : Optional[Path]
        The board file; when omitted the data-directory resolver decides (issue #792).
    host : str
        The bind address.
    single_project_mode : bool
        ``False`` lets one server hold several projects (ADR-0013 K6).

    Returns
    -------
    Dict[str, object]
        The config written.
    """
    kanban: Dict[str, object] = {"provider": "sqlite"}
    if sqlite_db_path is not None:
        kanban["sqlite_db_path"] = str(sqlite_db_path)
    config: Dict[str, object] = {
        "kanban": kanban,
        "default_project_name": f"{project_name} - Main Board",
        "project_name": project_name,
        "single_project_mode": single_project_mode,
        "auto_find_board": False,
        "ai": {"provider": "anthropic", "enabled": False, "anthropic_api_key": ""},
        "features": {
            "events": False,
            "context": False,
            "memory": False,
            "visibility": False,
        },
        "transport": {
            "type": "http",
            "http_host": host,
            "http_port": port,
            "http_path": "/mcp",
            "http_enabled": True,
            "dual_mode": False,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return config


def build_env(
    checkout: Path,
    data_dir: Path,
    config_path: Path,
    base: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Build the server's environment: keys stripped, paths set, telemetry off.

    Parameters
    ----------
    checkout : Path
        The pinned Marcus checkout, which becomes ``PYTHONPATH``.
    data_dir : Path
        ``MARCUS_DATA_DIR``.
    config_path : Path
        ``MARCUS_CONFIG``.
    base : Optional[Dict[str, str]]
        The environment to start from; the process environment by default.

    Returns
    -------
    Dict[str, str]
    """
    env = {
        k: v
        for k, v in (base if base is not None else os.environ).items()
        if k not in STRIPPED_KEYS
    }
    env["MARCUS_DATA_DIR"] = str(data_dir)
    env["MARCUS_CONFIG"] = str(config_path)
    env["MARCUS_TELEMETRY"] = "off"
    env["PYTHONPATH"] = str(checkout)
    return env


def model_calls_in_log(log_path: Path) -> int:
    """Count lines of the server log that name a model-provider host."""
    if not log_path.is_file():
        return 0
    text = log_path.read_text(errors="replace")
    return sum(
        line.count(h) > 0
        for line in text.splitlines()
        for h in MODEL_HOSTS
        if h in line
    )


class ServerUnderTest:
    """
    Start, probe and stop one private Marcus server.

    Parameters
    ----------
    checkout : Path
        The pinned Marcus checkout to serve from (its ``src`` is importable).
    data_dir : Path
        The run's own ``MARCUS_DATA_DIR``.
    config_path : Path
        The config written by :func:`write_config`.
    port : int
        The HTTP port in that config.
    log_path : Path
        Where the server's stdout and stderr go.
    python : str
        The interpreter to run ``src.marcus_mcp.server`` with.
    """

    def __init__(
        self,
        checkout: Path,
        data_dir: Path,
        config_path: Path,
        port: int,
        log_path: Path,
        python: str = "python",
    ) -> None:
        self.checkout = checkout
        self.data_dir = data_dir
        self.config_path = config_path
        self.port = port
        self.log_path = log_path
        self.python = python
        self.process: Optional[subprocess.Popen[bytes]] = None

    @property
    def url(self) -> str:
        """The MCP endpoint."""
        return f"http://127.0.0.1:{self.port}/mcp/"

    @property
    def pid_file(self) -> Path:
        """Where the pid is written for the watchdog."""
        return self.data_dir / "server.pid"

    def start(self, wait_seconds: float = 60.0) -> int:
        """
        Start the server and wait until it answers and the port is ours.

        Parameters
        ----------
        wait_seconds : float
            How long to wait for readiness.

        Returns
        -------
        int
            The server's pid.

        Raises
        ------
        RuntimeError
            If the port is held by someone else, the server never answers, or
            the port's owner is not a Marcus server.
        """
        if port_in_use(self.port):
            raise RuntimeError(
                f"port {self.port} is already in use; refusing to run against"
                " another session's Marcus"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = build_env(self.checkout, self.data_dir, self.config_path)
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(  # nosec B603
                [self.python, "-m", "src.marcus_mcp.server", "--http", "--all-tools"],
                cwd=str(self.checkout),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(f"{self.process.pid}\n")
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"the server exited with {self.process.returncode};"
                    f" see {self.log_path}"
                )
            if self._answers():
                break
            time.sleep(0.5)
        else:
            self.stop()
            raise RuntimeError(
                f"the server never answered on {self.url} within {wait_seconds}s;"
                f" see {self.log_path}"
            )
        owner = port_owner(self.port)
        if owner is None or "src.marcus_mcp.server" not in command_of(owner):
            self.stop()
            raise RuntimeError(
                f"port {self.port} is not owned by a Marcus server of this run"
                f" (owner {owner})"
            )
        return self.process.pid

    def _answers(self) -> bool:
        try:
            urllib.request.urlopen(
                self.url, timeout=2
            )  # nosec B310 - our own local server
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            return False

    def stop(self, wait_seconds: float = 20.0) -> None:
        """Stop the server and whoever else holds the port, then wait."""
        pids: List[int] = []
        if self.process is not None and self.process.poll() is None:
            pids.append(self.process.pid)
        owner = port_owner(self.port)
        if owner is not None and owner not in pids:
            pids.append(owner)
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def model_calls(self) -> int:
        """How many model-provider lines the log carries; a model-free run has zero."""
        return model_calls_in_log(self.log_path)
