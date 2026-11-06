"""
Proof that the network guard covers UNMARKED unit tests (#726 Codex P1).

``test_external_connection_is_blocked_without_a_unit_marker`` below
deliberately carries NO ``pytest.mark.unit`` marker — it is the
control case. 136 of 293 files under ``tests/unit`` are unmarked,
and the full-suite CI workflow runs plain ``pytest`` (testpaths =
tests/unit) with live OPENAI_API_KEY / CLAUDE_API_KEY / Planka
credentials in the environment. A marker-only guard would leave those
files free to contact a paid service; scope must come from the path too.

If someone changes the guard back to marker-only, that test fails.

The other two tests in this file ARE marked ``unit`` individually
(issue #857, Codex review on PR #872): the module's own unmarked-by-
design control case above meant ``pytest -m unit`` never ran this
file at all, so a stale assertion about the guard's loopback policy
sat here unnoticed by the project's own fast baseline until Codex
caught it reviewing a PR that changed that exact policy. Marking only
those two, not the whole module, keeps the control case above intact
while closing that blind spot for the loopback-policy tests.
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


@pytest.mark.unit
def test_loopback_is_blocked_by_default() -> None:
    """Issue #857: loopback is blocked by default now, not exempted.

    The #856 incident #857 was filed on was a real Planka reachable
    on *loopback*, so an "external only" guard would have let that
    exact incident straight through unchanged -- the bug this test
    used to assert as correct behavior. Marked ``unit`` explicitly
    (not via this file's own unmarked module scope, which stays
    unmarked on purpose as the control case above): the full-suite
    job already ran this file, but ``pytest -m unit`` never did,
    which is exactly how a stale assertion here went unnoticed until
    Codex caught it reviewing PR #872.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="mock ALL external"):
            client.connect(("127.0.0.1", port))
    finally:
        client.close()
        server.close()


@pytest.mark.unit
@pytest.mark.allows_loopback_network
def test_loopback_opt_in_marker_reopens_it() -> None:
    """The ``allows_loopback_network`` marker is the one way to
    legitimately reach a real loopback socket from a unit test now."""
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
