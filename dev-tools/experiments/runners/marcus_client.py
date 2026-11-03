"""
Minimal MCP-over-HTTP client for the experiment runner (issue #595 Fix 3).

The session launcher calls Marcus several times per run
(``create_project``, ``get_project_status``). This module
provides a small client that performs the MCP handshake once and reuses
the session for every subsequent tool call.

The transport (``urllib``) lives in :class:`MarcusMCPClient`; the SSE
envelope parsing is split out into the pure :func:`parse_mcp_tool_result`
so it is unit-testable without a live server.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

_DEFAULT_MARCUS_URL = "http://localhost:4298/mcp/"


def parse_mcp_tool_result(raw: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse an MCP ``tools/call`` HTTP response into its structured result.

    Marcus returns tool results as a Server-Sent-Events stream: one or
    more lines prefixed with ``data:``, each carrying a JSON-RPC
    envelope. The tool's own return value sits at
    ``result.structuredContent.result``.

    Parameters
    ----------
    raw : bytes
        The raw HTTP response body.

    Returns
    -------
    Optional[Dict[str, Any]]
        The tool's structured result dict, or ``None`` if no parseable
        structured result is present.
    """
    for line in raw.decode(errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            envelope = json.loads(line[len("data:") :].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        result = envelope.get("result")
        if not isinstance(result, dict):
            continue
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            inner = structured.get("result")
            if isinstance(inner, dict):
                return inner
    return None


class MarcusMCPClient:
    """
    Reusable MCP-over-HTTP client for one Marcus server.

    Call :meth:`connect` once, then :meth:`call_tool` as often as needed —
    the session id from the handshake is reused on every call. All network
    failures are caught and surfaced as ``None`` / ``False`` rather than
    raised, so the runner's control loop can degrade gracefully on a
    transient error instead of crashing.

    Parameters
    ----------
    marcus_url : str
        The Marcus MCP HTTP endpoint.
    timeout : float
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        marcus_url: str = _DEFAULT_MARCUS_URL,
        timeout: float = 10.0,
    ) -> None:
        # The MCP streamable-HTTP endpoint is served at ``/mcp/``. A POST
        # to ``/mcp`` (no trailing slash) gets a 307 redirect that urllib
        # will not replay as a POST — it raises HTTPError instead. Force
        # the trailing slash so the redirect never happens.
        self._url = marcus_url.rstrip("/") + "/"
        self._timeout = timeout
        self._session_id: str = ""
        self._request_id = 0

    @property
    def connected(self) -> bool:
        """True once :meth:`connect` has established a session."""
        return bool(self._session_id)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _post(self, payload: Dict[str, Any]) -> bytes:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310
            body: bytes = resp.read()
            return body

    def connect(self) -> bool:
        """
        Perform the MCP handshake and store the session id.

        Returns
        -------
        bool
            True if a session was established; False on any network
            failure or a missing ``mcp-session-id`` header.
        """
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "experiment-runner", "version": "1.0"},
            },
        }
        try:
            req = urllib.request.Request(
                self._url,
                data=json.dumps(init_payload).encode(),
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(  # nosec B310
                req, timeout=self._timeout
            ) as resp:
                self._session_id = resp.headers.get("mcp-session-id", "") or ""
            if not self._session_id:
                return False
            # notifications/initialized is required by the MCP spec.
            self._post(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            print(f"  MCP connect failed ({type(exc).__name__}: {exc})")
            self._session_id = ""
            return False

    def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Call an MCP tool and return its structured result.

        Parameters
        ----------
        name : str
            Tool name, e.g. ``"get_desired_agent_count"``.
        arguments : Optional[Dict[str, Any]]
            Tool arguments; defaults to an empty mapping.

        Returns
        -------
        Optional[Dict[str, Any]]
            The tool's structured result dict, or ``None`` if the client
            is not connected, the request failed, or the response carried
            no parseable result.
        """
        if not self._session_id:
            return None
        self._request_id += 1
        try:
            raw = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            )
        except (urllib.error.URLError, OSError) as exc:
            print(f"  MCP call '{name}' failed ({type(exc).__name__}: {exc})")
            return None
        return parse_mcp_tool_result(raw)
