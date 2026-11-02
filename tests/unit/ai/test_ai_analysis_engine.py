"""
Unit tests for the AI Analysis Engine.

This module tests the AI-powered decision-making capabilities of Marcus,
including task assignment optimization, blocker analysis, and project risk assessment.

Notes
-----
Tests use mocked Anthropic API calls to ensure reproducibility and avoid
external dependencies during testing.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import (
    BlockerReport,
    Priority,
    ProjectRisk,
    ProjectState,
    RiskLevel,
    Task,
    TaskStatus,
    WorkerStatus,
)
from src.integrations.ai_analysis_engine import AIAnalysisEngine


class TestAIAnalysisEngine:
    """
    Test suite for the AI Analysis Engine.

    This tests the core intelligence of Marcus - how it makes decisions
    about task assignments, analyzes blockers, and provides recommendations.
    """

    @pytest.fixture
    def ai_engine(self) -> AIAnalysisEngine:
        """
        Create a test instance of the AI Analysis Engine.

        Returns
        -------
        AIAnalysisEngine
            An AI engine instance with mocked Anthropic client.

        Notes
        -----
        The Anthropic client is mocked to avoid real API calls during tests.
        This fixture runs before each test method.
        """
        with patch("anthropic.Anthropic") as mock_anthropic:
            engine = AIAnalysisEngine()
            # Mock the client to avoid real API calls in tests
            engine.client = Mock()
            return engine

    @pytest.fixture
    def sample_tasks(self) -> List[Task]:
        """
        Create sample task data for testing.

        Returns
        -------
        List[Task]
            A list of task objects with varying priorities and requirements.

        Notes
        -----
        Tasks represent different scenarios including high-priority features,
        bug fixes, and technical debt items to test various decision paths.
        """
        return [
            Task(
                id="TASK-001",
                name="Implement user authentication",
                description="Add OAuth2 login functionality",
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=datetime.now(timezone.utc) + timedelta(days=3),
                estimated_hours=16.0,
                dependencies=[],
                labels=["backend", "security", "oauth"],
            ),
            Task(
                id="TASK-002",
                name="Create dashboard UI",
                description="Design and implement analytics dashboard",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=datetime.now(timezone.utc) + timedelta(days=5),
                estimated_hours=24.0,
                dependencies=["TASK-001"],
                labels=["frontend", "react", "ui/ux"],
            ),
            Task(
                id="TASK-003",
                name="Fix critical bug in payment system",
                description="Payment processing fails for certain cards",
                status=TaskStatus.TODO,
                priority=Priority.URGENT,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=datetime.now(timezone.utc) + timedelta(hours=4),
                estimated_hours=4.0,
                dependencies=[],
                labels=["backend", "payments", "bug", "critical"],
            ),
        ]

    @pytest.fixture
    def sample_workers(self):
        """
        STEP 3: Create sample worker profiles

        What's happening:
        - We create different worker personas with varying skills and capacities
        - Each worker has different strengths (backend vs frontend, senior vs junior)
        - This tests how the AI matches tasks to the right people
        """
        return {
            "backend_senior": WorkerStatus(
                worker_id="dev-001",
                name="Alice Johnson",
                role="Senior Backend Developer",
                email="alice@example.com",
                current_tasks=[],
                completed_tasks_count=50,
                capacity=40,
                skills=[
                    "python",
                    "django",
                    "postgresql",
                    "oauth",
                    "payments",
                    "security",
                ],
                availability={"monday": True, "tuesday": True, "wednesday": True},
                performance_score=1.2,  # Above average performer
            ),
            "frontend_dev": WorkerStatus(
                worker_id="dev-002",
                name="Bob Smith",
                role="Frontend Developer",
                email="bob@example.com",
                current_tasks=[],
                completed_tasks_count=30,
                capacity=35,
                skills=["react", "typescript", "css", "ui/ux", "javascript"],
                availability={"monday": True, "tuesday": True},
                performance_score=1.0,
            ),
            "fullstack_junior": WorkerStatus(
                worker_id="dev-003",
                name="Charlie Davis",
                role="Junior Full-stack Developer",
                email="charlie@example.com",
                current_tasks=[],
                completed_tasks_count=10,
                capacity=30,
                skills=["python", "react", "basic"],
                availability={"monday": True},
                performance_score=0.8,  # Still learning
            ),
        }

    @pytest.mark.asyncio
    async def test_match_task_to_agent_urgent_priority(
        self, ai_engine, sample_tasks, sample_workers
    ):
        """
        TEST 1: Verify AI assigns urgent tasks to the most qualified available agent

        What's being tested:
        1. The AI recognizes task priority (URGENT > HIGH > MEDIUM)
        2. It matches task requirements (payments) to worker skills
        3. It selects the senior developer for critical work

        Step-by-step:
        1. We mock the AI's response to return TASK-003 (urgent payment bug)
        2. We ask the AI to find a task for our senior backend developer
        3. We verify it chose the urgent task over others
        """
        # STEP 1: Mock the Claude API response
        mock_response = {
            "recommended_task_id": "TASK-003",  # The urgent payment bug
            "confidence_score": 0.95,
            "reasoning": "Urgent payment bug requires immediate attention from experienced developer",
            "alternative_tasks": ["TASK-001"],
            "considerations": ["Critical business impact", "Matches developer skills"],
        }

        ai_engine._call_claude = AsyncMock(return_value=json.dumps(mock_response))

        # STEP 2: Create a simple project state
        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test Project",
            total_tasks=10,
            completed_tasks=3,
            in_progress_tasks=2,
            blocked_tasks=0,
            progress_percent=30.0,
            overdue_tasks=[],
            team_velocity=5.0,
            risk_level=RiskLevel.MEDIUM,
            last_updated=datetime.now(timezone.utc),
        )

        # STEP 3: Call the AI to match a task
        result = await ai_engine.match_task_to_agent(
            sample_tasks, sample_workers["backend_senior"], project_state
        )

        # STEP 4: Verify the results
        assert result is not None
        assert result.id == "TASK-003"
        assert result.priority == Priority.URGENT

        # STEP 5: Verify the AI was called with proper context
        call_args = ai_engine._call_claude.call_args[0][0]
        assert "payment" in call_args.lower()
        assert "urgent" in call_args.lower()

    @pytest.mark.asyncio
    async def test_generate_task_instructions(
        self, ai_engine, sample_tasks, sample_workers
    ):
        """
        TEST 2: Verify AI generates appropriate instructions based on worker experience

        What's being tested:
        1. The AI tailors instructions to the worker's skill level
        2. Instructions are comprehensive and actionable
        3. The AI considers the task context and requirements

        Step-by-step:
        1. We test with both senior and junior developers
        2. We verify instructions are more detailed for juniors
        3. We check that instructions include relevant technical details
        """
        # Test Case 1: Instructions for senior developer
        senior_task = sample_tasks[0]  # Auth implementation
        senior_worker = sample_workers["backend_senior"]

        # Mock response for senior developer
        ai_engine._call_claude = AsyncMock(
            return_value="""## OAuth2 Implementation Instructions

Given your experience with authentication systems, here's the streamlined approach:

1. **Setup OAuth2 Provider**
   - Configure Google/GitHub OAuth credentials
   - Set up redirect URIs in provider console

2. **Implementation**
   - Use `authlib` or `python-social-auth`
   - Implement callback handlers
   - Store tokens securely (consider Redis for sessions)

3. **Security Considerations**
   - Implement PKCE flow for additional security
   - Set up proper CORS policies
   - Add rate limiting on auth endpoints

You're familiar with these patterns, so focus on our specific business requirements."""
        )

        senior_instructions = await ai_engine.generate_task_instructions(
            senior_task, senior_worker
        )

        # Verify senior instructions are concise but thorough
        assert "OAuth2" in senior_instructions
        assert "PKCE" in senior_instructions  # Advanced security concept
        assert len(senior_instructions) < 1000  # Not overly verbose

        # Test Case 2: Instructions for junior developer
        junior_worker = sample_workers["fullstack_junior"]

        # Mock more detailed response for junior
        ai_engine._call_claude = AsyncMock(
            return_value="""## OAuth2 Implementation Instructions - Detailed Guide

Since this is your first OAuth2 implementation, here's a step-by-step guide:

1. **Understanding OAuth2**
   - OAuth2 is a protocol that allows users to log in using existing accounts
   - Read this primer first: [link to OAuth2 basics]

2. **Detailed Implementation Steps**

   a. Install required packages:
   ```bash
   pip install authlib requests
   ```

   b. Create OAuth configuration:
   ```python
   # config/oauth.py
   OAUTH_PROVIDERS = {
       'google': {
           'client_id': os.environ.get('GOOGLE_CLIENT_ID'),
           'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET'),
           'authorize_url': 'https://accounts.google.com/o/oauth2/v2/auth',
           'token_url': 'https://oauth2.googleapis.com/token'
       }
   }
   ```

   c. Implement the login route:
   ```python
   @app.route('/login/<provider>')
   def login(provider):
       # Detailed implementation here...
   ```

3. **Testing Your Implementation**
   - Start with local testing
   - Use ngrok for callback URL testing
   - Write unit tests for each component

4. **Getting Help**
   - Check with Alice (senior backend) if you get stuck
   - Useful debugging tips: check network tab, log all responses

Remember: Take your time, test thoroughly, and ask questions!"""
        )

        junior_instructions = await ai_engine.generate_task_instructions(
            senior_task, junior_worker
        )

        # Verify junior instructions are more detailed
        assert len(junior_instructions) > len(senior_instructions)
        assert "step-by-step" in junior_instructions.lower()
        assert "pip install" in junior_instructions  # Includes basic setup
        assert "Getting Help" in junior_instructions  # Guidance on asking for help

    @pytest.mark.asyncio
    async def test_analyze_blocker_with_escalation(self, ai_engine):
        """
        TEST 3: Verify AI correctly analyzes blockers and determines escalation needs

        What's being tested:
        1. The AI identifies root causes of blockers
        2. It determines when escalation is needed
        3. It provides actionable resolution steps

        Step-by-step:
        1. We present a serious production blocker
        2. The AI analyzes it and determines escalation is needed
        3. We verify the response includes proper escalation procedures
        """
        # Simulate a critical production blocker
        task_id = "TASK-100"
        blocker_description = (
            "Production database is showing 90% CPU usage, queries timing out"
        )
        severity = "critical"

        # Mock AI response for critical blocker
        mock_analysis = {
            "root_cause": "Database query optimization needed - missing indexes on large tables",
            "impact_assessment": "All users experiencing slow response times, potential data loss",
            "needs_coordination": True,
            "resolution_steps": [
                "1. Immediately add read replica to distribute load",
                "2. Identify slow queries using pg_stat_statements",
                "3. Add missing indexes on user_transactions table",
                "4. Implement query result caching",
                "5. Schedule database maintenance window",
            ],
            "required_resources": ["DBA", "Senior Backend Developer", "DevOps"],
            "estimated_hours": 6,
            "escalation_needed": True,
            "prevention_measures": [
                "Implement query performance monitoring",
                "Add automated index recommendations",
                "Set up capacity planning alerts",
            ],
        }

        ai_engine._call_claude = AsyncMock(return_value=json.dumps(mock_analysis))

        # Call the blocker analysis
        result = await ai_engine.analyze_blocker(task_id, blocker_description, severity)

        # Verify critical elements of the response
        assert result["escalation_needed"] is True
        assert result["needs_coordination"] is True
        assert "DBA" in result["required_resources"]
        assert result["estimated_hours"] >= 4  # Significant time needed
        assert len(result["resolution_steps"]) >= 3  # Multiple steps required
        assert "prevention_measures" in result  # Includes future prevention

    @pytest.mark.asyncio
    async def test_project_health_analysis(self, ai_engine):
        """
        TEST 4: Verify AI correctly assesses overall project health

        What's being tested:
        1. The AI synthesizes multiple data points into health assessment
        2. It identifies risks and provides recommendations
        3. It predicts timeline impacts

        Step-by-step:
        1. We provide project data showing warning signs
        2. The AI analyzes trends and team capacity
        3. We verify it identifies the issues and suggests fixes
        """
        # Create project state with some concerning metrics
        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="E-commerce Platform",
            total_tasks=100,
            completed_tasks=25,
            in_progress_tasks=30,  # Too many tasks in progress
            blocked_tasks=8,  # High number of blockers
            progress_percent=25.0,
            overdue_tasks=[],  # Will be populated
            team_velocity=3.0,  # Low velocity
            risk_level=RiskLevel.HIGH,
            last_updated=datetime.now(timezone.utc),
        )

        # Recent activities showing problems
        recent_activities = [
            {"type": "task_blocked", "count": 5, "timeframe": "last_week"},
            {"type": "task_completed", "count": 3, "timeframe": "last_week"},
            {"type": "deadline_missed", "count": 2, "timeframe": "last_week"},
        ]

        # Team showing capacity issues
        team_status = [
            WorkerStatus(
                worker_id="dev-001",
                name="Alice",
                role="Senior Dev",
                email="alice@example.com",
                current_tasks=[Mock(), Mock(), Mock(), Mock()],  # Overloaded
                completed_tasks_count=20,
                capacity=40,
                skills=["python"],
                availability={},
                performance_score=0.7,  # Performance dropping
            )
        ]

        # Mock AI health analysis response
        mock_analysis = {
            "overall_health": "red",
            "progress_assessment": "Project is at high risk of missing deadline",
            "risk_factors": [
                {
                    "type": "resource",
                    "severity": "high",
                    "description": "Team is overloaded - 30 tasks in progress with only 5 developers",
                },
                {
                    "type": "technical",
                    "severity": "medium",
                    "description": "High blocker rate indicates architectural issues",
                },
                {
                    "type": "timeline",
                    "severity": "high",
                    "description": "Current velocity of 3 tasks/week won't meet deadline",
                },
            ],
            "recommendations": [
                {
                    "action": "Immediately limit work in progress to 2 tasks per developer",
                    "priority": "high",
                    "owner": "Project Manager",
                },
                {
                    "action": "Conduct blocker retrospective to identify patterns",
                    "priority": "high",
                    "owner": "Tech Lead",
                },
                {
                    "action": "Consider bringing in additional resources or extending timeline",
                    "priority": "medium",
                    "owner": "Stakeholders",
                },
            ],
            "timeline_prediction": {
                "on_track": False,
                "estimated_completion": "2024-06-15",  # 3 months late
                "confidence": 0.3,
            },
            "resource_optimization": [
                {
                    "suggestion": "Redistribute tasks from overloaded developers",
                    "impact": "Could improve velocity by 40%",
                }
            ],
        }

        ai_engine._call_claude = AsyncMock(return_value=json.dumps(mock_analysis))

        # Run the analysis
        result = await ai_engine.analyze_project_health(
            project_state, recent_activities, team_status
        )

        # Verify the AI identified all major issues
        assert result["overall_health"] == "red"
        assert result["timeline_prediction"]["on_track"] is False
        assert len(result["risk_factors"]) >= 3
        assert any(risk["type"] == "resource" for risk in result["risk_factors"])
        assert len(result["recommendations"]) >= 2
        assert result["timeline_prediction"]["confidence"] < 0.5  # Low confidence

    @pytest.mark.asyncio
    async def test_fallback_behavior_on_api_error(
        self, ai_engine, sample_tasks, sample_workers
    ):
        """
        TEST 5: Verify system degrades gracefully when AI is unavailable

        What's being tested:
        1. The system doesn't crash when Claude API fails
        2. It falls back to reasonable defaults
        3. Basic functionality continues working

        Step-by-step:
        1. We simulate an API error
        2. The system falls back to simple priority-based assignment
        3. We verify the fallback produces valid results
        """
        # Simulate API failure
        ai_engine._call_claude = AsyncMock(
            side_effect=Exception("API rate limit exceeded")
        )

        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test Project",
            total_tasks=10,
            completed_tasks=5,
            in_progress_tasks=3,
            blocked_tasks=0,
            progress_percent=50.0,
            overdue_tasks=[],
            team_velocity=5.0,
            risk_level=RiskLevel.LOW,
            last_updated=datetime.now(timezone.utc),
        )

        # Should fall back to highest priority task
        result = await ai_engine.match_task_to_agent(
            sample_tasks, sample_workers["backend_senior"], project_state
        )

        # Verify fallback behavior
        assert result is not None
        assert result.priority == Priority.URGENT  # Picked highest priority

        # Test blocker analysis fallback
        blocker_result = await ai_engine.analyze_blocker(
            "TASK-999", "Cannot connect to external API", "high"
        )

        # Verify fallback provides basic structure
        assert "resolution_steps" in blocker_result
        assert "escalation_needed" in blocker_result
        assert blocker_result["escalation_needed"] is True  # Conservative approach

    @pytest.mark.asyncio
    async def test_client_initialization_with_api_key(self, monkeypatch):
        """Test successful client initialization with API key"""
        # Mock environment variable
        monkeypatch.setenv("CLAUDE_API_KEY", "test-api-key")

        # Mock config to return test API key
        with patch("src.config.marcus_config.get_config") as mock_get_config:
            mock_config = Mock()
            mock_config.ai.anthropic_api_key = "test-api-key"
            mock_config.ai.provider = "anthropic"
            mock_get_config.return_value = mock_config

            # Mock anthropic module
            with patch(
                "src.integrations.ai_analysis_engine.anthropic"
            ) as mock_anthropic:
                mock_client = Mock()
                mock_anthropic.Anthropic.return_value = mock_client

                # Create engine - should initialize client
                engine = AIAnalysisEngine()

                assert engine.client == mock_client
                mock_anthropic.Anthropic.assert_called_once_with(api_key="test-api-key")

    @pytest.mark.asyncio
    async def test_client_initialization_failure(self, monkeypatch, capsys):
        """Test client initialization when Anthropic raises exception"""
        monkeypatch.setenv("CLAUDE_API_KEY", "test-api-key")

        # Mock config loader
        with patch("src.config.config_loader.get_config") as mock_get_config:
            mock_config = Mock()
            mock_config.get.return_value = "test-api-key"
            mock_get_config.return_value = mock_config

            with patch(
                "src.integrations.ai_analysis_engine.anthropic"
            ) as mock_anthropic:
                mock_anthropic.Anthropic.side_effect = Exception("Connection failed")

                engine = AIAnalysisEngine()

                assert engine.client is None
            # Note: Error logging removed to avoid MCP stdio interference

    @pytest.mark.asyncio
    async def test_generate_task_instructions_with_agent(
        self, ai_engine, sample_tasks, sample_workers
    ):
        """Test instruction generation with agent context"""
        task = sample_tasks[0]
        agent = sample_workers["backend_senior"]

        mock_response = """## Task Instructions

**Setup Steps:**
1. Install dependencies
2. Configure OAuth

**Implementation Steps:**
1. Create auth module
2. Add OAuth endpoints

**Testing Approach:**
Write unit tests for auth flow

**Acceptance Criteria:**
- Users can login
- Tokens are secure"""

        ai_engine._call_claude = AsyncMock(return_value=mock_response)

        result = await ai_engine.generate_task_instructions(task, agent)

        assert isinstance(result, str)
        assert "Setup Steps" in result
        assert "Implementation Steps" in result

    @pytest.mark.asyncio
    async def test_generate_task_instructions_fallback(self, ai_engine, sample_tasks):
        """Test instruction generation fallback when AI fails"""
        task = sample_tasks[0]

        # Make AI call fail
        ai_engine._call_claude = AsyncMock(side_effect=Exception("API error"))

        result = await ai_engine.generate_task_instructions(task)

        # Should return fallback instructions
        assert isinstance(result, str)
        assert "Task Assignment" in result
        assert task.name in result

    @pytest.mark.asyncio
    async def test_analyze_blocker_json_error(self, ai_engine):
        """Test blocker analysis with JSON parsing error"""
        ai_engine._call_claude = AsyncMock(return_value="Not valid JSON")

        result = await ai_engine.analyze_blocker("TASK-001", "Database down", "high")

        # Should return fallback
        assert result["escalation_needed"] is True
        assert "resolution_steps" in result

    @pytest.mark.asyncio
    async def test_analyze_project_risks_with_blockers(self, ai_engine, sample_workers):
        """Test project risk analysis with recent blockers"""
        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test Project",
            total_tasks=50,
            completed_tasks=10,
            in_progress_tasks=20,
            blocked_tasks=5,
            progress_percent=20.0,
            overdue_tasks=[],
            team_velocity=2.0,
            risk_level=RiskLevel.HIGH,
            last_updated=datetime.now(timezone.utc),
        )

        blockers = [
            BlockerReport(
                task_id="TASK-001",
                reporter_id="dev-001",
                description="API rate limit",
                severity=RiskLevel.HIGH,
                reported_at=datetime.now(timezone.utc),
                resolved=False,
                resolution=None,
                resolved_at=None,
            )
        ]

        team = [sample_workers["backend_senior"]]

        mock_response = json.dumps(
            {
                "risks": [
                    {
                        "type": "technical",
                        "description": "API dependencies causing delays",
                        "likelihood": "high",
                        "impact": "high",
                        "mitigation": "Implement caching layer",
                    }
                ],
                "overall_risk_level": "high",
                "recommendations": ["Add redundancy", "Review architecture"],
            }
        )

        ai_engine._call_claude = AsyncMock(return_value=mock_response)

        result = await ai_engine.analyze_project_risks(project_state, blockers, team)

        assert len(result) > 0
        assert result[0].risk_type == "project"
        assert result[0].severity == RiskLevel.HIGH

    @pytest.mark.asyncio
    async def test_analyze_project_risks_json_error(self, ai_engine):
        """Test risk analysis with JSON parsing error"""
        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test Project",
            total_tasks=10,
            completed_tasks=5,
            in_progress_tasks=3,
            blocked_tasks=0,
            progress_percent=50.0,
            overdue_tasks=[],
            team_velocity=5.0,
            risk_level=RiskLevel.LOW,
            last_updated=datetime.now(timezone.utc),
        )

        ai_engine._call_claude = AsyncMock(return_value="Invalid response")

        result = await ai_engine.analyze_project_risks(project_state, [], [])

        # Should return empty list on error for low risk project
        assert len(result) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "risk_level,expected_risk_count,expected_risk_type",
        [
            (RiskLevel.HIGH, 1, "timeline"),
            (RiskLevel.MEDIUM, 0, None),
            (RiskLevel.LOW, 0, None),
        ],
    )
    async def test_fallback_risk_analysis(
        self, risk_level, expected_risk_count, expected_risk_type
    ):
        """Test fallback risk analysis for different risk levels"""
        engine = AIAnalysisEngine()

        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test Project",
            total_tasks=10,
            completed_tasks=2,
            in_progress_tasks=3,
            blocked_tasks=2,
            progress_percent=20.0,
            overdue_tasks=[],
            team_velocity=1.0,
            risk_level=risk_level,
            last_updated=datetime.now(timezone.utc),
        )

        risks = engine._generate_fallback_risk_analysis(project_state)

        assert len(risks) == expected_risk_count
        if expected_risk_count > 0:
            assert risks[0].risk_type == expected_risk_type
            assert risks[0].severity == risk_level

    @pytest.mark.asyncio
    async def test_call_claude_success(self, ai_engine):
        """Test successful Claude API call"""
        mock_response = Mock()
        mock_response.content = [Mock(text="AI response")]

        ai_engine.client = Mock()
        ai_engine.client.messages.create.return_value = mock_response

        result = await ai_engine._call_claude("test prompt")

        assert result == "AI response"
        ai_engine.client.messages.create.assert_called_once_with(
            model=ai_engine.model,
            max_tokens=2000,
            temperature=0.7,
            messages=[{"role": "user", "content": "test prompt"}],
        )

    @pytest.mark.asyncio
    async def test_call_claude_no_client(self, ai_engine):
        """Test Claude call when client is None"""
        ai_engine.client = None

        with pytest.raises(Exception, match="Anthropic client not available"):
            await ai_engine._call_claude("test prompt")

    @pytest.mark.asyncio
    async def test_call_claude_api_error(self, ai_engine, capsys):
        """Test Claude call with API error"""
        ai_engine.client = Mock()
        ai_engine.client.messages.create.side_effect = Exception("API rate limit")

        with pytest.raises(Exception):
            await ai_engine._call_claude("test prompt")

        captured = capsys.readouterr()
        assert "Error calling Claude" in captured.err

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Plain JSON — no fences
            ('{"key": "value"}', '{"key": "value"}'),
            # Standard ```json fence
            ('```json\n{"key": "value"}\n```', '{"key": "value"}'),
            # Plain ``` fence (no language tag)
            ('```\n{"key": "value"}\n```', '{"key": "value"}'),
            # Fence + trailing prose after closing fence
            (
                '```json\n{"key": "value"}\n```\n\nSome explanation here.',
                '{"key": "value"}',
            ),
            # Fence + leading prose before opening fence
            ('Here is the JSON:\n```json\n{"key": "value"}\n```', '{"key": "value"}'),
            # JSON array
            ("```json\n[1, 2, 3]\n```", "[1, 2, 3]"),
            # Nested JSON with braces in strings
            ('```json\n{"msg": "use {braces}"}\n```', '{"msg": "use {braces}"}'),
            # Extra whitespace inside fence
            ('```json\n\n  {"key": "value"}  \n\n```', '{"key": "value"}'),
            # No fence, but leading/trailing prose (extract by brace)
            ('Here you go: {"key": "value"} enjoy!', '{"key": "value"}'),
            # Multi-line JSON with trailing markdown section
            (
                '```json\n{"a": 1, "b": 2}\n```\n\n**Note:** something',
                '{"a": 1, "b": 2}',
            ),
            # Response is just whitespace around JSON
            ('  \n{"key": "value"}\n  ', '{"key": "value"}'),
        ],
    )
    async def test_call_claude_fence_stripping(self, ai_engine, raw, expected):
        """_call_claude strips markdown fences and trailing prose from all LLMs"""
        mock_response = Mock()
        mock_response.content = [Mock(text=raw)]
        mock_response.usage = None

        ai_engine.client = Mock()
        ai_engine.client.messages.create.return_value = mock_response

        result = await ai_engine._call_claude("test prompt")
        assert result == expected

    @pytest.mark.asyncio
    async def test_call_claude_fence_stripping_real_blocker_shape(self, ai_engine):
        """Fence stripping works on realistic blocker analysis response with trailing prose"""
        blocker_json = json.dumps(
            {
                "root_cause": "Validator bug",
                "resolution_steps": ["Check logs", "File a bug"],
                "escalation_needed": True,
            }
        )
        raw = f"```json\n{blocker_json}\n```\n\n**Quick Start:** Do this first."

        mock_response = Mock()
        mock_response.content = [Mock(text=raw)]
        mock_response.usage = None

        ai_engine.client = Mock()
        ai_engine.client.messages.create.return_value = mock_response

        result = await ai_engine._call_claude("blocker prompt")
        parsed = json.loads(result)
        assert parsed["root_cause"] == "Validator bug"
        assert parsed["escalation_needed"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "completed,blocked,overdue_count,velocity,risk_level,progress_percent,expected_health,expected_on_track",
        [
            # High risk project with many issues - completion rate (20/100=0.2) >= progress (0.4) = False
            (20, 5, 3, 1.5, RiskLevel.HIGH, 40.0, "red", False),
            # Medium risk project - completion rate (50/100=0.5) >= progress (0.5) = True
            (50, 3, 0, 5.0, RiskLevel.MEDIUM, 50.0, "yellow", True),
            # Low risk healthy project - completion rate (50/100=0.5) >= progress (0.5) = True
            (50, 0, 0, 5.0, RiskLevel.LOW, 50.0, "green", True),
            # Project ahead of schedule - completion rate (60/100=0.6) >= progress (0.5) = True
            (60, 0, 0, 5.0, RiskLevel.LOW, 50.0, "green", True),
            # Project behind schedule - completion rate (30/100=0.3) >= progress (0.5) = False
            (30, 1, 1, 3.0, RiskLevel.MEDIUM, 50.0, "yellow", False),
        ],
    )
    async def test_fallback_health_analysis_parameterized(
        self,
        completed,
        blocked,
        overdue_count,
        velocity,
        risk_level,
        progress_percent,
        expected_health,
        expected_on_track,
    ):
        """Test fallback health analysis with various project states"""
        engine = AIAnalysisEngine()

        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test Project",
            total_tasks=100,
            completed_tasks=completed,
            in_progress_tasks=30,
            blocked_tasks=blocked,
            progress_percent=progress_percent,
            overdue_tasks=[Mock() for _ in range(overdue_count)],
            team_velocity=velocity,
            risk_level=risk_level,
            last_updated=datetime.now(timezone.utc),
        )

        team_status: dict[str, Any] = {}

        result = engine._generate_fallback_health_analysis(project_state, team_status)

        assert result["overall_health"] == expected_health
        assert result["timeline_prediction"]["on_track"] == expected_on_track

    @pytest.mark.asyncio
    async def test_match_task_edge_cases(self, ai_engine):
        """Test task matching with edge cases"""
        # Empty task list
        result = await ai_engine.match_task_to_agent([], Mock(), Mock())
        assert result is None

        # Task with valid priority
        task_with_priority = Task(
            id="TASK-999",
            name="Task with priority",
            description="Test",
            status=TaskStatus.TODO,
            priority=Priority.LOW,  # Valid priority
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=1.0,
            dependencies=[],
            labels=[],
        )

        agent = WorkerStatus(
            worker_id="test-001",
            name="Test Worker",
            role="Developer",
            email="test@example.com",
            current_tasks=[],
            completed_tasks_count=0,
            capacity=40,
            skills=[],
            availability={},
            performance_score=1.0,
        )

        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test",
            total_tasks=10,
            completed_tasks=5,
            in_progress_tasks=3,
            blocked_tasks=0,
            progress_percent=50.0,
            overdue_tasks=[],
            team_velocity=5.0,
            risk_level=RiskLevel.LOW,
            last_updated=datetime.now(timezone.utc),
        )

        result = await ai_engine.match_task_to_agent(
            [task_with_priority], agent, project_state
        )
        assert result == task_with_priority  # Should return the only task

    @pytest.mark.asyncio
    async def test_project_health_with_dict_team_status(self, ai_engine):
        """Test project health analysis with dict team status"""
        project_state = Mock()
        project_state.board_id = "BOARD-001"
        project_state.project_name = "Test"
        project_state.total_tasks = 10
        project_state.completed_tasks = 5
        project_state.in_progress_tasks = 3
        project_state.blocked_tasks = 1
        project_state.progress_percent = 50.0
        project_state.team_velocity = 5.0
        project_state.risk_level = RiskLevel.LOW
        project_state.last_updated = datetime.now(timezone.utc)
        project_state.overdue_tasks = []

        # Pass team status as dict instead of list
        team_status = {"team_size": 5, "avg_capacity": 35}

        mock_response = json.dumps(
            {
                "overall_health": "green",
                "timeline_prediction": {
                    "on_track": True,
                    "estimated_completion": "On schedule",
                    "confidence": 0.8,
                },
                "risk_factors": [],
                "recommendations": [],
                "resource_optimization": [],
            }
        )

        ai_engine._call_claude = AsyncMock(return_value=mock_response)

        result = await ai_engine.analyze_project_health(
            project_state, [], team_status  # Dict instead of list
        )

        assert result["overall_health"] == "green"

    @pytest.mark.asyncio
    async def test_no_client_fallback_all_methods(self):
        """Test all methods work with no client"""
        # Create engine with no client
        engine = AIAnalysisEngine()
        engine.client = None

        # Test data
        task = Task(
            id="TASK-001",
            name="Test task",
            description="Test",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=None,
            estimated_hours=1.0,
            dependencies=[],
            labels=["test"],
        )

        worker = WorkerStatus(
            worker_id="test-001",
            name="Test Worker",
            role="Developer",
            email="test@example.com",
            current_tasks=[],
            completed_tasks_count=0,
            capacity=40,
            skills=["test"],
            availability={},
            performance_score=1.0,
        )

        project_state = ProjectState(
            board_id="BOARD-001",
            project_name="Test",
            total_tasks=10,
            completed_tasks=5,
            in_progress_tasks=3,
            blocked_tasks=0,
            progress_percent=50.0,
            overdue_tasks=[],
            team_velocity=5.0,
            risk_level=RiskLevel.LOW,
            last_updated=datetime.now(timezone.utc),
        )

        # Test match_task_to_agent with no client
        result = await engine.match_task_to_agent([task], worker, project_state)
        assert result == task

        # Test generate_task_instructions with no client
        instructions = await engine.generate_task_instructions(task, worker)
        assert "Task Assignment" in instructions

        # Test analyze_blocker with no client
        blocker_result = await engine.analyze_blocker(
            "TASK-001", "Test blocker", "high"
        )
        assert blocker_result["escalation_needed"] is True

        # Test analyze_project_health with no client
        health_result = await engine.analyze_project_health(project_state, [], [])
        assert "overall_health" in health_result

        # Test analyze_project_risks with no client
        risks = await engine.analyze_project_risks(project_state, [], [])
        assert isinstance(risks, list)

    @pytest.mark.asyncio
    async def test_generate_instructions_no_agent(self, ai_engine):
        """Test instruction generation without agent"""
        task = Task(
            id="TASK-001",
            name="Test task",
            description="Test description",
            status=TaskStatus.TODO,
            priority=Priority.HIGH,
            assigned_to=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            due_date=datetime.now(timezone.utc) + timedelta(days=3),
            estimated_hours=8.0,
            dependencies=["TASK-999"],
            labels=["backend"],
        )

        mock_instructions = "Test instructions"
        ai_engine._call_claude = AsyncMock(return_value=mock_instructions)

        result = await ai_engine.generate_task_instructions(task, None)
        assert result == mock_instructions

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "severity,description,expected_escalation",
        [
            ("medium", "Database slow", False),
            ("low", "Minor UI issue", False),
            ("unknown", "System down", False),
            ("high", "Production outage", True),
            ("urgent", "Critical system failure", True),
            ("critical", "Data corruption", False),  # Not in the implementation's list
        ],
    )
    async def test_blocker_severity_mapping(
        self, ai_engine, severity, description, expected_escalation
    ):
        """Test blocker analysis with different severity mappings"""
        result = await ai_engine.analyze_blocker("TASK-001", description, severity)
        assert result["escalation_needed"] == expected_escalation

    @pytest.mark.asyncio
    async def test_blocker_analysis_all_fields(self, ai_engine):
        """Test blocker analysis returns all expected fields"""
        mock_response = json.dumps(
            {
                "root_cause": "Database connection pool exhausted",
                "resolution_steps": [
                    "Check database connection settings",
                    "Increase connection pool size",
                    "Review query optimization",
                ],
                "estimated_resolution_time": "2-4 hours",
                "escalation_needed": False,
                "suggested_experts": ["DBA team", "Backend lead"],
                "similar_past_issues": [
                    {
                        "issue": "Connection timeouts in production",
                        "resolution": "Increased pool size and added monitoring",
                    }
                ],
            }
        )

        ai_engine._call_claude = AsyncMock(return_value=mock_response)

        result = await ai_engine.analyze_blocker(
            "TASK-001", "Database connection errors", "high"
        )

        # Verify all fields are present
        assert "root_cause" in result
        assert "resolution_steps" in result
        assert "estimated_resolution_time" in result
        assert "escalation_needed" in result
        assert "suggested_experts" in result
        assert "similar_past_issues" in result
        assert len(result["resolution_steps"]) == 3

    @pytest.mark.asyncio
    async def test_initialize_success(self, capsys):
        """Test successful initialization"""
        with patch("src.integrations.ai_analysis_engine.anthropic") as mock_anthropic:
            mock_client = Mock()
            mock_response = Mock()
            mock_response.content = [Mock(text="test")]
            mock_client.messages.create.return_value = mock_response
            mock_anthropic.Anthropic.return_value = mock_client

            with patch.dict(os.environ, {"CLAUDE_API_KEY": "test-key"}):
                engine = AIAnalysisEngine()
                engine.client = mock_client
                await engine.initialize()

                # Note: Success logging removed to avoid MCP stdio interference
                # Verify client is properly wrapped instead
                assert engine.client is not None

    @pytest.mark.asyncio
    async def test_initialize_no_client(self, capsys):
        """Test initialize with no client"""
        engine = AIAnalysisEngine()
        engine.client = None

        await engine.initialize()

        captured = capsys.readouterr()
        assert "AI Engine running in fallback mode" in captured.err

    @pytest.mark.asyncio
    async def test_initialize_connection_failure(self, capsys):
        """Test initialize when connection test fails"""
        with patch("src.integrations.ai_analysis_engine.anthropic") as mock_anthropic:
            mock_client = Mock()
            mock_client.messages.create.side_effect = Exception("Connection failed")
            mock_anthropic.Anthropic.return_value = mock_client

            with patch.dict(os.environ, {"CLAUDE_API_KEY": "test-key"}):
                engine = AIAnalysisEngine()
                engine.client = mock_client
                await engine.initialize()

                # Note: Error logging removed to avoid MCP stdio interference
                # Verify client is disabled on failure
                assert engine.client is None


@pytest.mark.unit
class TestCallClaudeCostRecording:
    """``_call_claude`` must write a ``token_events`` row (#768).

    Run-time planner calls — ``generate_task_instructions`` and
    ``analyze_blocker`` — reach the model through
    :meth:`AIAnalysisEngine._call_claude`, which uses a raw Anthropic
    SDK client. Before the fix this path recorded nothing to the cost
    store (``token_events``), so Cato under-reported every project's
    spend. These tests pin the recording and its attribution.
    """

    @pytest.fixture
    def cost_store(self, tmp_path):
        """A throwaway on-disk CostStore with seed prices loaded."""
        from src.cost_tracking.cost_store import CostStore

        store = CostStore(db_path=tmp_path / "costs.db")
        store.load_seed_prices()
        return store

    @pytest.fixture
    def recorder(self, cost_store):
        """Bind a fresh enabled recorder to the module singleton."""
        from src.cost_tracking.cost_recorder import CostRecorder, set_recorder

        rec = CostRecorder(store=cost_store, enabled=True)
        set_recorder(rec)
        yield rec
        set_recorder(None)

    @staticmethod
    def _mock_sdk_response(
        *,
        input_tokens: int,
        output_tokens: int,
        cache_creation: int = 0,
        cache_read: int = 0,
        text: str = '{"ok": true}',
        model: str = "claude-3-5-sonnet-20241022",
        request_id: str = "msg_abc",
    ) -> Mock:
        """Build a Mock mimicking anthropic SDK ``messages.create`` output."""
        usage = Mock()
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        usage.cache_creation_input_tokens = cache_creation
        usage.cache_read_input_tokens = cache_read
        resp = Mock()
        resp.usage = usage
        resp.model = model
        resp.id = request_id
        resp.content = [Mock(text=text)]
        return resp

    @pytest.mark.asyncio
    async def test_call_claude_records_token_event(self, recorder, cost_store):
        """A successful call writes one row with the response's token counts."""
        from src.cost_tracking.cost_recorder import PlannerContext

        engine = AIAnalysisEngine.__new__(AIAnalysisEngine)
        engine.model = "claude-3-5-sonnet-20241022"
        engine.current_project_id = None
        engine.current_agent_id = None
        engine.client = Mock()
        engine.client.messages.create.return_value = self._mock_sdk_response(
            input_tokens=120,
            output_tokens=45,
            cache_creation=10,
            cache_read=5,
        )

        with recorder.planner_context(
            PlannerContext(run_id="run_abc", project_id="projhex")
        ):
            await engine._call_claude("prompt", operation="generate_instructions")

        row = cost_store.conn.execute(
            "SELECT run_id, project_id, input_tokens, cache_creation_tokens, "
            "cache_read_tokens, output_tokens, operation, provider, request_id "
            "FROM token_events"
        ).fetchone()
        assert row == (
            "run_abc",
            "projhex",
            120,
            10,
            5,
            45,
            "generate_instructions",
            "anthropic",
            "msg_abc",
        )

    @pytest.mark.asyncio
    async def test_call_claude_skips_recording_without_context(
        self, recorder, cost_store
    ):
        """No active PlannerContext means no row.

        ``_call_claude`` has callers unrelated to the run-time planner
        operations (for example ``analyze_task``). They run with no active
        context and must not manufacture an ``'unassigned'`` cost row (#768).
        """
        engine = AIAnalysisEngine.__new__(AIAnalysisEngine)
        engine.model = "claude-3-5-sonnet-20241022"
        engine.current_project_id = None
        engine.current_agent_id = None
        engine.client = Mock()
        engine.client.messages.create.return_value = self._mock_sdk_response(
            input_tokens=7, output_tokens=3
        )

        # No ``recorder.planner_context(...)`` wrap: no attribution was set up.
        await engine._call_claude("prompt")

        (count,) = cost_store.conn.execute(
            "SELECT COUNT(*) FROM token_events"
        ).fetchone()
        assert count == 0

    @pytest.mark.asyncio
    async def test_call_claude_survives_nonnumeric_usage(self, recorder, cost_store):
        """A Mock/garbage usage object must never break the call path.

        The recorder is best-effort: a provider response whose usage
        fields are not plain integers must not raise into the caller. The
        call runs under an active context so the recording path, and its
        defensive coercion, is actually exercised.
        """
        from src.cost_tracking.cost_recorder import PlannerContext

        engine = AIAnalysisEngine.__new__(AIAnalysisEngine)
        engine.model = "claude-3-5-sonnet-20241022"
        engine.current_project_id = None
        engine.current_agent_id = None
        engine.client = Mock()
        # A bare Mock response: .usage is an auto-Mock whose token
        # attributes are Mocks, not ints.
        bare = Mock()
        bare.content = [Mock(text="plain text")]
        engine.client.messages.create.return_value = bare

        # Must not raise despite non-numeric usage fields.
        with recorder.planner_context(
            PlannerContext(run_id="run_x", project_id="projx")
        ):
            result = await engine._call_claude("prompt", operation="analyze_blocker")
        assert result == "plain text"
