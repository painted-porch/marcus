"""
Unit tests for WorkspaceManager
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.core.workspace import WorkspaceConfig, WorkspaceManager, WorkspaceSecurityError


class TestWorkspaceManager:
    """Test suite for WorkspaceManager"""

    def test_marcus_root_detection(self):
        """Test that Marcus root is correctly detected"""
        manager = WorkspaceManager()

        # Should detect the Marcus root directory (validates by structure, not name)
        # The root must exist and should contain expected marker files/directories
        assert os.path.exists(manager.marcus_root)
        assert os.path.isdir(manager.marcus_root)

        # Check for at least one expected marker (src directory should always exist)
        assert os.path.exists(os.path.join(manager.marcus_root, "src"))

        # Marcus root should be in forbidden paths
        assert manager.marcus_root in manager.forbidden_paths

    def test_forbidden_paths_initialization(self):
        """Test that forbidden paths are properly initialized"""
        manager = WorkspaceManager()

        # Marcus root should be forbidden
        assert manager.marcus_root in manager.forbidden_paths

        # Should have multiple forbidden paths (including system paths)
        assert len(manager.forbidden_paths) >= 1

    def test_path_validation_forbidden(self):
        """Test that forbidden paths are rejected"""
        manager = WorkspaceManager()

        # Try to access Marcus directory
        with pytest.raises(WorkspaceSecurityError):
            manager.validate_path(manager.marcus_root)

        # Try to access subdirectory of Marcus
        marcus_subdir = os.path.join(manager.marcus_root, "src", "core")
        with pytest.raises(WorkspaceSecurityError):
            manager.validate_path(marcus_subdir)

    def test_path_validation_allowed(self):
        """Test that allowed paths pass validation"""
        manager = WorkspaceManager()

        # Create a temporary directory outside Marcus
        with tempfile.TemporaryDirectory() as tmpdir:
            # This should be allowed
            validated_path = manager.validate_path(tmpdir)
            assert validated_path == os.path.abspath(tmpdir)

            # Check with is_path_allowed
            assert manager.is_path_allowed(tmpdir)

    def test_workspace_assignment(self):
        """Test agent workspace assignment"""
        manager = WorkspaceManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Assign workspace to agent
            workspace = manager.assign_agent_workspace("agent1", tmpdir)

            assert workspace.agent_id == "agent1"
            assert workspace.path == os.path.abspath(tmpdir)
            assert workspace.workspace_id == "agent1_workspace"

            # Should be retrievable
            retrieved = manager.get_agent_workspace("agent1")
            assert retrieved == workspace

    def test_workspace_assignment_forbidden(self):
        """Test that forbidden paths cannot be assigned as workspaces"""
        manager = WorkspaceManager()

        # Try to assign Marcus directory as workspace
        with pytest.raises(WorkspaceSecurityError):
            manager.assign_agent_workspace("agent1", manager.marcus_root)

    def test_project_workspace_config(self):
        """Test loading project workspace configuration"""
        manager = WorkspaceManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test configuration
            config = {
                "project": {
                    "workspaces": {
                        "main": tmpdir,
                        "agents": {
                            "frontend": os.path.join(tmpdir, "frontend"),
                            "backend": os.path.join(tmpdir, "backend"),
                        },
                    }
                }
            }

            # Write config to file
            config_path = os.path.join(tmpdir, "test_config.json")
            with open(config_path, "w") as f:
                json.dump(config, f)

            # Load configuration
            manager.load_config(config_path)

            assert manager.project_config is not None
            assert manager.project_config.main_workspace == os.path.abspath(tmpdir)
            assert len(manager.project_config.agent_workspaces) == 2

    def test_project_workspace_overlap_detection(self):
        """Test that overlapping workspaces with forbidden paths are detected"""
        manager = WorkspaceManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create config that tries to use Marcus directory
            config = {
                "project": {
                    "workspaces": {
                        "main": manager.marcus_root,  # Forbidden!
                        "agents": {},
                    }
                }
            }

            config_path = os.path.join(tmpdir, "bad_config.json")
            with open(config_path, "w") as f:
                json.dump(config, f)

            # Should raise security error
            with pytest.raises(WorkspaceSecurityError):
                manager.load_config(config_path)

    def test_task_assignment_data(self):
        """Test generation of task assignment data"""
        manager = WorkspaceManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Assign workspace
            manager.assign_agent_workspace("agent1", tmpdir)

            # Get task assignment data
            data = manager.get_task_assignment_data("agent1")

            assert "workspace_path" in data
            assert "forbidden_paths" in data
            assert data["workspace_path"] == os.path.abspath(tmpdir)
            assert manager.marcus_root in data["forbidden_paths"]

    def test_add_forbidden_path(self):
        """Test adding custom forbidden paths"""
        manager = WorkspaceManager()

        # Add a custom forbidden path
        custom_forbidden = "/etc/sensitive"
        manager.add_forbidden_path(custom_forbidden)

        assert os.path.abspath(custom_forbidden) in manager.forbidden_paths

        # Should be forbidden now
        with pytest.raises(WorkspaceSecurityError):
            manager.validate_path(custom_forbidden)

    def test_default_config_honors_marcus_config_env_var_first(self, tmp_path):
        """MARCUS_CONFIG must win over the repo-root config_marcus.json
        (issue #891).

        _load_default_config's own candidate list checked
        MARCUS_CONFIG LAST, after the XDG location and the repo-root
        config_marcus.json -- the opposite priority of every other
        config-loading path in this codebase
        (MarcusConfig.get_config(), the legacy ConfigLoader,
        KanbanClient._load_config). An explicit override could lose
        to an ambient repo-root file here even when both are set.

        Both the repo-root path and the MARCUS_CONFIG path "exist"
        here, with different content, so a correct priority order is
        the only way this test passes.
        """
        override_dir = tmp_path / "override"
        override_dir.mkdir()
        override_main = tmp_path / "override-workspace"
        override_main.mkdir()
        override_path = override_dir / "config_marcus.json"
        override_path.write_text(
            json.dumps({"project": {"workspaces": {"main": str(override_main)}}})
        )

        marcus_root = tmp_path / "marcus-root"
        marcus_root.mkdir()
        repo_root_main = tmp_path / "repo-root-workspace"
        repo_root_main.mkdir()
        (marcus_root / "config_marcus.json").write_text(
            json.dumps({"project": {"workspaces": {"main": str(repo_root_main)}}})
        )

        manager = WorkspaceManager.__new__(WorkspaceManager)
        manager.marcus_root = str(marcus_root)
        manager.forbidden_paths = {manager.marcus_root}
        manager.agent_workspaces = {}
        manager.project_config = None

        with patch(
            "src.core.workspace.os.environ",
            {"MARCUS_CONFIG": str(override_path)},
        ):
            manager._load_default_config()

        assert manager.project_config is not None
        assert manager.project_config.main_workspace == os.path.abspath(
            str(override_main)
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
