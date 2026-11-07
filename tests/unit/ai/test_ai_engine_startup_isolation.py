"""
Unit tests for issue #905: a Marcus server start never calls the model provider.

``MarcusServer.initialize()`` calls ``AIAnalysisEngine.initialize()`` before
anything else.
Before this fix the engine built an Anthropic client whenever any non-empty
key was present, ignoring ``ai.enabled``, and ``initialize()`` then sent a real
``messages.create`` request as a connectivity probe.
A placeholder key was therefore the worst case, not a safe one.

These tests pin the contract the fix introduces:

- ``ai.enabled: false`` builds no client, whatever key is present.
- ``initialize()`` never calls the provider, enabled or not.
- ``initialize()`` announces the resolved state in one log line a live check
  can read before trusting anything else.
"""

import logging
from typing import Iterator, Optional
from unittest.mock import Mock, patch

import pytest

from src.config.marcus_config import AISettings, MarcusConfig
from src.integrations.ai_analysis_engine import AIAnalysisEngine

pytestmark = pytest.mark.unit

ENGINE_MODULE = "src.integrations.ai_analysis_engine"
GET_CONFIG = "src.config.marcus_config.get_config"
ISOLATED_CONFIG = "/isolated/config_marcus.json"


def _config(
    *,
    enabled: bool,
    key: Optional[str],
    source: Optional[str] = ISOLATED_CONFIG,
) -> MarcusConfig:
    """
    Build a real ``MarcusConfig`` the way ``from_file`` would.

    Parameters
    ----------
    enabled : bool
        Value of ``ai.enabled``.
    key : Optional[str]
        Value of ``ai.anthropic_api_key``.
    source : Optional[str]
        The file the config was loaded from, or ``None`` for built-in defaults.

    Returns
    -------
    MarcusConfig
        A config with only the AI section and the source path set.
    """
    cfg = MarcusConfig(
        ai=AISettings(enabled=enabled, anthropic_api_key=key, model="claude-test-model")
    )
    cfg.source_path = source
    return cfg


def _ai_engine_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the engine's startup announcement lines captured by caplog."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == ENGINE_MODULE and record.getMessage().startswith("AI engine")
    ]


@pytest.fixture
def anthropic_module() -> Iterator[Mock]:
    """Replace the anthropic SDK module the engine imports with a mock."""
    with patch(f"{ENGINE_MODULE}.anthropic") as module:
        module.Anthropic.return_value = Mock(name="anthropic_client")
        yield module


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable the engine could fall back to."""
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


class TestDisabledAIBuildsNoClient:
    """``ai.enabled: false`` means no client, no matter which key exists."""

    def test_disabled_with_placeholder_key_builds_no_client(
        self, anthropic_module: Mock, no_env_key: None
    ) -> None:
        """A non-empty placeholder key must not produce a client when AI is off."""
        with patch(
            GET_CONFIG, return_value=_config(enabled=False, key="sk-placeholder")
        ):
            engine = AIAnalysisEngine()

        assert engine.client is None
        anthropic_module.Anthropic.assert_not_called()

    def test_disabled_ignores_the_environment_key_fallback(
        self, anthropic_module: Mock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLAUDE_API_KEY fallback must not override a disabled flag."""
        monkeypatch.setenv("CLAUDE_API_KEY", "sk-from-env")
        with patch(GET_CONFIG, return_value=_config(enabled=False, key=None)):
            engine = AIAnalysisEngine()

        assert engine.client is None
        anthropic_module.Anthropic.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_initialize_makes_no_provider_call(
        self, anthropic_module: Mock, no_env_key: None
    ) -> None:
        """Startup with AI disabled constructs nothing and sends nothing."""
        with patch(
            GET_CONFIG, return_value=_config(enabled=False, key="sk-placeholder")
        ):
            engine = AIAnalysisEngine()
            await engine.initialize()

        assert engine.client is None
        anthropic_module.Anthropic.assert_not_called()


class TestInitializeNeverProbesTheProvider:
    """``initialize()`` is a local operation even when AI is enabled."""

    @pytest.mark.asyncio
    async def test_enabled_initialize_sends_no_connectivity_probe(
        self, anthropic_module: Mock, no_env_key: None
    ) -> None:
        """No ``messages.create`` happens at startup; the first use is the first call."""
        client = anthropic_module.Anthropic.return_value
        with patch(
            GET_CONFIG, return_value=_config(enabled=True, key="sk-placeholder")
        ):
            engine = AIAnalysisEngine()
            await engine.initialize()

        client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_initialize_keeps_the_client(
        self, anthropic_module: Mock, no_env_key: None
    ) -> None:
        """Startup never disables a client; only real calls can learn the key is bad."""
        client = anthropic_module.Anthropic.return_value
        with patch(
            GET_CONFIG, return_value=_config(enabled=True, key="sk-placeholder")
        ):
            engine = AIAnalysisEngine()
            await engine.initialize()

        assert engine.client is client


class TestInitializeAnnouncesTheResolvedState:
    """One log line states config path, key presence, flag and model before any call."""

    @pytest.mark.asyncio
    async def test_disabled_state_is_announced(
        self, anthropic_module: Mock, no_env_key: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A disabled engine says so, names its config file, and promises no call."""
        caplog.set_level(logging.INFO, logger=ENGINE_MODULE)
        with patch(
            GET_CONFIG, return_value=_config(enabled=False, key="sk-placeholder")
        ):
            engine = AIAnalysisEngine()
            await engine.initialize()

        [line] = _ai_engine_lines(caplog)
        assert "enabled=False" in line
        assert "key_present=True" in line
        assert ISOLATED_CONFIG in line
        assert "no provider call" in line

    @pytest.mark.asyncio
    async def test_enabled_state_announces_the_deferred_first_call(
        self, anthropic_module: Mock, no_env_key: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An enabled engine names the model and says the first call waits for first use."""
        caplog.set_level(logging.INFO, logger=ENGINE_MODULE)
        with patch(
            GET_CONFIG, return_value=_config(enabled=True, key="sk-placeholder")
        ):
            engine = AIAnalysisEngine()
            await engine.initialize()

        [line] = _ai_engine_lines(caplog)
        assert "enabled=True" in line
        assert "key_present=True" in line
        assert "model=claude-test-model" in line
        assert "first use" in line

    @pytest.mark.asyncio
    async def test_enabled_without_a_key_announces_fallback_mode(
        self, anthropic_module: Mock, no_env_key: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Enabled with no key anywhere is fallback mode, stated as such."""
        caplog.set_level(logging.INFO, logger=ENGINE_MODULE)
        with patch(GET_CONFIG, return_value=_config(enabled=True, key=None)):
            engine = AIAnalysisEngine()
            await engine.initialize()

        assert engine.client is None
        [line] = _ai_engine_lines(caplog)
        assert "enabled=True" in line
        assert "key_present=False" in line
        assert "fallback" in line

    @pytest.mark.asyncio
    async def test_builtin_defaults_are_named_when_no_file_was_loaded(
        self, anthropic_module: Mock, no_env_key: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When no config file existed the line says so instead of printing a path."""
        caplog.set_level(logging.INFO, logger=ENGINE_MODULE)
        with patch(
            GET_CONFIG, return_value=_config(enabled=False, key=None, source=None)
        ):
            engine = AIAnalysisEngine()
            await engine.initialize()

        [line] = _ai_engine_lines(caplog)
        assert "config=built-in defaults" in line
