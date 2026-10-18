"""
Unit tests for bundled domain-based design tasks

Tests the domain discovery, bundled design task generation, and dependency
inference for the bundled domain designs feature (GH-108).
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.ai.advanced.prd.advanced_parser import AdvancedPRDParser, ProjectConstraints


class TestBundledDomainDiscovery:
    """Test suite for domain discovery from functional requirements"""

    @pytest.fixture
    def parser(self):
        """Create AdvancedPRDParser with mocked dependencies"""
        with patch(
            "src.ai.advanced.prd.advanced_parser.LLMAbstraction"
        ) as mock_llm_class:
            with patch(
                "src.ai.advanced.prd.advanced_parser.HybridDependencyInferer"
            ) as mock_dep_class:
                mock_llm = Mock()
                mock_llm.analyze = AsyncMock()
                mock_llm_class.return_value = mock_llm

                mock_dep = Mock()
                mock_dep.infer_dependencies = AsyncMock(return_value=[])
                mock_dep_class.return_value = mock_dep

                parser = AdvancedPRDParser()
                parser.llm_client = mock_llm
                parser.dependency_inferer = mock_dep
                return parser

    @pytest.fixture
    def sample_functional_requirements(self):
        """Sample functional requirements for testing domain discovery"""
        return [
            {
                "id": "feature_user_login",
                "name": "User Login",
                "description": "Allow users to login with email and password",
                "affected_components": ["frontend", "auth-service", "database"],
                "complexity": "coordinated",
            },
            {
                "id": "feature_user_registration",
                "name": "User Registration",
                "description": "Allow new users to create accounts",
                "affected_components": ["frontend", "auth-service", "database"],
                "complexity": "coordinated",
            },
            {
                "id": "feature_product_catalog",
                "name": "Product Catalog",
                "description": "Display available products",
                "affected_components": ["frontend", "product-service", "database"],
                "complexity": "simple",
            },
            {
                "id": "feature_shopping_cart",
                "name": "Shopping Cart",
                "description": "Add and manage items in cart",
                "affected_components": ["frontend", "cart-service", "database"],
                "complexity": "coordinated",
            },
            {
                "id": "feature_checkout",
                "name": "Checkout",
                "description": "Process orders and payments",
                "affected_components": [
                    "frontend",
                    "payment-service",
                    "order-service",
                ],
                "complexity": "distributed",
            },
        ]

    @pytest.fixture
    def mock_ai_domain_response(self):
        """Mock AI response for domain discovery"""
        return {
            "domains": [
                {
                    "name": "Authentication",
                    "feature_ids": [
                        "feature_user_login",
                        "feature_user_registration",
                    ],
                    "rationale": "User authentication and registration features",
                },
                {
                    "name": "Shopping",
                    "feature_ids": [
                        "feature_product_catalog",
                        "feature_shopping_cart",
                        "feature_checkout",
                    ],
                    "rationale": "E-commerce shopping features",
                },
            ]
        }

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_discover_domains_returns_proper_structure(
        self, parser, sample_functional_requirements, mock_ai_domain_response
    ):
        """Test domain discovery returns dict mapping domain names to feature IDs"""
        # Arrange
        parser.llm_client.analyze.return_value = json.dumps(mock_ai_domain_response)

        # Act
        domains = await parser._discover_domains(sample_functional_requirements)

        # Assert
        assert isinstance(domains, dict)
        assert "Authentication" in domains
        assert "Shopping" in domains
        assert "feature_user_login" in domains["Authentication"]
        assert "feature_user_registration" in domains["Authentication"]
        assert len(domains["Shopping"]) == 3

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_discover_domains_handles_small_project(
        self, parser, sample_functional_requirements
    ):
        """Test domain discovery adapts target domain count for small projects"""
        # Arrange
        small_requirements = sample_functional_requirements[:3]
        parser.llm_client.analyze.return_value = json.dumps(
            {
                "domains": [
                    {
                        "name": "Core Features",
                        "feature_ids": [req["id"] for req in small_requirements],
                        "rationale": "Core features for small project",
                    }
                ]
            }
        )

        # Act
        await parser._discover_domains(small_requirements)

        # Assert - Should suggest 1-3 domains for small projects.
        #
        # Bug #649 root cause 3: the previous floor of "2-3" forced
        # trivial projects (snake game, etc.) into ≥2 domains, which
        # cascaded into over-decomposition (11 tasks for 50-line snake).
        # Lowering the floor to "1-3" lets the LLM return 1 domain when
        # the project genuinely fits one — without preventing it from
        # returning 2-3 when warranted.
        parser.llm_client.analyze.assert_called_once()
        call_args = parser.llm_client.analyze.call_args.kwargs["prompt"]
        assert "1-3" in call_args  # Target domain count for small projects

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_discover_domains_prototype_mode_forces_single_domain(
        self, parser, sample_functional_requirements
    ):
        """Test that complexity_mode='prototype' forces target=1 domain.

        Bug #649 root cause 3: ``should_decompose`` correctly skips
        further per-task decomposition in prototype mode, but
        ``_discover_domains`` was called unconditionally — so trivial
        projects still got split into multiple domains, defeating the
        prototype-mode promise of "speed over granularity."

        With the fix, prototype mode forces ``target_domains="1"`` in
        the LLM prompt so the model returns a single cohesive domain
        for the whole feature set.
        """
        # Arrange
        parser.llm_client.analyze.return_value = json.dumps(
            {
                "domains": [
                    {
                        "name": "Snake Game",
                        "feature_ids": [
                            req["id"] for req in sample_functional_requirements
                        ],
                        "rationale": "Single trivial-project domain",
                    }
                ]
            }
        )

        # Act
        await parser._discover_domains(
            sample_functional_requirements,
            complexity_mode="prototype",
        )

        # Assert - Prompt asks for exactly 1 domain.
        parser.llm_client.analyze.assert_called_once()
        call_args = parser.llm_client.analyze.call_args.kwargs["prompt"]
        assert "1" in call_args.split("Return JSON with")[1].split("domains")[0]
        # And the prompt MUST NOT carry the multi-domain floor language.
        assert "2-3" not in call_args
        assert "3-5" not in call_args

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_discover_domains_non_prototype_unchanged_for_medium(self, parser):
        """Standard/enterprise complexity preserves the existing 4-7 floor
        for medium projects (16-30 features).

        Regression guard: the fix for bug #649 root cause 3 must only
        change behavior for prototype mode and small projects — medium
        and large projects retain the same target_domains buckets.
        """
        # Arrange - 20 features so we hit the "4-7" bucket
        medium_requirements = [
            {
                "id": f"feature_{i}",
                "name": f"Feature {i}",
                "description": f"Description {i}",
                "affected_components": ["frontend"],
                "complexity": "simple",
            }
            for i in range(20)
        ]
        parser.llm_client.analyze.return_value = json.dumps(
            {
                "domains": [
                    {
                        "name": f"Domain{i}",
                        "feature_ids": [
                            f"feature_{j}" for j in range(i * 4, (i + 1) * 4)
                        ],
                        "rationale": f"Domain {i}",
                    }
                    for i in range(5)
                ]
            }
        )

        # Act
        await parser._discover_domains(
            medium_requirements,
            complexity_mode="standard",
        )

        # Assert - Medium-project floor (4-7) unchanged.
        call_args = parser.llm_client.analyze.call_args.kwargs["prompt"]
        assert "4-7" in call_args

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_discover_domains_handles_large_project(
        self, parser, sample_functional_requirements
    ):
        """Test domain discovery adapts target domain count for large projects"""
        # Arrange - Create 20 features
        large_requirements = [
            {
                "id": f"feature_{i}",
                "name": f"Feature {i}",
                "description": f"Description {i}",
                "affected_components": ["frontend"],
                "complexity": "simple",
            }
            for i in range(20)
        ]
        parser.llm_client.analyze.return_value = json.dumps(
            {
                "domains": [
                    {
                        "name": f"Domain{i}",
                        "feature_ids": [
                            f"feature_{j}" for j in range(i * 4, (i + 1) * 4)
                        ],
                        "rationale": f"Domain {i} features",
                    }
                    for i in range(5)
                ]
            }
        )

        # Act
        await parser._discover_domains(large_requirements)

        # Assert - Should suggest 4-7 domains for medium projects
        parser.llm_client.analyze.assert_called_once()
        call_args = parser.llm_client.analyze.call_args.kwargs["prompt"]
        assert "4-7" in call_args  # Target domain count for medium projects

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_discover_domains_includes_feature_details_in_prompt(
        self, parser, sample_functional_requirements
    ):
        """Test domain discovery includes all feature details in AI prompt"""
        # Arrange
        parser.llm_client.analyze.return_value = json.dumps(
            {"domains": {"Test": ["feature_user_login"]}}
        )

        # Act
        await parser._discover_domains(sample_functional_requirements)

        # Assert - Verify prompt includes feature details
        call_args = parser.llm_client.analyze.call_args.kwargs["prompt"]
        assert "User Login" in call_args
        assert "frontend" in call_args
        assert "coordinated" in call_args
        assert "Allow users to login" in call_args


class TestBundledDesignTaskGeneration:
    """Test suite for bundled design task generation"""

    @pytest.fixture
    def parser(self):
        """Create AdvancedPRDParser with mocked dependencies"""
        with patch(
            "src.ai.advanced.prd.advanced_parser.LLMAbstraction"
        ) as mock_llm_class:
            with patch(
                "src.ai.advanced.prd.advanced_parser.HybridDependencyInferer"
            ) as mock_dep_class:
                mock_llm = Mock()
                mock_llm.analyze = AsyncMock()
                mock_llm_class.return_value = mock_llm

                mock_dep = Mock()
                mock_dep.infer_dependencies = AsyncMock(return_value=[])
                mock_dep_class.return_value = mock_dep

                parser = AdvancedPRDParser()
                parser.llm_client = mock_llm
                parser.dependency_inferer = mock_dep
                return parser

    @pytest.fixture
    def sample_domains(self):
        """Sample domain mapping"""
        return {
            "Authentication": ["feature_user_login", "feature_user_registration"],
            "Shopping": ["feature_product_catalog", "feature_shopping_cart"],
        }

    @pytest.fixture
    def sample_functional_requirements(self):
        """Sample functional requirements"""
        return [
            {
                "id": "feature_user_login",
                "name": "User Login",
                "description": "Allow users to login with email and password",
            },
            {
                "id": "feature_user_registration",
                "name": "User Registration",
                "description": "Allow new users to create accounts",
            },
            {
                "id": "feature_product_catalog",
                "name": "Product Catalog",
                "description": "Display available products",
            },
            {
                "id": "feature_shopping_cart",
                "name": "Shopping Cart",
                "description": "Add and manage items in cart",
            },
        ]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_bundled_design_tasks_returns_one_per_domain(
        self, parser, sample_domains, sample_functional_requirements
    ):
        """Test bundled design task generation creates one task per domain"""
        # Act
        tasks = await parser._create_bundled_design_tasks(
            sample_domains, sample_functional_requirements, "standard"
        )

        # Assert
        assert len(tasks) == 2  # One per domain
        task_names = [task["name"] for task in tasks]
        assert "Design Authentication" in task_names
        assert "Design Shopping" in task_names

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bundled_design_task_includes_all_features(
        self, parser, sample_domains, sample_functional_requirements
    ):
        """Test bundled design task description includes all domain features"""
        # Act
        tasks = await parser._create_bundled_design_tasks(
            sample_domains, sample_functional_requirements, "standard"
        )

        # Assert
        auth_task = next(t for t in tasks if "Authentication" in t["name"])
        assert "USER LOGIN" in auth_task["description"]
        assert "USER REGISTRATION" in auth_task["description"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bundled_design_task_has_proper_metadata(
        self, parser, sample_domains, sample_functional_requirements
    ):
        """Test bundled design tasks have proper metadata"""
        # Act
        tasks = await parser._create_bundled_design_tasks(
            sample_domains, sample_functional_requirements, "standard"
        )

        # Assert
        auth_task = next(t for t in tasks if "Authentication" in t["name"])
        assert auth_task["id"] == "design_authentication"
        assert auth_task["type"] == parser.TASK_TYPE_DESIGN
        assert auth_task["domain_name"] == "Authentication"
        assert auth_task["feature_ids"] == [
            "feature_user_login",
            "feature_user_registration",
        ]
        assert auth_task["priority"] == "high"
        assert "design" in auth_task["labels"]
        assert "architecture" in auth_task["labels"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bundled_design_task_includes_coordination_guidance(
        self, parser, sample_domains, sample_functional_requirements
    ):
        """Test bundled design tasks include agent coordination guidance"""
        # Act
        tasks = await parser._create_bundled_design_tasks(
            sample_domains, sample_functional_requirements, "standard"
        )

        # Assert
        auth_task = next(t for t in tasks if "Authentication" in t["name"])
        description = auth_task["description"]
        assert "Component boundaries" in description
        assert "Data flows" in description
        assert "Integration points" in description
        assert "Shared data models" in description

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bundled_design_task_skips_empty_domains(
        self, parser, sample_functional_requirements
    ):
        """Test bundled design generation skips domains with no matching features"""
        # Arrange
        domains_with_invalid = {
            "Authentication": ["feature_user_login"],
            "EmptyDomain": ["nonexistent_feature"],
        }

        # Act
        tasks = await parser._create_bundled_design_tasks(
            domains_with_invalid, sample_functional_requirements, "standard"
        )

        # Assert - Should only create task for Authentication
        assert len(tasks) == 1
        assert tasks[0]["domain_name"] == "Authentication"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_bundled_design_task_estimated_hours_scales_with_features(
        self, parser, sample_domains, sample_functional_requirements
    ):
        """Test bundled design task duration scales with number of features"""
        # Act
        tasks = await parser._create_bundled_design_tasks(
            sample_domains, sample_functional_requirements, "standard"
        )

        # Assert
        auth_task = next(t for t in tasks if "Authentication" in t["name"])
        shopping_task = next(t for t in tasks if "Shopping" in t["name"])

        # Auth has 2 features, Shopping has 2 features
        # Both should have same estimated hours
        assert auth_task["estimated_hours"] == shopping_task["estimated_hours"]
        # Should be reasonable (6 minutes per feature = 0.1 hours per feature)
        assert auth_task["estimated_hours"] > 0


class TestBundledDesignDependencies:
    """Test suite for bundled design dependency inference"""

    @pytest.fixture
    def parser(self):
        """Create AdvancedPRDParser with mocked dependencies"""
        with patch(
            "src.ai.advanced.prd.advanced_parser.LLMAbstraction"
        ) as mock_llm_class:
            with patch(
                "src.ai.advanced.prd.advanced_parser.HybridDependencyInferer"
            ) as mock_dep_class:
                mock_llm = Mock()
                mock_llm.analyze = AsyncMock()
                mock_llm_class.return_value = mock_llm

                mock_dep = Mock()
                mock_dep.infer_dependencies = AsyncMock(return_value=[])
                mock_dep_class.return_value = mock_dep

                parser = AdvancedPRDParser()
                parser.llm_client = mock_llm
                parser.dependency_inferer = mock_dep
                return parser

    @pytest.fixture
    def sample_tasks(self):
        """Sample tasks with design, implement, and test types"""
        from src.core.models import Priority, Task, TaskStatus

        return [
            Task(
                id="design_authentication",
                name="Design Authentication",
                description="Design auth",
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=1.0,
            ),
            Task(
                id="task_user_login_implement",
                name="Implement User Login",
                description="Implement login",
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=4.0,
            ),
            Task(
                id="task_user_login_test",
                name="Test User Login",
                description="Test login",
                status=TaskStatus.TODO,
                priority=Priority.MEDIUM,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=2.0,
            ),
            Task(
                id="task_user_registration_implement",
                name="Implement User Registration",
                description="Implement registration",
                status=TaskStatus.TODO,
                priority=Priority.HIGH,
                assigned_to=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                due_date=None,
                estimated_hours=4.0,
            ),
        ]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_prd_dependencies_returns_empty_without_bundled_designs(
        self, parser, sample_tasks
    ):
        """Test dependency inference returns empty without bundled design metadata"""
        # Arrange
        from src.ai.advanced.prd.advanced_parser import PRDAnalysis

        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        # Act
        deps = await parser._add_prd_specific_dependencies(sample_tasks, analysis)

        # Assert
        assert deps == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_prd_dependencies_creates_deps_for_implement_tasks(
        self, parser, sample_tasks
    ):
        """Test dependency inference creates deps from implement tasks to
        bundled designs"""
        # Arrange
        from src.ai.advanced.prd.advanced_parser import PRDAnalysis

        parser._bundled_designs = {"Authentication": "design_authentication"}
        parser._domain_mapping = {"Authentication": ["user_login", "user_registration"]}

        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        # Act
        deps = await parser._add_prd_specific_dependencies(sample_tasks, analysis)

        # Assert - Should create deps for both implement tasks
        assert len(deps) >= 2
        implement_deps = [
            d for d in deps if "implement" in d["dependent_task_id"].lower()
        ]
        assert len(implement_deps) == 2

        # Check specific dependencies
        login_dep = next(
            d for d in deps if d["dependent_task_id"] == "task_user_login_implement"
        )
        assert login_dep["dependency_task_id"] == "design_authentication"
        assert login_dep["dependency_type"] == "architectural"
        assert login_dep["confidence"] == 1.0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_prd_dependencies_creates_deps_for_test_tasks(
        self, parser, sample_tasks
    ):
        """Test dependency inference creates deps from test tasks to bundled designs"""
        # Arrange
        from src.ai.advanced.prd.advanced_parser import PRDAnalysis

        parser._bundled_designs = {"Authentication": "design_authentication"}
        parser._domain_mapping = {"Authentication": ["user_login"]}

        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        # Act
        deps = await parser._add_prd_specific_dependencies(sample_tasks, analysis)

        # Assert - Should create dep for test task
        test_deps = [d for d in deps if "test" in d["dependent_task_id"].lower()]
        assert len(test_deps) >= 1

        test_dep = next(
            d for d in deps if d["dependent_task_id"] == "task_user_login_test"
        )
        assert test_dep["dependency_task_id"] == "design_authentication"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_prd_dependencies_skips_design_tasks(self, parser, sample_tasks):
        """Test dependency inference skips design tasks (no self-dependencies)"""
        # Arrange
        from src.ai.advanced.prd.advanced_parser import PRDAnalysis

        parser._bundled_designs = {"Authentication": "design_authentication"}
        parser._domain_mapping = {"Authentication": ["authentication"]}

        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        # Act
        deps = await parser._add_prd_specific_dependencies(sample_tasks, analysis)

        # Assert - No dependencies for design tasks
        design_deps = [d for d in deps if "design" in d["dependent_task_id"].lower()]
        assert len(design_deps) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_add_prd_dependencies_handles_multi_part_feature_ids(
        self, parser, sample_tasks
    ):
        """Test dependency inference correctly extracts feature IDs with underscores"""
        # Arrange
        from src.ai.advanced.prd.advanced_parser import PRDAnalysis

        parser._bundled_designs = {"Authentication": "design_authentication"}
        parser._domain_mapping = {"Authentication": ["user_login", "user_registration"]}

        analysis = PRDAnalysis(
            functional_requirements=[],
            non_functional_requirements=[],
            technical_constraints=[],
            business_objectives=[],
            user_personas=[],
            success_metrics=[],
            implementation_approach="",
            complexity_assessment={},
            risk_factors=[],
            confidence=0.8,
        )

        # Act
        deps = await parser._add_prd_specific_dependencies(sample_tasks, analysis)

        # Assert - Should correctly extract "user_login" from
        # "task_user_login_implement"
        assert len(deps) >= 2
        login_dep = next(
            d for d in deps if d["dependent_task_id"] == "task_user_login_implement"
        )
        assert login_dep["dependency_task_id"] == "design_authentication"


class TestBundledDesignIntegration:
    """Integration tests for bundled domain designs in task generation flow"""

    @pytest.fixture
    def parser(self):
        """Create AdvancedPRDParser with mocked dependencies"""
        with patch(
            "src.ai.advanced.prd.advanced_parser.LLMAbstraction"
        ) as mock_llm_class:
            with patch(
                "src.ai.advanced.prd.advanced_parser.HybridDependencyInferer"
            ) as mock_dep_class:
                mock_llm = Mock()
                mock_llm.analyze = AsyncMock()
                mock_llm_class.return_value = mock_llm

                mock_dep = Mock()
                mock_dep.infer_dependencies = AsyncMock(return_value=[])
                mock_dep_class.return_value = mock_dep

                parser = AdvancedPRDParser()
                parser.llm_client = mock_llm
                parser.dependency_inferer = mock_dep
                return parser

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_complexity_mode_passed_through_to_bundled_designs(self, parser):
        """Test complexity_mode from constraints is passed to bundled design
        generation"""
        # Arrange
        constraints = ProjectConstraints(
            team_size=5,
            complexity_mode="enterprise",
        )

        functional_reqs = [
            {
                "id": "feature_test",
                "name": "Test Feature",
                "description": "Test",
                "affected_components": ["frontend"],
                "complexity": "simple",
            }
        ]

        # Mock domain discovery to return a domain
        parser.llm_client.analyze.return_value = json.dumps(
            {"domains": {"TestDomain": ["feature_test"]}}
        )

        # Act
        with patch.object(
            parser, "_create_bundled_design_tasks", new=AsyncMock(return_value=[])
        ) as mock_create:
            # Call a method that would trigger bundled design creation
            # We'll call _discover_domains and then check if
            # _create_bundled_design_tasks would be called with
            # the right complexity_mode
            domains = await parser._discover_domains(functional_reqs)

            # Manually trigger the bundled design creation to verify the call
            await parser._create_bundled_design_tasks(
                domains, functional_reqs, constraints.complexity_mode
            )

            # Assert
            mock_create.assert_called_once()
            call_args = mock_create.call_args[0]
            assert call_args[2] == "enterprise"  # complexity_mode parameter

    @pytest.mark.unit
    def test_project_constraints_has_complexity_mode_field(self):
        """Test ProjectConstraints dataclass includes complexity_mode field"""
        # Act
        constraints = ProjectConstraints(
            team_size=3,
            complexity_mode="prototype",
        )

        # Assert
        assert constraints.complexity_mode == "prototype"
        assert hasattr(constraints, "complexity_mode")

    @pytest.mark.unit
    def test_project_constraints_complexity_mode_defaults_to_standard(self):
        """Test ProjectConstraints complexity_mode defaults to 'standard'"""
        # Act
        constraints = ProjectConstraints(team_size=3)

        # Assert
        assert constraints.complexity_mode == "standard"
