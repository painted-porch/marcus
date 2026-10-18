"""
Unit tests for spec coverage check in task decomposition.

Dashboard-v88 post-mortem: Marcus's task decomposer silently dropped the
"5-day forecast" feature from the project spec. No task was generated for
it, so no agent knew it was in scope. UD1 even wrote .forecast-card CSS
as a hint, but with no task, no agent built the component.

Fix: after task decomposition, run a spec coverage check. Extract feature
phrases from the description, verify each has at least one task covering it,
and synthesize gap tasks for any uncovered features before agents start.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus
from src.integrations.spec_coverage import (
    check_spec_coverage,
    extract_spec_features,
    find_uncovered_features,
)

pytestmark = pytest.mark.unit


def _make_task(
    name: str, description: str = "", labels: list[str] | None = None
) -> Task:
    """Build a minimal Task for testing."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Task(
        id=f"task_{name.lower().replace(' ', '_')[:20]}",
        name=name,
        description=description,
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        labels=labels or [],
        dependencies=[],
        estimated_hours=2.0,
        assigned_to=None,
        created_at=now,
        updated_at=now,
        due_date=None,
    )


NIMBUS_SPEC = """
Build a weather dashboard named Nimbus with:
- Animated digital clock with live seconds using flip animation
- Weather panel showing current temperature, feels like, humidity, wind speed, UV index
- 5-day weather forecast with icons from lucide-react
- Dark glassmorphism theme with gradient card backgrounds
- Smooth fade-in animations on page load
- Hardcoded realistic NYC weather data (no external API calls)
"""

NIMBUS_TASKS_MISSING_FORECAST = [
    _make_task("Tech Foundation Setup", "Set up Vite + React + Tailwind"),
    _make_task("Design System Setup", "glassmorphism theme, CSS tokens"),
    _make_task("Shared Component Foundation", "Card component with animations"),
    _make_task("Design Real-Time Clock", "design the AnimatedClock component"),
    _make_task("Implement Animated Digital Clock", "flip animation, live seconds"),
    _make_task("Implement WeatherPanel", "current conditions, 5 metrics"),
    _make_task("Integration Verification", "smoke test all components"),
    _make_task("README Documentation", "usage docs"),
]

NIMBUS_TASKS_COMPLETE = NIMBUS_TASKS_MISSING_FORECAST + [
    _make_task("Implement 5-Day Forecast", "forecast cards with lucide icons"),
]


class TestExtractSpecFeatures:
    """extract_spec_features pulls feature phrases from a project description."""

    @pytest.mark.asyncio
    async def test_returns_list_of_features(self) -> None:
        """LLM returns JSON list of features."""
        mock_llm_response = (
            '["animated digital clock", "weather panel", '
            '"5-day forecast", "glassmorphism theme", "fade-in animations"]'
        )
        with patch(
            "src.integrations.spec_coverage._llm_extract_features",
            new_callable=AsyncMock,
            return_value=mock_llm_response,
        ):
            features = await extract_spec_features(NIMBUS_SPEC)

        assert isinstance(features, list)
        assert len(features) >= 3
        assert any("forecast" in f.lower() for f in features)
        assert any("clock" in f.lower() for f in features)

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_llm_failure(self) -> None:
        """LLM call failure returns empty list (non-fatal)."""
        with patch(
            "src.integrations.spec_coverage._llm_extract_features",
            new_callable=AsyncMock,
            side_effect=Exception("LLM timeout"),
        ):
            features = await extract_spec_features(NIMBUS_SPEC)

        assert features == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_malformed_json(self) -> None:
        """Malformed LLM JSON returns empty list (non-fatal)."""
        with patch(
            "src.integrations.spec_coverage._llm_extract_features",
            new_callable=AsyncMock,
            return_value="not valid json at all",
        ):
            features = await extract_spec_features(NIMBUS_SPEC)

        assert features == []


class TestFindUncoveredFeatures:
    """find_uncovered_features compares feature list against task list."""

    def test_finds_missing_forecast_feature(self) -> None:
        """5-day forecast in spec but not in tasks → flagged as uncovered."""
        features = [
            "animated digital clock",
            "weather panel",
            "5-day forecast",
            "glassmorphism theme",
        ]
        uncovered = find_uncovered_features(
            features=features,
            tasks=NIMBUS_TASKS_MISSING_FORECAST,
        )

        assert len(uncovered) >= 1
        assert any("forecast" in f.lower() for f in uncovered)

    def test_no_uncovered_when_all_features_present(self) -> None:
        """All features have matching tasks → returns empty list."""
        features = [
            "animated digital clock",
            "weather panel",
            "5-day forecast",
        ]
        uncovered = find_uncovered_features(
            features=features,
            tasks=NIMBUS_TASKS_COMPLETE,
        )

        assert uncovered == []

    def test_coverage_is_case_insensitive(self) -> None:
        """Feature matching ignores case."""
        features = ["Animated Digital Clock", "WEATHER PANEL"]
        uncovered = find_uncovered_features(
            features=features,
            tasks=NIMBUS_TASKS_MISSING_FORECAST,
        )

        assert uncovered == []

    def test_partial_word_match_counts_as_covered(self) -> None:
        """'clock' in feature matches 'AnimatedClock' in task name."""
        features = ["clock animation"]
        uncovered = find_uncovered_features(
            features=features,
            tasks=NIMBUS_TASKS_MISSING_FORECAST,
        )

        assert uncovered == []

    def test_returns_empty_when_no_features(self) -> None:
        """No features extracted → nothing to check."""
        uncovered = find_uncovered_features(
            features=[],
            tasks=NIMBUS_TASKS_MISSING_FORECAST,
        )

        assert uncovered == []

    def test_checks_task_description_not_just_name(self) -> None:
        """Feature word in task description counts as covered."""
        tasks = [
            _make_task(
                "Backend Services",
                description="REST endpoints including forecast data endpoint",
            )
        ]
        uncovered = find_uncovered_features(
            features=["forecast"],
            tasks=tasks,
        )

        assert uncovered == []


class TestCheckSpecCoverage:
    """check_spec_coverage orchestrates extraction + gap task generation."""

    @pytest.mark.asyncio
    async def test_returns_gap_task_for_missing_forecast(self) -> None:
        """Missing 5-day forecast → gap task synthesized."""
        extracted_features = [
            "animated digital clock",
            "weather panel",
            "5-day forecast",
        ]
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=extracted_features,
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=["5-day forecast"],
            ),
        ):
            gap_tasks = await check_spec_coverage(
                description=NIMBUS_SPEC,
                tasks=NIMBUS_TASKS_MISSING_FORECAST,
            )

        assert len(gap_tasks) >= 1
        gap_names = [t.name.lower() for t in gap_tasks]
        assert any("forecast" in name for name in gap_names)

    @pytest.mark.asyncio
    async def test_returns_empty_when_all_covered(self) -> None:
        """All features covered → no gap tasks."""
        extracted_features = [
            "animated digital clock",
            "weather panel",
            "5-day forecast",
        ]
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=extracted_features,
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            gap_tasks = await check_spec_coverage(
                description=NIMBUS_SPEC,
                tasks=NIMBUS_TASKS_COMPLETE,
            )

        assert gap_tasks == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_llm_failure(self) -> None:
        """LLM failure → no gap tasks, no crash (non-fatal)."""
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=[],  # empty on LLM failure
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
            ) as mock_confirm,
        ):
            gap_tasks = await check_spec_coverage(
                description=NIMBUS_SPEC,
                tasks=NIMBUS_TASKS_MISSING_FORECAST,
            )

        assert gap_tasks == []
        mock_confirm.assert_not_awaited()  # no features -> nothing to confirm

    @pytest.mark.asyncio
    async def test_gap_task_has_correct_labels(self) -> None:
        """Gap tasks are labeled spec_gap so Cato/agents recognize them."""
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=["5-day forecast"],
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=["5-day forecast"],
            ),
        ):
            gap_tasks = await check_spec_coverage(
                description=NIMBUS_SPEC,
                tasks=NIMBUS_TASKS_MISSING_FORECAST,
            )

        assert len(gap_tasks) >= 1
        gap_task = gap_tasks[0]
        assert "spec_gap" in gap_task.labels

    @pytest.mark.asyncio
    async def test_gap_task_description_mentions_spec_source(self) -> None:
        """Gap task description explains it was found by spec coverage check."""
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=["5-day forecast"],
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=["5-day forecast"],
            ),
        ):
            gap_tasks = await check_spec_coverage(
                description=NIMBUS_SPEC,
                tasks=NIMBUS_TASKS_MISSING_FORECAST,
            )

        assert len(gap_tasks) >= 1
        desc = gap_tasks[0].description.lower()
        assert "spec" in desc or "coverage" in desc or "missing" in desc


class TestLlmConfirmUncovered:
    """Issue #666: the LLM semantic-coverage check that replaces keyword
    guessing — judges, per feature, whether any task actually delivers it.
    """

    @pytest.mark.asyncio
    async def test_returns_features_the_llm_marks_uncovered(self) -> None:
        """Returns exactly the features the LLM judges no task delivers."""
        from src.integrations.spec_coverage import _llm_confirm_uncovered

        tasks = [_make_task("Game Engine", "core loop and rendering")]
        with patch("src.ai.providers.llm_abstraction.LLMAbstraction") as mock_llm:
            mock_llm.return_value.analyze = AsyncMock(return_value='["game restart"]')
            out = await _llm_confirm_uncovered(["game restart", "move snake"], tasks)
        assert out == ["game restart"]

    @pytest.mark.asyncio
    async def test_filters_out_features_not_in_input(self) -> None:
        """Guards against the LLM inventing feature strings."""
        from src.integrations.spec_coverage import _llm_confirm_uncovered

        with patch("src.ai.providers.llm_abstraction.LLMAbstraction") as mock_llm:
            mock_llm.return_value.analyze = AsyncMock(
                return_value='["hallucinated", "real one"]'
            )
            out = await _llm_confirm_uncovered(["real one"], [_make_task("t")])
        assert out == ["real one"]

    @pytest.mark.asyncio
    async def test_returns_none_on_unparseable_response(self) -> None:
        """A non-JSON response yields None so the caller can fall back."""
        from src.integrations.spec_coverage import _llm_confirm_uncovered

        with patch("src.ai.providers.llm_abstraction.LLMAbstraction") as mock_llm:
            mock_llm.return_value.analyze = AsyncMock(return_value="not json at all")
            out = await _llm_confirm_uncovered(["x"], [_make_task("t")])
        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        """Any LLM error yields None (non-fatal — caller falls back)."""
        from src.integrations.spec_coverage import _llm_confirm_uncovered

        with patch("src.ai.providers.llm_abstraction.LLMAbstraction") as mock_llm:
            mock_llm.return_value.analyze = AsyncMock(side_effect=RuntimeError("boom"))
            out = await _llm_confirm_uncovered(["x"], [_make_task("t")])
        assert out is None

    @pytest.mark.asyncio
    async def test_prompt_includes_task_criteria(self) -> None:
        """The coverage prompt includes completion/acceptance criteria, so a
        feature outcome coverage rolled into a task's criteria (#607/#611) is
        seen as covered — preventing a duplicate-task double-fire with #665.
        """
        from src.integrations.spec_coverage import _llm_confirm_uncovered

        task = _make_task("Tech Foundation", "vite project scaffold")
        task.completion_criteria = [
            "Implementation must cover: keyboard input handler for arrows"
        ]
        captured: dict[str, str] = {}

        async def _fake_analyze(prompt: str, ctx: object, operation: str = "") -> str:
            captured["prompt"] = prompt
            return "[]"

        with patch("src.ai.providers.llm_abstraction.LLMAbstraction") as mock_llm:
            mock_llm.return_value.analyze = AsyncMock(side_effect=_fake_analyze)
            await _llm_confirm_uncovered(["keyboard input"], [task])

        assert "keyboard input handler for arrows" in captured["prompt"]


class TestCheckSpecCoverageSemantic:
    """Issue #666: check_spec_coverage uses semantic coverage, with the
    keyword scan retained only as a fallback when the LLM call fails.
    """

    @pytest.mark.asyncio
    async def test_synthesizes_gap_the_keyword_scan_would_miss(self) -> None:
        """The snake-restart regression: 'game restart' is marked covered by
        the keyword scan (the word 'game' matches a task) yet no task does
        restart — semantic coverage catches it and synthesizes a task.
        """
        tasks = [_make_task("Game Mechanics", "snake movement and growth")]
        # Prove the keyword scan MISSES it (the false negative behind #666):
        assert find_uncovered_features(["game restart"], tasks) == []
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=["game restart"],
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=["game restart"],
            ),
        ):
            gaps = await check_spec_coverage("snake game with restart", tasks)
        assert len(gaps) == 1
        assert gaps[0].name == "Implement Game Restart"
        assert "spec_gap" in gaps[0].labels

    @pytest.mark.asyncio
    async def test_no_spurious_task_when_semantically_covered(self) -> None:
        """The #637 regression: a feature a task DOES deliver is not
        duplicated, even though the keyword scan would flag it (no shared
        word with the covering task).
        """
        tasks = [
            _make_task(
                "Game Presentation and Rendering System",
                "draws the playfield to an html canvas each frame",
            )
        ]
        # Keyword scan WOULD flag it (false positive -> spurious build):
        assert find_uncovered_features(["browser playability"], tasks) == [
            "browser playability"
        ]
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=["browser playability"],
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            gaps = await check_spec_coverage("playable in a browser", tasks)
        assert gaps == []

    @pytest.mark.asyncio
    async def test_domain_agnostic_non_game_gap(self) -> None:
        """Not game-specific: a SaaS 'password reset' gap is synthesized the
        same way, proving the logic carries no domain assumptions.
        """
        tasks = [_make_task("User Login", "email and password authentication")]
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=["password reset"],
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=["password reset"],
            ),
        ):
            gaps = await check_spec_coverage("saas app with login and reset", tasks)
        assert len(gaps) == 1
        assert gaps[0].name == "Implement Password Reset"

    @pytest.mark.asyncio
    async def test_falls_back_to_keyword_when_semantic_check_unavailable(
        self,
    ) -> None:
        """When the LLM coverage check errors (returns None), the keyword
        scan is the fallback so the pipeline stays non-fatal.
        """
        tasks = [_make_task("Login", "auth")]
        with (
            patch(
                "src.integrations.spec_coverage.extract_spec_features",
                new_callable=AsyncMock,
                return_value=["data export"],
            ),
            patch(
                "src.integrations.spec_coverage._llm_confirm_uncovered",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            gaps = await check_spec_coverage("app with data export", tasks)
        assert len(gaps) == 1
        assert gaps[0].name == "Implement Data Export"
