"""
Tests pinning who actually reads the Marcus service registry.

Every Marcus server writes a JSON file describing itself to
``~/.marcus/services`` on startup, so that a tool can discover the
servers running on a machine. ``CLI.md`` describes this as being there
"for other tools to discover".

**Nothing reads it.** ``discover_services()`` has no caller in this
repository, and none in Cato, Seneca, Posidonius or marcus-mini either.
The registry is write-only across all of them.

That matters because the architecture doc said otherwise. It claimed
"Cato calls ``MarcusServiceRegistry.discover_services()``", and acting on
that line rather than on Cato's code produced a change whose stated
benefit could not happen: a field was added to the registry entry so Cato
could tell two Marcus instances apart, when Cato never reads the entry.
Cato is pointed at Marcus by ``marcus_data_path`` in its own
``config.json`` and reads the data directory directly, and it already
distinguishes instances through ``marcus_data_paths``.

These two tests exist so the next person is not misled the same way. One
pins the corrected doc. The other fails if a reader appears inside
Marcus, because the registry's machine-wide scope is a decision that
depends entirely on who reads it, and that decision should be revisited
out loud rather than inherited silently.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestTheDocumentationMatchesTheCode:
    """The stale claim, and the tripwire for the assumption behind it."""

    def test_the_doc_does_not_claim_cato_calls_discovery(self) -> None:
        """A reader must not be told a consumer exists that does not."""
        doc = (
            Path(__file__).parents[3]
            / "docs"
            / "source"
            / "systems"
            / "architecture"
            / "15-service-registry.md"
        )

        text = doc.read_text()

        assert "Cato calls `MarcusServiceRegistry.discover_services()`" not in text

    def test_nothing_in_the_repository_reads_the_registry(self) -> None:
        """Pins that every potential consumer is out of tree.

        If a reader appears inside Marcus, this fails, and whoever added
        it should say so in the docs. Two things then need deciding: the
        registry is machine-wide rather than per data directory on the
        assumption that its consumers are external, and an in-tree
        caller changes that; and ``discover_services`` deletes entries
        whose process is gone, so a new caller starts mutating
        machine-wide state that other tools may be reading.
        """
        src = Path(__file__).parents[3] / "src"
        callers = sorted(
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if "discover_services(" in path.read_text()
            and path.name != "service_registry.py"
        )

        assert not callers, (
            "something in src/ now reads the service registry: "
            f"{callers}. The registry is machine-wide on the assumption "
            "that its consumers are external, and discovery prunes other "
            "instances' entries; revisit both if Marcus itself starts "
            "discovering instances."
        )
