"""
Unit tests for ADR-0013 K2's registration field on ProjectConfig.

K2 (Accepted 2026-11-05): an extension declares, per project, a named
set of task types it serves and, for each, the evidence it requires
and whether checks may be injected. Registration is board state --
part of the project record (``ProjectConfig``), not a server config
file or a callable reference.

Before this field existed, ``ProjectConfig.from_dict`` filtered every
incoming dict to a fixed ``expected_fields`` set and silently dropped
anything outside it, so a registration block in ``options`` would
have vanished on the very next read.
"""

from datetime import datetime, timezone

import pytest

from src.core.project_registry import ProjectConfig

pytestmark = pytest.mark.unit


def _config(**overrides: object) -> ProjectConfig:
    """Build a minimal ProjectConfig, overriding only what a test needs."""
    defaults: dict = {
        "id": "proj-1",
        "name": "payments-rework",
        "provider": "sqlite",
        "provider_config": {},
        "created_at": datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc),
        "last_used": datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)  # type: ignore[arg-type]


class TestTaskTypeRegistrationsDefaultsEmpty:
    """A project that registers nothing behaves exactly as before K2."""

    def test_omitting_it_defaults_to_an_empty_dict(self) -> None:
        config = _config()

        assert config.task_type_registrations == {}

    def test_from_dict_on_a_pre_k2_payload_still_constructs(self) -> None:
        """A payload persisted before K2 existed has no such key at all."""
        legacy = {
            "id": "proj-1",
            "name": "payments-rework",
            "provider": "sqlite",
            "provider_config": {},
            "created_at": "2026-11-06T00:00:00+00:00",
            "last_used": "2026-11-06T00:00:00+00:00",
        }

        config = ProjectConfig.from_dict(legacy)

        assert config.task_type_registrations == {}


class TestTaskTypeRegistrationsRoundTrip:
    """The registration survives exactly the persistence path it needs to."""

    def test_to_dict_includes_the_registration(self) -> None:
        config = _config(
            task_type_registrations={
                "verify": {
                    "evidence_schema": {"required": ["verdict"]},
                    "checks_injectable": True,
                }
            }
        )

        data = config.to_dict()

        assert data["task_type_registrations"] == {
            "verify": {
                "evidence_schema": {"required": ["verdict"]},
                "checks_injectable": True,
            }
        }

    def test_from_dict_of_to_dict_round_trips_the_registration(self) -> None:
        """This is the exact gap K2 found: from_dict used to drop any key
        outside its hardcoded expected_fields set, so a registration
        written via to_dict would vanish on the very next read."""
        original = _config(
            task_type_registrations={
                "verify": {
                    "evidence_schema": {"required": ["verdict"]},
                    "checks_injectable": True,
                },
                "synthesize": {
                    "evidence_schema": {"required": ["summary"]},
                    "checks_injectable": False,
                },
            }
        )

        restored = ProjectConfig.from_dict(original.to_dict())

        assert restored.task_type_registrations == original.task_type_registrations

    def test_two_different_projects_keep_independent_registrations(self) -> None:
        """No shared mutable default -- a classic dataclass pitfall that
        would make every project's registration the same dict object."""
        a = _config(id="proj-a")
        b = _config(id="proj-b")

        a.task_type_registrations["verify"] = {"evidence_schema": {}}

        assert b.task_type_registrations == {}
