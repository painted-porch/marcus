"""
Unit tests: the MCP client's reply-text helper is public (issue #910).

``src/worker/inspector.py`` reads the text out of a ``CallToolResult`` in
one place. The citation extension's exporter needs the same thing, and
until #910 it imported the private ``_extract_text_from_result`` through
its kernel door, the one non-public name on the extension contract's
surface table. The helper is now ``extract_text_from_result``; the private
name stays as an alias so nothing internal moves.
"""

from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from src.worker import inspector
from src.worker.inspector import extract_text_from_result

pytestmark = pytest.mark.unit


class TestExtractTextFromResult:
    """The contract: the first text item's text, or the empty string."""

    def test_returns_the_first_text_items_text(self) -> None:
        """Two text items: the first wins."""
        result = CallToolResult(
            content=[
                TextContent(type="text", text="first"),
                TextContent(type="text", text="second"),
            ]
        )
        assert extract_text_from_result(result) == "first"

    def test_no_content_is_the_empty_string(self) -> None:
        """An empty reply reads as empty text, never None."""
        assert extract_text_from_result(CallToolResult(content=[])) == ""

    def test_an_object_with_a_text_attribute_counts(self) -> None:
        """A test double carrying ``.text`` is read like a TextContent."""
        result = SimpleNamespace(content=[SimpleNamespace(text="doubled")])
        assert extract_text_from_result(result) == "doubled"  # type: ignore[arg-type]

    def test_the_private_name_is_the_same_function(self) -> None:
        """Internal callers keep working: the alias is the public function itself."""
        assert inspector._extract_text_from_result is extract_text_from_result
