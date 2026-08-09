"""
Proof that the network guard covers UNMARKED unit tests (#726 Codex P1).

This file deliberately carries NO ``pytest.mark.unit`` marker — it is
the control case. 136 of 293 files under ``tests/unit`` are unmarked,
and the full-suite CI workflow runs plain ``pytest`` (testpaths =
tests/unit) with live OPENAI_API_KEY / CLAUDE_API_KEY / Planka
credentials in the environment. A marker-only guard would leave those
files free to contact a paid service; scope must come from the path too.

If someone changes the guard back to marker-only, these tests fail.
"""

import socket

import pytest


def test_external_connection_is_blocked_without_a_unit_marker() -> None:
    """An unmarked test under tests/unit must still be guarded."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="mock ALL external"):
            sock.connect(("api.openai.com", 443))
    finally:
        sock.close()


def test_loopback_is_still_allowed() -> None:
    """Local-socket fixtures must keep working — only external is blocked."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))  # must NOT raise
    finally:
        client.close()
        server.close()
