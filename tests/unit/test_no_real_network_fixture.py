"""
Unit tests for the socket-blocking autouse fixture itself (issue #857).

``pytest -m unit`` is supposed to run entirely in-process. The
autouse fixture ``_block_external_network`` in the root
``tests/conftest.py`` makes a real ``socket.connect``/``connect_ex``
raise immediately for every unit test, so an undeclared network
dependency fails loudly and machine-independently instead of passing
silently on a machine that happens to have a real service reachable.

Issue #857 was filed on exactly that failure mode: two test files
reached a real, reachable *local* Planka instance (loopback,
``127.0.0.1``) with nothing in either test declaring the dependency.
A fixture that left loopback open -- the design this fixture had
before this issue -- would have let that same incident straight
through unchanged, so loopback is now blocked by default too. A
one-time audit (one ``pytest -m unit`` run with loopback also
blocked, re-confirmed with the real ``marcus-planka``/
``marcus-postgres`` Docker containers stopped) found zero other unit
tests that legitimately need a real loopback socket, so there is no
opt-in list to carry here; a future test that does gets
``@pytest.mark.allows_loopback_network``.

These tests are kept permanent rather than a one-off audit script,
so a future change to the fixture that silently narrows its coverage
-- or quietly reopens loopback -- is caught by the suite itself.
"""

import socket

import pytest

pytestmark = pytest.mark.unit


class TestSocketBlockingFixtureBlocksNonLoopback:
    """connect/connect_ex to a non-loopback address are both blocked."""

    def test_connect_to_a_non_loopback_address_is_blocked(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(
            RuntimeError, match="Unit test attempted a network connection"
        ):
            s.connect(("198.51.100.7", 4242))

    def test_connect_ex_to_a_non_loopback_address_is_blocked(self) -> None:
        """The non-raising variant some clients use to probe a
        socket (e.g. a readiness check) must not be a back door
        around the ``connect`` guard."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(
            RuntimeError, match="Unit test attempted a network connection"
        ):
            s.connect_ex(("198.51.100.7", 4242))

    def test_the_error_names_the_attempted_host(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(RuntimeError, match=r"198\.51\.100\.7"):
            s.connect(("198.51.100.7", 4242))


class TestSocketBlockingFixtureBlocksLoopbackByDefault:
    """Issue #857's actual incident was on loopback (a real Planka on
    127.0.0.1), so loopback must be blocked by default, not exempted."""

    def test_connect_to_loopback_is_blocked_without_the_opt_in_marker(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(
            RuntimeError, match="Unit test attempted a network connection"
        ):
            s.connect(("127.0.0.1", 59998))

    def test_connect_ex_to_loopback_is_blocked_without_the_opt_in_marker(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(
            RuntimeError, match="Unit test attempted a network connection"
        ):
            s.connect_ex(("127.0.0.1", 59998))


class TestAllowsLoopbackNetworkOptIn:
    """A test that legitimately binds its own in-process listener can
    opt back in to a real loopback connection, explicitly."""

    @pytest.mark.allows_loopback_network
    def test_marked_test_can_reach_its_own_loopback_listener(self) -> None:
        """Binds to port 0 (the OS assigns a free port) and connects
        to that exact port -- never a hardcoded port number, which
        would risk colliding with something already listening on a
        given machine (Codex review, PR #872 round 1)."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        _, assigned_port = listener.getsockname()

        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2)
            # No exception: the opt-in marker re-opens loopback for
            # this test only: a real connection to this test's own
            # listener succeeds.
            client.connect(("127.0.0.1", assigned_port))
            client.close()
        finally:
            listener.close()
