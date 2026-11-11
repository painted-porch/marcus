"""Unit tests for marcus_runner.client."""

import pytest
from marcus_runner.client import (
    MarcusClientError,
    MarcusMCPClient,
    parse_mcp_tool_result,
)

pytestmark = pytest.mark.unit

SSE = (
    b"event: message\n"
    b'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"x"}],'
    b'"structuredContent":{"result":{"success":true,"project_id":"p1"}}}}\n\n'
)


def test_parse_returns_the_structured_result():
    """The tool's own return value sits at result.structuredContent.result."""
    assert parse_mcp_tool_result(SSE) == {"success": True, "project_id": "p1"}


def test_parse_returns_none_without_a_structured_result():
    """A body with no data line, or a non-dict result, parses to None."""
    assert parse_mcp_tool_result(b"nothing here") is None
    assert (
        parse_mcp_tool_result(
            b'data: {"jsonrpc":"2.0","result":{"structuredContent":{"result":[1]}}}\n'
        )
        is None
    )


def test_url_gets_a_trailing_slash():
    """A POST to /mcp without the slash is redirected and lost; the client forces it."""
    assert (
        MarcusMCPClient("http://127.0.0.1:4777/mcp").url == "http://127.0.0.1:4777/mcp/"
    )


def test_call_tool_is_none_and_call_raises_when_not_connected():
    """Without a session nothing is sent."""
    client = MarcusMCPClient("http://127.0.0.1:1/mcp")
    assert client.call_tool("get_project_status") is None
    with pytest.raises(MarcusClientError, match="not connected"):
        client.call("get_project_status")


def test_call_returns_the_reply_body_and_raises_on_an_unreadable_one(monkeypatch):
    """call() reads the body back; an empty reply is an error naming the tool."""
    client = MarcusMCPClient("http://127.0.0.1:1/mcp")
    client._session_id = "s"
    monkeypatch.setattr(client, "_post", lambda payload: SSE)
    assert client.call("create_project", {"x": 1})["project_id"] == "p1"
    monkeypatch.setattr(client, "_post", lambda payload: b"")
    with pytest.raises(MarcusClientError, match="create_project"):
        client.call("create_project")
