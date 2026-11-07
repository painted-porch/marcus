"""
Unit tests: ``LLMAbstraction`` honours ``ai.enabled`` (issue #905, Codex P2 on #906).

Every module that wants a model builds ``LLMAbstraction()`` itself: the
decomposer and dependency wiring through ``generate_structured_response``,
the advanced parser, the enricher, the work analyzer, the NLP tools, spec
coverage.
Before this change the abstraction initialised providers from the key
alone, so ``ai.enabled: false`` with a valid-looking key still produced a
real, paid request from any of those paths while the server's startup line
promised there would be none.
One gate here covers all of them.
"""

from unittest.mock import patch

import pytest

from src.ai.providers.llm_abstraction import LLMAbstraction
from src.config.marcus_config import AISettings, MarcusConfig

pytestmark = pytest.mark.unit

GET_CONFIG = "src.config.marcus_config.get_config"
VALID_LOOKING_KEY = "sk-ant-valid-looking-key-0123456789"


def _config(enabled: bool) -> MarcusConfig:
    return MarcusConfig(
        ai=AISettings(
            enabled=enabled, provider="anthropic", anthropic_api_key=VALID_LOOKING_KEY
        )
    )


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment key the providers could fall back to."""
    for name in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class TestDisabledAIInitialisesNoProvider:
    """A valid-looking key is not a licence to call when the flag says no."""

    def test_no_provider_is_constructed_and_the_refusal_names_the_flag(
        self, no_env_key: None
    ) -> None:
        """Initialisation refuses with a RuntimeError, the type callers already handle."""
        with patch(GET_CONFIG, return_value=_config(enabled=False)):
            llm = LLMAbstraction()
            with patch(
                "src.ai.providers.anthropic_provider.AnthropicProvider"
            ) as provider:
                with pytest.raises(RuntimeError, match="ai.enabled"):
                    llm._initialize_providers()
        provider.assert_not_called()
        assert llm.providers == {}

    @pytest.mark.asyncio
    async def test_a_call_through_the_abstraction_is_refused_before_any_provider(
        self, no_env_key: None
    ) -> None:
        """The public path every consumer uses refuses the same way."""
        with patch(GET_CONFIG, return_value=_config(enabled=False)):
            llm = LLMAbstraction()
            with patch(
                "src.ai.providers.anthropic_provider.AnthropicProvider"
            ) as provider:
                with pytest.raises(RuntimeError, match="ai.enabled"):
                    await llm.analyze("anything", {"type": "general"})
        provider.assert_not_called()

    def test_enabled_still_initialises_the_configured_provider(
        self, no_env_key: None
    ) -> None:
        """The gate is the flag, not the key: enabled with a key behaves as before."""
        with patch(GET_CONFIG, return_value=_config(enabled=True)):
            llm = LLMAbstraction()
            with patch(
                "src.ai.providers.anthropic_provider.AnthropicProvider"
            ) as provider:
                llm._initialize_providers()
        provider.assert_called_once()
