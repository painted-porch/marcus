"""Unit tests for marcus_runner.pricing."""

import pytest
from marcus_runner import pricing

pytestmark = pytest.mark.unit


def test_resolve_accepts_aliases_dated_ids_and_provider_prefixes():
    """Minecraft records say 'sonnet'; configs say a dated, prefixed id."""
    assert pricing.resolve("sonnet") == "claude-sonnet-4-5"
    assert pricing.resolve("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert pricing.resolve("anthropic/claude-sonnet-5") == "claude-sonnet-5"
    assert pricing.resolve("Opus-5") == "claude-opus-5"


def test_resolve_refuses_an_unknown_model():
    """A run is never priced by guess."""
    with pytest.raises(KeyError):
        pricing.resolve("claude-9")


def test_price_follows_the_table():
    """A million Sonnet 5 input tokens is two dollars; cache read is a tenth."""
    assert pricing.price({"input": 1_000_000}, "claude-sonnet-5") == 2.0
    assert pricing.price({"cacheRead": 1_000_000}, "claude-sonnet-5") == 0.2
    assert pricing.price(
        {"input": 1000, "output": 100, "cacheRead": 0, "cacheWrite": 0}, "sonnet"
    ) == pytest.approx(0.0045)
    assert pricing.price({}, "haiku") == 0.0
