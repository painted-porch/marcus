"""
A minimal MCP-over-HTTP client for run-side tooling.

Lifted from ``dev-tools/experiments/runners/marcus_client.py``, which the
Minecraft plumbing loaded by absolute file path from two scripts. The
transport (``urllib``) lives in :class:`MarcusMCPClient`; the SSE envelope
parsing is the pure :func:`parse_mcp_tool_result`, unit-testable without a
server.

One addition: :meth:`MarcusMCPClient.call` raises instead of returning
``None``, for callers that must read back every reply body rather than
infer success from silence (the live-check rule every fix is held to).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

_DEFAULT_MARCUS_URL = "http://127.0.0.1:4298/mcp/"


class MarcusClientError(RuntimeError):
    """A tool call that produced no readable reply."""


def parse_mcp_tool_result(raw: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse an MCP ``tools/call`` HTTP response into its structured result.

    Marcus returns tool results as a Server-Sent-Events stream: one or more
    lines prefixed with ``data:``, each a JSON-RPC envelope. The tool's own
    return value sits at ``result.structuredContent.result``.

    Parameters
    ----------
    raw : bytes
        The raw HTTP response body.

    Returns
    -------
    Optional[Dict[str, Any]]
        The tool's structured result, or ``None`` if none is present.
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

    Call :meth:`connect` once, then :meth:`call_tool` or :meth:`call` as
    often as needed; the session id from the handshake is reused.

    Parameters
    ----------
    marcus_url : str
        The Marcus MCP HTTP endpoint. A trailing slash is forced: a POST to
        ``/mcp`` without one gets a 307 that ``urllib`` will not replay.
    timeout : float
        Per-request timeout in seconds.
    """

    def __init__(
        self, marcus_url: str = _DEFAULT_MARCUS_URL, timeout: float = 10.0
    ) -> None:
        self._url = marcus_url.rstrip("/") + "/"
        self._timeout = timeout
        self._session_id: str = ""
        self._request_id = 0

    @property
    def url(self) -> str:
        """The endpoint this client posts to."""
        return self._url

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
        with urllib.request.urlopen(
            req, timeout=self._timeout
        ) as resp:  # nosec B310 - a local server this run started
            body: bytes = resp.read()
            return body

    def connect(self) -> bool:
        """
        Perform the MCP handshake and store the session id.

        Returns
        -------
        bool
            True if a session was established.
        """
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "marcus-runner", "version": "0.1"},
            },
        }
        try:
            req = urllib.request.Request(
                self._url,
                data=json.dumps(init_payload).encode(),
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(
                req, timeout=self._timeout
            ) as resp:  # nosec B310
                self._session_id = resp.headers.get("mcp-session-id", "") or ""
            if not self._session_id:
                return False
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            self._session_id = ""
            return False

    def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Call a tool and return its structured result, or ``None`` on any failure.

        Parameters
        ----------
        name : str
            The tool name.
        arguments : Optional[Dict[str, Any]]
            The tool arguments.

        Returns
        -------
        Optional[Dict[str, Any]]
            The reply body, or ``None`` when not connected, the request
            failed, or the response carried no structured result.
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
        except (urllib.error.URLError, OSError):
            return None
        return parse_mcp_tool_result(raw)

    def call(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Call a tool and return its reply body, raising when there is none.

        Parameters
        ----------
        name : str
            The tool name.
        arguments : Optional[Dict[str, Any]]
            The tool arguments.

        Returns
        -------
        Dict[str, Any]
            The reply body.

        Raises
        ------
        MarcusClientError
            When the client is not connected or the call produced no
            readable reply. The message names the tool.
        """
        if not self._session_id:
            raise MarcusClientError(f"{name}: not connected")
        reply = self.call_tool(name, arguments)
        if reply is None:
            raise MarcusClientError(f"{name}: no readable reply from {self._url}")
        return reply
