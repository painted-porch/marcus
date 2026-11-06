"""
Unit tests for the socket-blocking autouse fixture itself (issue #857).

``pytest -m unit`` is supposed to run entirely in-process. The
autouse fixture ``_block_external_network`` in the root
``tests/conftest.py`` makes a real ``socket.connect``/``connect_ex``
to a non-loopback address raise immediately for every unit test, so
an undeclared network dependency fails loudly and machine-
independently instead of passing silently on a machine that happens
to have a real service reachable (the #856 round-4 failure mode this
issue was filed from: two test files reached a real, reachable local
Planka instance with nothing in the test declaring that dependency).

Issue #857 asked specifically for ``connect`` AND ``connect_ex`` to
be guarded; the pre-existing fixture only guarded ``connect``. These
tests prove both are now blocked for a non-loopback address, and that
loopback stays open (the fixture's own stated design, so a test that
legitimately starts its own ephemeral local server still works) --
kept permanent rather than a one-off audit, so a future change to the
fixture that silently narrows its coverage is caught by the suite
itself.
"""

import socket

import pytest

pytestmark = pytest.mark.unit


class TestSocketBlockingFixtureCoversConnectEx:
    """``connect_ex`` is blocked the same way ``connect`` already was."""

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

    def test_loopback_connect_ex_is_not_blocked(self) -> None:
        """Loopback stays open (the fixture's own design, so a test
        that starts its own ephemeral local server keeps working) --
        a refused connection on an unused port proves the real
        ``connect_ex`` ran, not the guard."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # A closed port on loopback returns a nonzero errno from the
        # real connect_ex rather than raising -- if the guard were
        # wrongly blocking loopback, this would raise RuntimeError
        # instead.
        result = s.connect_ex(("127.0.0.1", 59999))
        assert result != 0
