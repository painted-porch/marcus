"""Unit tests for marcus_runner.seed."""

import pytest
from marcus_runner import seed

pytestmark = pytest.mark.unit

GOOD = [
    {
        "name": "Hold the north ford",
        "description": "d",
        "labels": ["war", "seeded"],
        "completion_criteria": ["c"],
        "estimated_hours": 0.2,
        "priority": "high",
    },
    {
        "name": "Cross the river",
        "description": "d",
        "labels": ["war"],
        "depends_on": ["Hold the north ford"],
        "priority": "medium",
    },
]


class FakeClient:
    """Records calls and answers like the server."""

    def __init__(self, project_id="p1", total=3):
        self.calls = []
        self.project_id = project_id
        self.total = total

    def call(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {
            "success": True,
            "project_id": self.project_id,
            "actual_decomposer": "explicit",
        }

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"project": {"total_tasks": self.total}}


def test_validate_accepts_a_good_list():
    """Names, labels, criteria and a dependency by name."""
    seed.validate_specs(GOOD)


@pytest.mark.parametrize(
    "bad, message",
    [
        ([{"name": "a", "labels": ["build"]}], "software validation gate"),
        ([{"name": "a", "acceptance_criteria": ["x"]}], "unknown keys"),
        ([{"name": "a", "depends_on": ["b"]}], "names no task"),
        ([{"name": "a"}, {"name": "a"}], "distinct"),
        ([{"name": "a", "priority": "urgent"}], "priority"),
        ([{"name": "a", "completion_criteria": "one string"}], "list of sentences"),
    ],
)
def test_validate_refuses_what_the_board_mishandles(bad, message):
    """Each trap the Minecraft runs found is refused before the call."""
    with pytest.raises(ValueError, match=message):
        seed.validate_specs(bad)


def test_seed_project_sends_the_direct_path_and_reads_back(monkeypatch, tmp_path):
    """options.path is 'direct', the tasks go as given, the board is read back."""
    monkeypatch.setattr(seed.time, "sleep", lambda s: None)
    client = FakeClient(total=3)
    result = seed.seed_project(
        client,
        name="war-blue",
        description="blue's board",
        tasks=GOOD,
        objective="Hold the line",
        project_root=str(tmp_path),
    )
    name, args = client.calls[0]
    assert name == "create_project"
    assert args["project_name"] == "war-blue"
    assert args["options"]["path"] == "direct"
    assert args["options"]["provider"] == "sqlite"
    assert args["options"]["tasks"] == GOOD
    assert args["options"]["objective"] == "Hold the line"
    assert result.project_id == "p1"
    assert result.total_tasks == 3
    assert result.task_names == ["Hold the north ford", "Cross the river"]


def test_seed_project_refuses_test_in_the_name_and_a_missing_project_id(monkeypatch):
    """'test' in a name changes Marcus's behaviour; no id means nothing seeded."""
    with pytest.raises(ValueError, match="test"):
        seed.seed_project(FakeClient(), name="war-test", description="d", tasks=GOOD)
    monkeypatch.setattr(seed.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="no project id"):
        seed.seed_project(
            FakeClient(project_id=""), name="war-red", description="d", tasks=GOOD
        )
