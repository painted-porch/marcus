"""
LLM Abstraction Layer for Marcus AI.

Provides a unified interface across different LLM providers
(Anthropic, OpenAI, local models)
with intelligent fallback and provider switching capabilities.

This module implements the strategy pattern for LLM providers, allowing seamless
switching between providers and automatic fallback on failures.

Classes
-------
LLMAbstraction
    Multi-provider LLM abstraction with intelligent fallback

Notes
-----
Provider selection is controlled by the MARCUS_LLM_PROVIDER environment variable.
The system automatically falls back to alternative providers on failure.

Examples
--------
>>> llm = LLMAbstraction()
>>> analysis = await llm.analyze_task_semantics(task, context)
>>> if analysis.fallback_used:
...     print("Using fallback provider")
"""

import asyncio
import functools
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from src.core.models import Priority, Task, TaskStatus

from .base_provider import (
    BaseLLMProvider,
    EffortEstimate,
    SemanticAnalysis,
    SemanticDependency,
)

logger = logging.getLogger(__name__)


_T = TypeVar("_T")


def _tagged_operation(
    operation: str,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Wrap an LLMAbstraction method in ``recorder.operation_context``.

    Kaia review on PR #517: the five high-level methods on
    :class:`LLMAbstraction` (``analyze_task_semantics``,
    ``infer_dependencies_semantic``, ``generate_enhanced_description``,
    ``estimate_effort_intelligently``,
    ``analyze_blocker_and_suggest_solutions``) all needed the same
    four-line preamble that pushed an ``operation_context`` for the
    duration of the call. This decorator consolidates the boilerplate.

    Parameters
    ----------
    operation : str
        Operation key from :mod:`src.cost_tracking.operations` to
        stamp onto ``token_events.operation`` for calls made inside
        the wrapped method.

    Returns
    -------
    Callable
        A method decorator that wraps the original coroutine in the
        appropriate ``operation_context``.

    Notes
    -----
    The recorder import is performed lazily inside the wrapper so
    that ``llm_abstraction.py`` can be imported without forcing the
    cost-tracking module load — keeping startup paths lean.
    """

    def decorator(fn: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        @functools.wraps(fn)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> _T:
            from src.cost_tracking.cost_recorder import get_recorder

            with get_recorder().operation_context(operation):
                return await fn(self, *args, **kwargs)

        return wrapper

    return decorator


class LLMAbstraction:
    """

    Multi-provider LLM abstraction with intelligent fallback.

    Supports multiple LLM providers with automatic fallback when primary fails.
    Provides a unified interface for all AI operations in Marcus.

    Attributes
    ----------
    providers : dict
        Available LLM provider instances
    current_provider : str
        Name of the primary provider to use
    fallback_providers : list of str
        Ordered list of providers to try on failure
    provider_stats : dict
        Performance statistics for each provider

    Methods
    -------
    analyze_task_semantics(task, context)
        Analyze task meaning and intent
    infer_dependencies_semantic(tasks)
        Infer logical dependencies between tasks
    generate_enhanced_description(task, context)
        Create improved task descriptions
    estimate_effort_intelligently(task, context)
        AI-powered effort estimation
    analyze_blocker_and_suggest_solutions(task, blocker, severity, agent)
        Analyze blockers and provide solutions

    Notes
    -----
    Providers are initialized lazily to avoid circular imports.
    Statistics are tracked for intelligent provider selection.
    """

    def __init__(self) -> None:
        self.providers: Dict[str, BaseLLMProvider] = {}

        # Get provider from config first, then env var as override
        from src.config.marcus_config import get_config

        config = get_config()
        self.current_provider = config.ai.provider or "anthropic"

        # Build fallback list based on available providers
        self.fallback_providers: List[str] = []

        # Initialize providers (deferred to avoid circular imports)
        self.providers = {}
        self._providers_initialized = False

        # Performance tracking initialized after providers load
        self.provider_stats: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"LLM abstraction initialized with primary provider: "
            f"{self.current_provider}"
        )

    def _initialize_providers(self) -> None:
        """
        Initialize available LLM providers.

        Uses lazy loading to avoid circular imports. Providers are only
        initialized when first needed.

        Notes
        -----
        Failed provider initialization is logged but doesn't stop the system.
        At least one provider must initialize successfully.
        """
        if self._providers_initialized:
            return

        logger.debug("Starting provider initialization...")

        # Load config to get API keys directly
        try:
            from src.config.marcus_config import get_config

            config = get_config()
        except Exception as e:
            logger.warning(f"Failed to load config, falling back to env vars: {e}")
            # Create minimal config with defaults
            from src.config.marcus_config import MarcusConfig

            config = MarcusConfig()

        # Provider lockdown (Marcus #531). When the user explicitly sets
        # ``config.ai.provider``, ONLY that provider initializes. Other
        # providers never enter ``self.providers`` and never become
        # fallback candidates — even if their credentials happen to be
        # present in config or in the environment.
        #
        # The earlier code gated only the ENV-VAR fallback by
        # ``configured_provider``, but left the init block gated only
        # on key validity. Config substitution (``"openai_api_key":
        # "${OPENAI_API_KEY}"`` in config_marcus.json) put a real OpenAI
        # key into ``config.ai.openai_api_key`` whenever the env var was
        # exported in the user's shell — so OpenAI silently joined the
        # fallback chain even when ``provider: anthropic`` was set, and
        # cascaded billing to OpenAI when Anthropic momentarily failed.
        # Issue #905 (Codex P2 on PR #906): ``ai.enabled: false`` is a
        # promise that nothing calls a provider, and this class is how every
        # consumer that wants a model reaches one: the decomposer and
        # dependency wiring through ``generate_structured_response``, the
        # advanced parser, the enricher, the work analyzer, the NLP tools,
        # spec coverage. Gate here, once, so a valid-looking key alone is
        # never a licence to call. RuntimeError is the type every caller
        # already handles for "no provider could be initialised". The
        # initialised flag stays False so each later call refuses with this
        # same clear line rather than a misleading "all providers failed".
        if not bool(getattr(config.ai, "enabled", True)):
            logger.info(
                "LLM abstraction: ai.enabled is false; no provider initialised "
                "and no provider call will be made"
            )
            raise RuntimeError(
                "AI is disabled by configuration (ai.enabled=false): no LLM "
                "provider is initialised and no provider call is made"
            )

        configured_provider = config.ai.provider or ""

        def _allowed(name: str) -> bool:
            """Return True iff ``name`` may initialize under the current config.

            When ``configured_provider`` is set, only the matching name
            is allowed. When it's empty (legacy auto-discovery), every
            provider with valid credentials is allowed.
            """
            return not configured_provider or configured_provider == name

        # ----- Anthropic -----------------------------------------------------
        if _allowed("anthropic"):
            anthropic_key = config.ai.anthropic_api_key or ""
            if not anthropic_key:
                anthropic_key = os.getenv("CLAUDE_API_KEY", "").strip()

            if (
                anthropic_key
                and anthropic_key.startswith("sk-ant-")
                and len(anthropic_key) > 10
                and anthropic_key != "sk-ant-your-api-key-here"
            ):
                try:
                    from .anthropic_provider import AnthropicProvider

                    # Pass key directly to the provider — never write into
                    # os.environ. ANTHROPIC_API_KEY in the env would force
                    # Claude Code subprocesses (Epictetus, project creator,
                    # workers, monitor) to bill the API instead of using the
                    # user's Claude Code subscription.
                    self.providers["anthropic"] = AnthropicProvider(
                        api_key=anthropic_key
                    )
                    self.fallback_providers.append("anthropic")
                    logger.info("Successfully initialized Anthropic provider")
                except Exception as e:
                    logger.warning(f"Failed to initialize Anthropic provider: {e}")
            else:
                logger.debug(
                    f"Skipping Anthropic provider - no valid API key configured "
                    f"(key present: {bool(anthropic_key)})"
                )

        # ----- OpenAI --------------------------------------------------------
        if _allowed("openai"):
            openai_key = config.ai.openai_api_key or ""
            if not openai_key:
                openai_key = os.getenv("OPENAI_API_KEY", "").strip()

            if (
                openai_key
                and openai_key.startswith("sk-")
                and len(openai_key) > 10
                and openai_key != "sk-your-openai-key-here"
            ):
                try:
                    from .openai_provider import OpenAIProvider

                    # Temporarily set env var for the provider
                    os.environ["OPENAI_API_KEY"] = openai_key
                    self.providers["openai"] = OpenAIProvider()
                    self.fallback_providers.append("openai")
                    logger.info("Successfully initialized OpenAI provider")
                except Exception as e:
                    logger.warning(f"Failed to initialize OpenAI provider: {e}")
            else:
                logger.debug(
                    f"Skipping OpenAI provider - no valid API key configured "
                    f"(key present: {bool(openai_key)})"
                )
        elif config.ai.openai_api_key or os.getenv("OPENAI_API_KEY", "").strip():
            # Diagnostic only: user has an OpenAI key available somewhere
            # but `config.ai.provider` excludes openai. Tell them loudly
            # so they know we deliberately ignored the key.
            logger.info(
                "OpenAI key present but provider=%r — OpenAI deliberately "
                "NOT initialized. To use OpenAI, set ai.provider='openai' "
                "in config_marcus.json.",
                configured_provider,
            )

        # ----- Cloud ---------------------------------------------------------
        # Cloud was already gated correctly (only inits when explicitly
        # configured), so the existing check stays. Comment kept for
        # consistency with the rewritten anthropic/openai blocks above.
        if _allowed("cloud") and configured_provider == "cloud":
            cloud_key = config.ai.cloud_api_key or ""
            if not cloud_key:
                cloud_key = os.getenv("MARCUS_CLOUD_LLM_KEY", "").strip()

            cloud_url = config.ai.cloud_url or ""
            if not cloud_url:
                cloud_url = os.getenv("MARCUS_CLOUD_LLM_URL", "").strip()

            cloud_model = config.ai.model or ""

            if cloud_key and cloud_url and cloud_model:
                try:
                    from .cloud_provider import CloudLLMProvider

                    self.providers["cloud"] = CloudLLMProvider(
                        model=cloud_model,
                        api_key=cloud_key,
                        url=cloud_url,
                    )
                    self.fallback_providers.append("cloud")
                    logger.info(
                        "Successfully initialized cloud LLM provider: "
                        "model=%s url=%s",
                        cloud_model,
                        cloud_url,
                    )
                except Exception as e:
                    logger.warning("Failed to initialize cloud LLM provider: %s", e)
            else:
                logger.debug(
                    "Skipping cloud provider — missing key=%s url=%s model=%s",
                    bool(cloud_key),
                    bool(cloud_url),
                    bool(cloud_model),
                )

        # ----- Local ---------------------------------------------------------
        if _allowed("local"):
            local_model_path = config.ai.local_model or ""
            if not local_model_path:
                local_model_path = os.getenv("MARCUS_LOCAL_LLM_PATH", "").strip()

            if local_model_path:
                try:
                    from .local_provider import LocalLLMProvider

                    self.providers["local"] = LocalLLMProvider(local_model_path)
                    self.fallback_providers.append("local")
                    logger.info(
                        f"Successfully initialized local LLM provider "
                        f"with model: {local_model_path}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to initialize local LLM provider: {e}")

        # Hard-fail when the user explicitly set a provider and it didn't
        # initialize. The earlier code logged a warning and silently
        # cascaded to whichever provider happened to be available — that
        # caused real cost rows to land under the "wrong" provider after a
        # silent fallback. Surface the gap immediately. (Marcus #531)
        if configured_provider and configured_provider not in self.providers:
            raise RuntimeError(
                f"config.ai.provider={configured_provider!r} is set but the "
                f"provider failed to initialize. Refusing to silently fall "
                f"back to another provider. Check that the corresponding "
                f"credentials are present and valid in config_marcus.json "
                f"or the matching environment variable, then restart Marcus."
            )

        # Initialize provider stats only for successfully loaded providers
        self.provider_stats = {
            provider: {"requests": 0, "failures": 0, "avg_response_time": 0.0}
            for provider in self.providers.keys()
        }

        # Ensure we have at least one provider
        if not self.providers:
            raise RuntimeError(
                "No LLM providers could be initialized. "
                "Please check your AI configuration."
            )

        # Ensure current provider is available, otherwise use first available
        if self.current_provider not in self.providers:
            self.current_provider = list(self.providers.keys())[0]
            logger.warning(
                f"Requested provider not available, using {self.current_provider}"
            )

        self._providers_initialized = True
        logger.info(f"Initialized providers: {list(self.providers.keys())}")

    @_tagged_operation("analyze_task_semantics")
    async def analyze_task_semantics(
        self, task: Task, context: Dict[str, Any]
    ) -> SemanticAnalysis:
        """
        Analyze task semantics using the best available provider.

        Parameters
        ----------
        task : Task
            Task to analyze for semantic meaning
        context : dict
            Project context including related tasks

        Returns
        -------
        SemanticAnalysis
            Comprehensive semantic analysis including intent and risks

        Notes
        -----
        Automatically falls back to alternative providers on failure.
        Tagged ``analyze_task_semantics`` via :func:`_tagged_operation`
        so the resulting ``token_events.operation`` row carries that
        label instead of the provider's default.
        """
        result = await self._execute_with_fallback(
            "analyze_task", task=task, context=context
        )
        return result  # type: ignore

    @_tagged_operation("infer_dependencies")
    async def infer_dependencies_semantic(
        self, tasks: List[Task]
    ) -> List[SemanticDependency]:
        """
        Infer semantic dependencies between tasks.

        Parameters
        ----------
        tasks : list of Task
            All tasks to analyze for dependencies

        Returns
        -------
        list of SemanticDependency
            Inferred logical relationships between tasks

        Notes
        -----
        Complements rule-based dependency detection with semantic
        understanding. Tagged ``infer_dependencies`` via
        :func:`_tagged_operation`.
        """
        result = await self._execute_with_fallback("infer_dependencies", tasks=tasks)
        return result  # type: ignore

    @_tagged_operation("enrich_task")
    async def generate_enhanced_description(
        self, task: Task, context: Dict[str, Any]
    ) -> str:
        """
        Generate enhanced task description.

        Parameters
        ----------
        task : Task
            Task needing clearer description
        context : dict
            Project context for better understanding

        Returns
        -------
        str
            Enhanced description with more detail and clarity
        """
        result = await self._execute_with_fallback(
            "generate_enhanced_description", task=task, context=context
        )
        return result  # type: ignore

    @_tagged_operation("estimate_effort")
    async def estimate_effort_intelligently(
        self, task: Task, context: Dict[str, Any]
    ) -> EffortEstimate:
        """
        Estimate task effort using AI.

        Parameters
        ----------
        task : Task
            Task to estimate completion time for
        context : dict
            Project context with historical performance data

        Returns
        -------
        EffortEstimate
            AI-powered time estimate with confidence and factors
        """
        result = await self._execute_with_fallback(
            "estimate_effort", task=task, context=context
        )
        return result  # type: ignore

    @_tagged_operation("analyze_blocker")
    async def analyze_blocker_and_suggest_solutions(
        self,
        task: Task,
        blocker_description: str,
        severity: str,
        agent: Optional[Dict[str, Any]],
    ) -> List[str]:
        """
        Analyze a blocker and suggest solutions.

        Parameters
        ----------
        task : Task
            The blocked task
        blocker_description : str
            Detailed description of the blocker
        severity : str
            Severity level: 'low', 'medium', or 'high'
        agent : dict, optional
            Agent information for context

        Returns
        -------
        list of str
            Prioritized list of solution suggestions

        Notes
        -----
        Higher severity blockers receive more detailed analysis.
        """
        context = {
            "blocker_description": blocker_description,
            "severity": severity,
            "agent": agent,
        }

        result = await self._execute_with_fallback(
            "analyze_blocker",
            task=task,
            blocker=blocker_description,
            context=context,
        )
        return result  # type: ignore

    async def _execute_with_fallback(self, method_name: str, **kwargs: Any) -> Any:
        """
        Execute method with automatic provider fallback.

        Tries the primary provider first, then falls back to alternatives
        in order if the primary fails.

        Parameters
        ----------
        method_name : str
            Name of the provider method to call
        **kwargs
            Arguments to pass to the method

        Returns
        -------
        Any
            Result from the first successful provider

        Raises
        ------
        Exception
            If all providers fail with details of each failure

        Notes
        -----
        Updates provider statistics for intelligent future selection.
        Marks results with fallback_used=True when not using primary.
        """
        # Ensure providers are initialized
        self._initialize_providers()

        providers_to_try = [self.current_provider] + [
            p for p in self.fallback_providers if p != self.current_provider
        ]

        last_exception = None

        for provider_name in providers_to_try:
            if provider_name not in self.providers:
                continue

            provider = self.providers[provider_name]

            try:
                logger.debug(f"Trying {method_name} with provider: {provider_name}")

                # Track request
                self.provider_stats[provider_name]["requests"] += 1

                # Execute method
                method = getattr(provider, method_name)
                result = await method(**kwargs)

                # Mark fallback usage if not primary
                if hasattr(result, "fallback_used"):
                    result.fallback_used = provider_name != self.current_provider

                logger.debug(
                    f"Successfully executed {method_name} with {provider_name}"
                )
                return result

            except Exception as e:
                logger.warning(
                    f"Provider {provider_name} failed for {method_name}: {e}"
                )
                self.provider_stats[provider_name]["failures"] += 1
                last_exception = e
                continue

        # All providers failed
        available_providers = list(self.providers.keys())
        logger.error(
            f"All available providers {available_providers} failed for {method_name}"
        )

        # Provide a more helpful error message
        if not available_providers:
            raise Exception(
                "No AI providers are configured. "
                "Please check your API keys in config_marcus.json. "
                "Make sure keys start with 'sk-ant-' for Anthropic or 'sk-' for OpenAI."
            )
        elif len(available_providers) == 1:
            provider_name = available_providers[0]
            error_msg = f"{provider_name.capitalize()} API error: {last_exception}"
            if "401" in str(last_exception):
                error_msg += (
                    f". Please check that your {provider_name} API key in "
                    f"config_marcus.json is valid and not expired."
                )
            elif "API key" in str(last_exception):
                error_msg = (
                    f"Invalid {provider_name} API key. Please check that your "
                    f"API key in config_marcus.json is correct and starts with "
                    f"'{'sk-ant-' if provider_name == 'anthropic' else 'sk-'}'."
                )
        else:
            error_msg = f"All LLM providers failed. Last error: {last_exception}"

        raise Exception(error_msg)

    async def analyze(
        self, prompt: str, context: Any, *, operation: Optional[str] = None
    ) -> str:
        """
        Analyze content using LLM.

        Parameters
        ----------
        prompt : str
            The prompt to analyze.
        context : Any
            Analysis context (may carry ``max_tokens`` override).
        operation : str, optional
            Logical operation label to attach to the cost event. When
            provided, the recorder's active PlannerContext is shadowed
            with an ``operation_override`` for the duration of the call,
            so ``token_events.operation`` records ``operation`` instead
            of whatever default the provider stamps. Used for per-call
            drill-down in the Cato cost dashboard. See
            ``src/cost_tracking/operations.py`` for the canonical
            taxonomy and human-readable descriptions.

        Returns
        -------
        str
            Analysis result as string.
        """
        # Ensure providers are initialized before trying to use them
        self._initialize_providers()

        # Pass max_tokens through ONLY if the caller explicitly attached it
        # to ``context``.  Otherwise let the provider use its configured
        # default (sourced from ``config.ai.max_tokens``).  Previously this
        # method hardcoded 2000, which silently overrode the user's config
        # — a problem for reasoning-distilled models whose <think> blocks
        # alone exceed 2000 tokens before any structured output appears.
        kwargs: Dict[str, Any] = {"prompt": prompt}
        if hasattr(context, "max_tokens"):
            kwargs["max_tokens"] = context.max_tokens

        if operation:
            # Scope the recorder's active context with operation_override
            # so the resulting token_events row is tagged correctly. Local
            # import avoids cost_tracking import at module load.
            from src.cost_tracking.cost_recorder import get_recorder

            with get_recorder().operation_context(operation):
                result = await self._execute_with_fallback("complete", **kwargs)
        else:
            result = await self._execute_with_fallback("complete", **kwargs)
        return result  # type: ignore

    async def switch_provider(self, provider_name: str) -> bool:
        """
        Switch to a different provider.

        Args
        ----
            provider_name: Name of provider to switch to

        Returns
        -------
            True if switch successful, False otherwise
        """
        if provider_name not in self.providers:
            logger.error(f"Provider {provider_name} not available")
            return False

        old_provider = self.current_provider
        self.current_provider = provider_name

        logger.info(f"Switched LLM provider from {old_provider} to {provider_name}")
        return True

    def get_provider_stats(self) -> Dict[str, Any]:
        """Get performance statistics for all providers."""
        return {
            "current_provider": self.current_provider,
            "available_providers": list(self.providers.keys()),
            "stats": self.provider_stats.copy(),
        }

    def get_best_provider(self) -> str:
        """
        Determine the best performing provider based on success rate.

        Returns
        -------
            Name of best performing provider
        """
        best_provider = self.current_provider
        best_success_rate = 0.0

        for provider, stats in self.provider_stats.items():
            if provider not in self.providers:
                continue

            requests = stats["requests"]
            if requests == 0:
                continue

            success_rate = 1.0 - (stats["failures"] / requests)

            if success_rate > best_success_rate:
                best_success_rate = success_rate
                best_provider = provider

        return str(best_provider)

    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of all providers.

        Returns
        -------
            Health status for each provider
        """
        health_status = {}

        for provider_name, provider in self.providers.items():
            try:
                # Simple test request
                test_task = Task(
                    id="health-check",
                    name="Test task",
                    description="Health check test",
                    status=TaskStatus.TODO,
                    priority=Priority.LOW,
                    assigned_to=None,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    due_date=None,
                    estimated_hours=1.0,
                )

                await asyncio.wait_for(
                    provider.analyze_task(test_task, {"project_type": "test"}),
                    timeout=10.0,
                )

                health_status[provider_name] = {
                    "status": "healthy",
                    "response_time": "< 10s",
                    "last_check": "now",
                }

            except asyncio.TimeoutError:
                health_status[provider_name] = {
                    "status": "timeout",
                    "error": "Request timed out after 10s",
                }
            except Exception as e:
                health_status[provider_name] = {"status": "error", "error": str(e)}

        return health_status
