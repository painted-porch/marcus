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
