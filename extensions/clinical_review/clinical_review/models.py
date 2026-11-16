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

#: Sampling temperature, sent explicitly on every call.
#:
#: The 2026-11-14 run sent none, so every call used whatever each provider
#: happened to default to. The paper this replicates states it applied no
#: sampling overrides, so that was arguably faithful, but "the provider's
#: default" is not a setting: it is unrecorded, it differs between providers,
#: and it can change under a finished study. 0.0 is sent instead so a run is
#: reproducible and the value appears in its record.
DEFAULT_TEMPERATURE = 0.0

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
    truncated : bool
        The provider stopped this reply at the output cap rather than because
        the model finished. A truncated reply is not a quiet "no": the 2026-11-15
        runs capped the checker at 600 tokens while asking it to think at high
        effort, reasoning tokens count toward that cap on this provider, and
        every unparseable check in those runs sat at exactly 600 output tokens.
        Scoring those as misses understated every arm that used that model.
    """

    text: str
    tokens: dict[str, int]
    model: str
    raw: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


class ChatModel(Protocol):
    """Anything that completes a conversation."""

    model_id: str

    def complete(
        self, messages: Messages, *, max_tokens: int, json_object: bool = False
    ) -> Reply:
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
    temperature : float
        Sent on every call so the run records it (see
        :data:`DEFAULT_TEMPERATURE`).
    """

    def __init__(
        self,
        model_id: str = DEEPSEEK_V4_FLASH,
        api_key: str | None = None,
        reasoning_effort: str = "high",
        base_url: str = DEEPINFRA_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        from openai import OpenAI

        key = api_key or os.environ.get("DEEPINFRA_API_KEY") or ""
        if not key:
            raise RuntimeError("DEEPINFRA_API_KEY is not set in this process")
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.base_url = base_url
        self._client = OpenAI(api_key=key, base_url=base_url)

    def complete(
        self, messages: Messages, *, max_tokens: int, json_object: bool = False
    ) -> Reply:
        """
        Call the chat completions endpoint once.

        Parameters
        ----------
        messages : Sequence[Dict[str, str]]
            The conversation.
        max_tokens : int
            Output cap, which on this provider includes reasoning tokens.
        json_object : bool
            Ask the provider to constrain the reply to a JSON object. Used for
            the checkers and the judge, never for the adviser, which has to
            write prose clinical advice.
        """
        extra: dict[str, Any] = {"reasoning_effort": self.reasoning_effort}
        if json_object:
            extra["response_format"] = {"type": "json_object"}
        # The SDK types messages as a union of TypedDicts; the plain dicts this
        # extension builds are correct on the wire, so the cast is at the boundary.
        response = self._client.chat.completions.create(
            model=self.model_id,
            messages=cast(Any, list(messages)),
            max_tokens=max_tokens,
            temperature=self.temperature,
            extra_body=extra,
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
                "json_object_requested": json_object,
            },
            truncated=choice.finish_reason == "length",
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
    temperature : float
        Sent on every call so the run records it.
    """

    def __init__(
        self,
        model_id: str = HAIKU_4_5,
        api_key: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
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
        self.temperature = temperature
        self._client = Anthropic(api_key=key)

    def complete(
        self, messages: Messages, *, max_tokens: int, json_object: bool = False
    ) -> Reply:
        """
        Call the messages endpoint once; the system turn becomes ``system``.

        ``json_object`` is accepted for one interface across providers and is
        not sent: this API has no such mode, and this model returned a
        parseable object on every one of the 300 checks it ran on 2026-11-15.
        """
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
            temperature=self.temperature,
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
            raw={
                "stop_reason": response.stop_reason,
                "json_object_requested": json_object,
            },
            truncated=response.stop_reason == "max_tokens",
        )


class FakeChat:
    """
    A scripted model for the rehearsal and the tests; it costs nothing.

    Parameters
    ----------
    script : Callable[[Sequence[Dict[str, str]]], Union[str, Reply]]
        Maps the conversation to the reply text, or to a whole Reply when a
        test needs to set its token counts or its truncation flag.
    model_id : str
        The id written on usage rows; priced at zero.
    tokens_per_call : int
        Token counts to report, so the ledger has rows to add up.
    """

    def __init__(
        self,
        script: Callable[[Messages], str | Reply],
        model_id: str = "fake-model",
        tokens_per_call: int = 0,
    ) -> None:
        self.script = script
        self.model_id = model_id
        self.tokens_per_call = tokens_per_call
        self.calls: list[list[dict[str, str]]] = []
        self.json_object_calls: list[bool] = []

    def complete(
        self, messages: Messages, *, max_tokens: int, json_object: bool = False
    ) -> Reply:
        """Run the script; a script may return a Reply to set its own flags."""
        self.calls.append([dict(m) for m in messages])
        self.json_object_calls.append(json_object)
        produced = self.script(messages)
        if isinstance(produced, Reply):
            return produced
        return Reply(
            text=produced,
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
        json_object: bool = False,
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
        json_object : bool
            Passed through; the providers that support it constrain the reply.

        Returns
        -------
        Reply
        """
        self.ledger.check()
        reply = self.model.complete(
            messages, max_tokens=max_tokens, json_object=json_object
        )
        row = dict(extra or {})
        if reply.truncated:
            # On the record, so a later reader can see the rate without
            # re-reading every reply.
            row["truncated"] = True
        self.ledger.record(self.agent, reply.tokens, reply.model, row)
        return reply
