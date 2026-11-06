"""
Unit tests for Planka carrying a registered project's registry id
down to its KanbanClient (issue #854, the real fix for #849/#853).

Task.project_id was stamped from Planka's own project id, while a
registry entry created by add_project/sync_projects carries an
independently generated UUID as its own id. Carrying
registry_project_id down at construction and having _card_to_task
prefer it (src/integrations/kanban_client.py) closes that gap by
construction instead of resolving it after the fact.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _planka(config: dict) -> "Planka":  # noqa: F821
    from src.integrations.providers.planka import Planka

    with (
        patch("pathlib.Path.exists", return_value=False),
        patch.dict("os.environ", {}, clear=True),
        patch(
            "src.integrations.kanban_client.KanbanClient._load_workspace_state",
            return_value=None,
        ),
    ):
        return Planka(config)


def _planka_with_stale_config_file(
    config: dict, config_file_project_id: str, config_file_board_id: str
) -> "Planka":  # noqa: F821
    """Build a Planka provider as if config_marcus.json on disk already

    holds a DIFFERENT project's ids than the ``config`` this provider
    is being constructed for -- the exact disagreement issue #856
    round 2 found.
    """
    from src.integrations.kanban_client import KanbanClient
    from src.integrations.providers.planka import Planka

    def _fake_load_config(self: KanbanClient) -> None:
        self.project_id = config_file_project_id
        self.board_id = config_file_board_id

    with (
        patch.object(KanbanClient, "_load_config", _fake_load_config),
        patch.dict("os.environ", {}, clear=True),
        patch(
            "src.integrations.kanban_client.KanbanClient._load_workspace_state",
            return_value=None,
        ),
    ):
        return Planka(config)


class TestPlankaCarriesTheRegistryProjectId:
    def test_the_registry_id_is_carried_to_the_client(self) -> None:
        planka = _planka(
            {"project_id": "planka-proj-77", "registry_project_id": "uuid-registry-1"}
        )

        assert planka.client.registry_project_id == "uuid-registry-1"

    def test_a_card_from_this_provider_is_stamped_with_the_registry_id(self) -> None:
        planka = _planka(
            {"project_id": "planka-proj-77", "registry_project_id": "uuid-registry-1"}
        )

        task = planka.client._card_to_task({"id": "card-1", "name": "Task"})

        assert task.project_id == "uuid-registry-1"

    def test_no_registry_id_falls_back_to_plankas_own(self) -> None:
        """A standalone construction with no registry context keeps

        today's behavior exactly -- no regression for a caller that
        never goes through project_context_manager."""
        planka = _planka({"project_id": "planka-proj-77"})

        task = planka.client._card_to_task({"id": "card-1", "name": "Task"})

        assert task.project_id == "planka-proj-77"


class TestPlankaOverridesAStaleConfigFile:
    """Issue #856 round 2, Codex P1: ``KanbanClient._load_config`` reads

    project_id/board_id from config_marcus.json BEFORE the env-var
    fallback that ``Planka.__init__`` just set from THIS provider's own
    config -- so when the file already holds some OTHER project's ids,
    the ones this client was actually constructed for never took
    effect, leaving it bound to the wrong board while still stamping
    cards with the registry id of the project it was meant to serve.
    """

    def test_the_constructed_for_project_id_wins_over_the_config_file(self) -> None:
        planka = _planka_with_stale_config_file(
            {"project_id": "registry-B-project", "board_id": "registry-B-board"},
            config_file_project_id="config-A-project",
            config_file_board_id="config-A-board",
        )

        assert planka.client.project_id == "registry-B-project"

    def test_the_constructed_for_board_id_wins_over_the_config_file(self) -> None:
        planka = _planka_with_stale_config_file(
            {"project_id": "registry-B-project", "board_id": "registry-B-board"},
            config_file_project_id="config-A-project",
            config_file_board_id="config-A-board",
        )

        assert planka.client.board_id == "registry-B-board"

    def test_cards_are_stamped_with_bs_registry_id_not_as(self) -> None:
        """End to end: once the client is correctly bound to B, a card
        it fetches must be stamped with B's registry id -- the exact
        leak Codex named (A's tasks passing B's scoping filter)."""
        planka = _planka_with_stale_config_file(
            {
                "project_id": "registry-B-project",
                "board_id": "registry-B-board",
                "registry_project_id": "uuid-registry-B",
            },
            config_file_project_id="config-A-project",
            config_file_board_id="config-A-board",
        )

        task = planka.client._card_to_task({"id": "card-1", "name": "Task"})

        assert task.project_id == "uuid-registry-B"

    def test_no_supplied_ids_leaves_the_config_file_values_alone(self) -> None:
        """A standalone construction with no project/board ids supplied
        (e.g. a script that relies on config_marcus.json) must not be
        broken by this override -- nothing to override with."""
        planka = _planka_with_stale_config_file(
            {},
            config_file_project_id="config-A-project",
            config_file_board_id="config-A-board",
        )

        assert planka.client.project_id == "config-A-project"
        assert planka.client.board_id == "config-A-board"
