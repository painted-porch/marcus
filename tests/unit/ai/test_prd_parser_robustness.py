"""
Unit tests for PRD parser robustness with varying AI response formats
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.ai.advanced.prd.advanced_parser import (
    AdvancedPRDParser,
    PRDAnalysis,
    ProjectConstraints,
)


class TestPRDParserRobustness:
    """Test PRD parser handles different AI response formats correctly"""

    @pytest.fixture
    def parser(self):
        """Create parser with mocked dependencies"""
        with patch("src.ai.advanced.prd.advanced_parser.LLMAbstraction"):
            with patch("src.ai.advanced.prd.advanced_parser.HybridDependencyInferer"):
                return AdvancedPRDParser()

    @pytest.fixture
    def mock_constraints(self):
        """Create mock project constraints"""
        return ProjectConstraints(team_size=2)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_break_down_epic_with_feature_field(self, parser, mock_constraints):
        """Test epic breakdown when req has 'feature' field"""
        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="agile",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        req = {"feature": "CRUD Operations"}
        tasks = await parser._break_down_epic(req, analysis, mock_constraints)

        # Issue #607 step 3: the paired Test task is rolled up into the
        # implement task's completion_criteria; only design + implement
        # land on the board.
        assert len(tasks) == 2
        assert tasks[0]["id"] == "task_crud_operations_design"
        assert tasks[0]["name"] == "Design CRUD Operations"
        assert tasks[1]["id"] == "task_crud_operations_implement"
        assert all(t["type"] != "testing" for t in tasks)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_break_down_epic_with_description_field(
        self, parser, mock_constraints
    ):
        """Test epic breakdown when req has 'description' field instead of 'feature'"""
        analysis = PRDAnalysis(
            functional_requirements=[{"description": "User Authentication"}],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="agile",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        req = {"description": "User Authentication"}
        tasks = await parser._break_down_epic(req, analysis, mock_constraints)

        # Issue #607 step 3: design + implement only (no Test task).
        assert len(tasks) == 2
        assert tasks[0]["id"] == "task_user_authentication_design"
        assert tasks[0]["name"] == "Design User Authentication"
        assert all(t["type"] != "testing" for t in tasks)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_break_down_epic_with_complex_feature_name(
        self, parser, mock_constraints
    ):
        """Test epic breakdown with complex feature names containing special chars"""
        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="agile",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        req = {"feature": "CRUD operations for todos: Create, Read, Update, Delete"}
        tasks = await parser._break_down_epic(req, analysis, mock_constraints)

        # Issue #607 step 3: design + implement only (no Test task).
        assert len(tasks) == 2
        # Should clean up the ID properly
        assert "task_crud_operations_todos_create_read_update_delete" in tasks[0]["id"]
        assert (
            tasks[0]["name"]
            == "Design CRUD operations for todos: Create, Read, Update, Delete"
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_break_down_epic_with_common_words(self, parser, mock_constraints):
        """Test that common words are removed from IDs"""
        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="agile",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        req = {"feature": "User authentication with JWT tokens and OAuth"}
        tasks = await parser._break_down_epic(req, analysis, mock_constraints)

        # Issue #607 step 3: design + implement only (no Test task).
        assert len(tasks) == 2
        # Common words like 'with' and 'and' should be removed
        assert "task_user_authentication_jwt_tokens_oauth" in tasks[0]["id"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_break_down_epic_fallback_to_index(self, parser, mock_constraints):
        """Test fallback to index when no valid feature name"""
        analysis = PRDAnalysis(
            functional_requirements=[{}, {"feature": ""}, {"description": ""}],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="agile",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        req: dict[str, Any] = {}  # Empty req
        tasks = await parser._break_down_epic(req, analysis, mock_constraints)

        # Issue #607 step 3: design + implement only (no Test task).
        assert len(tasks) == 2
        assert tasks[0]["id"] == "task_req_0_design"
        assert tasks[0]["name"] == "Design feature"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_nfr_tasks_with_requirement_field(
        self, parser, mock_constraints
    ):
        """Test NFR task creation with 'requirement' field"""
        nfrs = [{"requirement": "Performance"}, {"requirement": "Security"}]

        tasks = await parser._create_nfr_tasks(nfrs, mock_constraints)

        assert len(tasks) == 2
        assert tasks[0]["id"] == "nfr_task_performance"
        assert tasks[0]["name"] == "Implement Performance"
        assert tasks[1]["id"] == "nfr_task_security"
        assert tasks[1]["name"] == "Implement Security"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_nfr_tasks_with_description_field(
        self, parser, mock_constraints
    ):
        """Test NFR task creation with 'description' field"""
        nfrs = [{"description": "System Scalability"}, {"name": "Data Privacy"}]

        tasks = await parser._create_nfr_tasks(nfrs, mock_constraints)

        assert len(tasks) == 2
        assert tasks[0]["id"] == "nfr_task_system_scalability"
        assert tasks[0]["name"] == "Implement System Scalability"
        assert tasks[1]["id"] == "nfr_task_data_privacy"
        assert tasks[1]["name"] == "Implement Data Privacy"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_generate_task_hierarchy_robust_handling(
        self, parser, mock_constraints
    ):
        """Test task hierarchy generation with mixed requirement formats"""
        analysis = PRDAnalysis(
            functional_requirements=[
                {"feature": "CRUD Operations"},
                {"description": "User Authentication"},
                {"name": "Data Validation"},
                {},  # Empty requirement to test fallback
            ],
            non_functional_requirements=[
                {"requirement": "Performance"},
                {"description": "Security"},
            ],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="agile",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        hierarchy = await parser._generate_task_hierarchy(analysis, mock_constraints)

        # #683: all 4 functional reqs are kept (capped by tier, not team_size),
        # so 4 functional + 1 NFR + 1 infra = 6. The old count of 5 was the
        # team_size cut dropping the 4th req — not "empty req filtered out".
        assert len(hierarchy) == 6

        # Check epic IDs are properly generated
        assert "epic_crud_operations" in hierarchy
        assert "epic_user_authentication" in hierarchy
        assert "epic_data_validation" in hierarchy
        assert "epic_non_functional" in hierarchy
        assert "epic_infrastructure" in hierarchy

        # Issue #607 step 3: design + implement only (no Test task) -> 2 tasks each.
        assert len(hierarchy["epic_crud_operations"]) == 2
        assert len(hierarchy["epic_user_authentication"]) == 2
        assert len(hierarchy["epic_data_validation"]) == 2
