"""
Unit tests for the hybrid inferer surviving untidy model replies.

``_get_ai_dependencies`` parsed the model's reply with ``json.loads``. A
reply with a code fence, a leading sentence, or a trailing sentence
raised, the batch was discarded (``AI inferences: 0``), and the call's
cost was wasted on every poll. It now goes through the shared parser.
"""

import json
from datetime import datetime, timezone
from typing import List
from unittest.mock import AsyncMock, Mock

import pytest

from src.config.hybrid_inference_config import HybridInferenceConfig
from src.core.models import Priority, Task, TaskStatus
from src.intelligence.dependency_inferer_hybrid import HybridDependencyInferer

pytestmark = pytest.mark.unit


def _task(task_id: str, name: str, labels: List[str]) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        name=name,
        description="",
        status=TaskStatus.TODO,
        priority=Priority.HIGH,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
        estimated_hours=4.0,
        labels=labels,
        dependencies=[],
    )


def _tasks() -> List[Task]:
    return [
        _task("1", "Design User Authentication", ["design", "auth"]),
        _task("2", "Implement User Login", ["backend", "auth"]),
    ]


AI_REPLY = [
    {
        "task1_id": "2",
        "task2_id": "1",
        "dependency_direction": "2->1",
        "confidence": 0.85,
        "reasoning": "Login needs the auth design",
        "dependency_type": "hard",
    }
]


def _inferer(reply: str) -> HybridDependencyInferer:
    engine = Mock()
    engine._call_claude = AsyncMock(return_value=reply)
    config = HybridInferenceConfig(
        pattern_confidence_threshold=0.99, enable_ai_inference=True
    )
    return HybridDependencyInferer(engine, config)


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps(AI_REPLY),
        "```json\n" + json.dumps(AI_REPLY, indent=2) + "\n```",
        json.dumps(AI_REPLY, indent=2)
        + "\n\nThese reflect the usual design-before-implementation order.",
        "Here is the JSON response:\n" + json.dumps(AI_REPLY),
    ],
    ids=["bare", "fenced", "trailing-prose", "leading-prose"],
)
@pytest.mark.asyncio
async def test_untidy_replies_still_yield_the_dependency(reply: str) -> None:
    """Every common reply shape produces the AI-inferred edge."""
    graph = await _inferer(reply).infer_dependencies(_tasks())

    assert "2" in graph.adjacency_list.get("1", []), graph.adjacency_list


@pytest.mark.asyncio
async def test_non_list_reply_is_rejected_not_crashed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An object where a list was asked for yields no AI edges and a log line."""
    graph = await _inferer(json.dumps({"unexpected": "shape"})).infer_dependencies(
        _tasks()
    )

    assert graph.adjacency_list.get("1", []) == []
    assert any("AI dependency inference" in r.getMessage() for r in caplog.records)
