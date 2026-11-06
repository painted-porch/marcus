"""
Unit tests: replacing ``MarcusServer.kanban_client`` rebinds every
long-lived service that captured a reference to it (issue #856 round 3).

``AssignmentMonitor`` (and its nested ``AssignmentReconciler``) and
``AssignmentLeaseManager`` each capture the kanban client passed to them
at construction into their own ``self.kanban_client`` attribute and
never see it change again. Round 1's always-fresh-client fix for
``create_project`` (a second project on the same provider now gets a
genuinely new client object instead of a mutated old one) made that
latent bug reachable: those three services -- built once per server
process -- kept talking to the OLD client. ``kanban_client`` is now a
property; its setter is the only writer and propagates a replacement to
every holder.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _build_server():
    """A real MarcusServer instance, built via ``__new__`` rather than
    ``__init__``.

    ``MarcusServer()`` requires a large amount of config/filesystem
    mocking that is currently broken elsewhere in this test file
    (``TestKanbanInitialization``/``TestMarcusServerInitialization``
    are skipped with "Config singleton mocking needs refactoring").
    What's under test here is the ``kanban_client`` property's setter
    logic alone -- it only reads ``self.assignment_monitor`` and
    ``self.lease_manager`` via ``getattr(..., None)``, so a real
    instance with just those three attributes set is sufficient and
    exercises the actual property descriptor on the real class.
    """
    from src.marcus_mcp.server import MarcusServer

    server = MarcusServer.__new__(MarcusServer)
    server._kanban_client = None
    server.assignment_monitor = None
    server.lease_manager = None
    return server


class TestKanbanClientRebind:
    """Setting ``server.kanban_client`` propagates to every captured holder."""

    def test_assignment_monitor_sees_the_new_client(self) -> None:
        server = _build_server()
        old_client = MagicMock(name="client_A")
        new_client = MagicMock(name="client_B")

        server.assignment_monitor = MagicMock()
        server.assignment_monitor.kanban_client = old_client
        server.assignment_monitor.reconciler = MagicMock()
        server.assignment_monitor.reconciler.kanban_client = old_client

        server.kanban_client = new_client

        assert server.assignment_monitor.kanban_client is new_client, (
            "AssignmentMonitor kept talking to the old client after "
            "create_project built a fresh one for a second project -- "
            "this is exactly what made _check_for_reversions query the "
            "wrong board (#856 round 3)."
        )

    def test_assignment_monitors_reconciler_sees_the_new_client(self) -> None:
        server = _build_server()
        old_client = MagicMock(name="client_A")
        new_client = MagicMock(name="client_B")

        server.assignment_monitor = MagicMock()
        server.assignment_monitor.kanban_client = old_client
        server.assignment_monitor.reconciler = MagicMock()
        server.assignment_monitor.reconciler.kanban_client = old_client

        server.kanban_client = new_client

        assert server.assignment_monitor.reconciler.kanban_client is new_client

    def test_lease_manager_sees_the_new_client(self) -> None:
        server = _build_server()
        old_client = MagicMock(name="client_A")
        new_client = MagicMock(name="client_B")

        server.lease_manager = MagicMock()
        server.lease_manager.kanban_client = old_client

        server.kanban_client = new_client

        assert server.lease_manager.kanban_client is new_client, (
            "AssignmentLeaseManager kept talking to the old client -- "
            "lease recovery would check terminal status against the "
            "wrong board (#856 round 3)."
        )

    def test_server_itself_reads_back_the_new_client(self) -> None:
        server = _build_server()
        new_client = MagicMock(name="client_B")

        server.kanban_client = new_client

        assert server.kanban_client is new_client

    def test_no_monitor_or_lease_manager_yet_does_not_raise(self) -> None:
        """Before _initialize_monitoring_systems has ever run (both are
        None, as at server construction), setting the client must be a
        no-op for the rebind, not an error."""
        server = _build_server()
        assert server.assignment_monitor is None
        assert server.lease_manager is None

        server.kanban_client = MagicMock(name="client_A")  # must not raise

        assert server.kanban_client is not None
