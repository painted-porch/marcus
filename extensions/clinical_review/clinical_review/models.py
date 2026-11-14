"""
The model adapters: DeepInfra (OpenAI-compatible), Anthropic, and a fake.

Every adapter takes OpenAI-style messages and returns the reply text with its
token counts. :class:`Metered` wraps any adapter with the run's
:class:`~clinical_review.spend.Ledger`: the stop line is checked before the
call and the usage row is written after it, so no call can escape the record.

DeepSeek-V4-Flash's reasoning trace is never appended to the context for a
later turn, which matches the paper's setup (Supplementary Methods 3); the
adapter drops it and keeps only the visible message.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from .spend import Ledger

DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPSEEK_V4_FLASH = "deepseek-ai/DeepSeek-V4-Flash"
HAIKU_4_5 = "claude-haiku-4-5-20251001"

Messages = Sequence[dict[str, str]]


@dataclass
class Reply:
    """
    One model reply.

    Parameters
    ----------
    text : str
        The visible message text.
    tokens : Dict[str, int]
        ``input`` and ``output`` counts (output includes reasoning tokens).
    model : str
        The model id the provider reports.
    raw : Dict[str, Any]
        Anything else worth keeping (finish reason, reasoning length).
    """

    text: str
    tokens: dict[str, int]
    model: str
    raw: dict[str, Any] = field(default_factory=dict)


class ChatModel(Protocol):
    """Anything that completes a conversation."""

    model_id: str

    def complete(self, messages: Messages, *, max_tokens: int) -> Reply:
        """Complete ``messages`` with at most ``max_tokens`` of output."""


class DeepInfraChat:
    """
    DeepSeek-V4-Flash through DeepInfra's OpenAI-compatible endpoint.

    Parameters
    ----------
    model_id : str
        The DeepInfra model id.
    api_key : Optional[str]
        The key; ``DEEPINFRA_API_KEY`` from the environment when omitted.
    reasoning_effort : str
        ``high`` is the paper's setting; ``none`` disables thinking.
    base_url : str
        The endpoint.
    """

    def __init__(
        self,
        model_id: str = DEEPSEEK_V4_FLASH,
        api_key: str | None = None,
        reasoning_effort: str = "high",
        base_url: str = DEEPINFRA_BASE_URL,
    ) -> None:
        from openai import OpenAI

        key = api_key or os.environ.get("DEEPINFRA_API_KEY") or ""
        if not key:
            raise RuntimeError("DEEPINFRA_API_KEY is not set in this process")
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self._client = OpenAI(api_key=key, base_url=base_url)

    def complete(self, messages: Messages, *, max_tokens: int) -> Reply:
        """Call the chat completions endpoint once."""
        # The SDK types messages as a union of TypedDicts; the plain dicts this
        # extension builds are correct on the wire, so the cast is at the boundary.
        response = self._client.chat.completions.create(
            model=self.model_id,
            messages=cast(Any, list(messages)),
            max_tokens=max_tokens,
            extra_body={"reasoning_effort": self.reasoning_effort},
        )
        choice = response.choices[0]
        message = choice.message
        text = message.content or ""
        reasoning = getattr(message, "reasoning_content", None) or getattr(
            message, "reasoning", None
        )
        usage = response.usage
        tokens = {
            "input": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output": int(getattr(usage, "completion_tokens", 0) or 0),
        }
        return Reply(
            text=text,
            tokens=tokens,
            model=str(response.model or self.model_id),
            raw={
                "finish_reason": choice.finish_reason,
                "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            },
        )


class AnthropicChat:
    """
    A Claude model through the Anthropic SDK.

    Parameters
    ----------
    model_id : str
        The model id.
    api_key : Optional[str]
        The key; ``ANTHROPIC_API_KEY`` or ``CLAUDE_API_KEY`` when omitted.
    """

    def __init__(self, model_id: str = HAIKU_4_5, api_key: str | None = None) -> None:
        from anthropic import Anthropic

        key = (
            api_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_API_KEY")
            or ""
        )
        if not key:
            raise RuntimeError("no Anthropic key is set in this process")
        self.model_id = model_id
        self._client = Anthropic(api_key=key)

    def complete(self, messages: Messages, *, max_tokens: int) -> Reply:
        """Call the messages endpoint once; the system turn becomes ``system``."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        response = self._client.messages.create(
            model=self.model_id,
            max_tokens=max_tokens,
            system=system or "You are a careful assistant.",
            messages=cast(Any, turns),
        )
        text = "".join(getattr(block, "text", "") for block in response.content)
        return Reply(
            text=text,
            tokens={
                "input": int(response.usage.input_tokens),
                "output": int(response.usage.output_tokens),
            },
            model=str(response.model),
            raw={"stop_reason": response.stop_reason},
        )


class FakeChat:
    """
    A scripted model for the rehearsal and the tests; it costs nothing.

    Parameters
    ----------
    script : Callable[[Sequence[Dict[str, str]]], str]
        Maps the conversation to the reply text.
    model_id : str
        The id written on usage rows; priced at zero.
    tokens_per_call : int
        Token counts to report, so the ledger has rows to add up.
    """

    def __init__(
        self,
        script: Callable[[Messages], str],
        model_id: str = "fake-model",
        tokens_per_call: int = 0,
    ) -> None:
        self.script = script
        self.model_id = model_id
        self.tokens_per_call = tokens_per_call
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: Messages, *, max_tokens: int) -> Reply:
        """Run the script."""
        self.calls.append([dict(m) for m in messages])
        return Reply(
            text=self.script(messages),
            tokens={"input": self.tokens_per_call, "output": self.tokens_per_call},
            model=self.model_id,
        )


class Metered:
    """
    A model behind the ledger: stop line before, usage row after.

    Parameters
    ----------
    model : ChatModel
        The adapter.
    ledger : Ledger
        The run's ledger.
    agent : str
        The row's agent name.
    """

    def __init__(self, model: ChatModel, ledger: Ledger, agent: str) -> None:
        self.model = model
        self.ledger = ledger
        self.agent = agent
        self.model_id = model.model_id

    def complete(
        self,
        messages: Messages,
        *,
        max_tokens: int,
        extra: dict[str, Any] | None = None,
    ) -> Reply:
        """
        Check the line, call, record.

        Parameters
        ----------
        messages : Sequence[Dict[str, str]]
            The conversation.
        max_tokens : int
            Output cap.
        extra : Optional[Dict[str, Any]]
            Fields written beside the usage row.

        Returns
        -------
        Reply
        """
        self.ledger.check()
        reply = self.model.complete(messages, max_tokens=max_tokens)
        self.ledger.record(self.agent, reply.tokens, reply.model, extra)
        return reply
