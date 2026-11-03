"""
Unit tests for ``dev-tools/experiments/runners/run_experiment.py``.

``conftest.py`` puts ``dev-tools/experiments/`` on ``sys.path`` so
``runners`` imports as a normal package.
"""

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from runners import run_experiment

pytestmark = pytest.mark.unit


class TestCreateExperimentStructureShutilShadowing:
    """``create_experiment_structure`` must not shadow module-level shutil.

    Python's scoping rule treats any name assigned in a function
    (including via ``import``) as local for the entire function. A
    previous version of the runner had ``import shutil`` inside a
    conditional block, which made the earlier ``shutil.copy`` call at
    the top of the same function raise ``UnboundLocalError`` whenever
    the function executed at all — fully breaking ``--init`` mode.

    Regression: when a stale ``.git`` exists at the experiment root,
    the function must clean it up using the module-level ``shutil`` and
    keep going, not crash before getting there.
    """

    def test_init_succeeds_when_stale_git_present(
        self,
        tmp_path: Path,
    ) -> None:
        """A pre-existing ``.git`` at the experiment root does not crash --init.

        Builds an experiment directory containing a stale ``.git`` so
        the function's cleanup branch fires, supplies a templates dir
        with the bundled config, and stubs the ``git`` / ``claude``
        subprocess calls because they would otherwise reach out to
        external binaries the CI runner may not have.
        """
        # Arrange — stale .git that must be removed by shutil.rmtree.
        experiment_dir = tmp_path / "experiment"
        experiment_dir.mkdir()
        stale_git = experiment_dir / ".git"
        stale_git.mkdir()
        (stale_git / "HEAD").write_text("ref: refs/heads/main\n")

        # Templates dir with a minimal config.yaml.template the function
        # copies in.
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "config.yaml.template").write_text(
            "project_name: 'test'\nagents: []\n"
        )

        # Stub subprocess.run so ``git init`` / ``claude mcp list`` are
        # no-ops on hosts that lack those binaries.
        with patch.object(
            (
                run_experiment.subprocess
                if hasattr(run_experiment, "subprocess")
                else subprocess
            ),
            "run",
            return_value=type("R", (), {"returncode": 0, "stdout": "marcus"})(),
        ):
            # Act — must not raise UnboundLocalError on shutil.
            run_experiment.create_experiment_structure(experiment_dir, templates_dir)

        # Assert — stale .git was actually removed and the templated
        # config.yaml landed in place.
        assert not stale_git.exists(), "Stale .git should have been rmtree'd"
        assert (
            experiment_dir / "config.yaml"
        ).exists(), "config.yaml should have been copied from the template"


def _experiment(tmp_path: Path) -> Path:
    """Lay out a valid experiment directory."""
    experiment = tmp_path / "exp"
    experiment.mkdir()
    (experiment / "config.yaml").write_text(
        "project_name: 'tiny'\nproject_spec_file: project_spec.md\n"
        "agents:\n  - id: a1\n    name: A1\n  - id: a2\n    name: A2\n"
        "  - id: a3\n    name: A3\n"
    )
    (experiment / "project_spec.md").write_text("Build a tiny app.")
    return experiment


class TestMainIsSessionOnly:
    """A plain run is the session launcher; there is no other mode."""

    def test_sessions_default_from_config(self, tmp_path: Path) -> None:
        """No --sessions: the count comes from config.yaml (here, three agents)."""
        experiment = _experiment(tmp_path)
        launcher = type("L", (), {"run": lambda self: True})
        with (
            patch.object(
                run_experiment.sys, "argv", ["run_experiment.py", str(experiment)]
            ),
            patch.object(run_experiment, "SessionLauncher") as launcher_cls,
            pytest.raises(SystemExit) as exit_info,
        ):
            launcher_cls.return_value = launcher()
            run_experiment.main()
        assert exit_info.value.code == 0
        assert launcher_cls.call_args.kwargs["sessions"] == 3

    def test_sessions_flag_overrides_config(self, tmp_path: Path) -> None:
        """--sessions N wins over the config count."""
        experiment = _experiment(tmp_path)
        with (
            patch.object(
                run_experiment.sys,
                "argv",
                ["run_experiment.py", str(experiment), "--sessions", "1"],
            ),
            patch.object(run_experiment, "SessionLauncher") as launcher_cls,
            pytest.raises(SystemExit),
        ):
            launcher_cls.return_value.run.return_value = True
            run_experiment.main()
        assert launcher_cls.call_args.kwargs["sessions"] == 1

    def test_failed_run_exits_nonzero(self, tmp_path: Path) -> None:
        """The launcher's False becomes exit code 1."""
        experiment = _experiment(tmp_path)
        with (
            patch.object(
                run_experiment.sys, "argv", ["run_experiment.py", str(experiment)]
            ),
            patch.object(run_experiment, "SessionLauncher") as launcher_cls,
            pytest.raises(SystemExit) as exit_info,
        ):
            launcher_cls.return_value.run.return_value = False
            run_experiment.main()
        assert exit_info.value.code == 1

    def test_no_spawn_loop_remains(self) -> None:
        """The runner module no longer knows about a spawner."""
        assert not hasattr(run_experiment, "AgentSpawner")


class TestMainSetupModes:
    """``--init``, ``--validate``, and the failure exits around them."""

    def _templates(self, tmp_path: Path) -> Path:
        """Create a templates dir next to a fake ``runners/`` and return it."""
        templates = tmp_path / "experiments" / "templates"
        templates.mkdir(parents=True)
        (templates / "config.yaml.template").write_text(
            "project_name: 'x'\nproject_spec_file: project_spec.md\nagents: []\n"
        )
        (tmp_path / "experiments" / "runners").mkdir()
        return templates

    def _run_main(self, argv: list[str], templates: Path) -> int:
        """Run ``main()`` with the module believing it lives beside ``templates``."""
        fake_module = templates.parent / "runners" / "run_experiment.py"
        with (
            patch.object(run_experiment.sys, "argv", ["run_experiment.py", *argv]),
            patch.object(run_experiment, "__file__", str(fake_module)),
            pytest.raises(SystemExit) as exit_info,
        ):
            run_experiment.main()
        return int(exit_info.value.code or 0)

    def test_init_lays_out_the_directory_and_exits_zero(self, tmp_path: Path) -> None:
        """--init copies the templates, creates the subdirectories, and stops."""
        templates = self._templates(tmp_path)
        experiment = tmp_path / "new-exp"
        with patch(
            "subprocess.run",
            return_value=type("R", (), {"returncode": 0, "stdout": "marcus"})(),
        ):
            code = self._run_main(["--init", str(experiment)], templates)
        assert code == 0
        assert (experiment / "config.yaml").exists()
        assert (experiment / "project_spec.md").exists()
        for sub in ("prompts", "logs", "implementation", "worktrees"):
            assert (experiment / sub).is_dir()

    def test_missing_directory_exits_one(self, tmp_path: Path) -> None:
        """A run against a directory that does not exist is refused."""
        templates = self._templates(tmp_path)
        code = self._run_main([str(tmp_path / "nope")], templates)
        assert code == 1

    def test_validate_passes_on_a_complete_experiment(self, tmp_path: Path) -> None:
        """--validate exits zero when config.yaml and the spec are present."""
        templates = self._templates(tmp_path)
        experiment = _experiment(tmp_path)
        assert self._run_main(["--validate", str(experiment)], templates) == 0

    def test_validate_fails_when_spec_is_missing(self, tmp_path: Path) -> None:
        """--validate exits one and names the missing spec."""
        templates = self._templates(tmp_path)
        experiment = _experiment(tmp_path)
        (experiment / "project_spec.md").unlink()
        assert self._run_main(["--validate", str(experiment)], templates) == 1

    def test_validate_fails_on_unreadable_config(self, tmp_path: Path) -> None:
        """A config.yaml that does not parse is a validation error."""
        experiment = tmp_path / "exp"
        experiment.mkdir()
        (experiment / "config.yaml").write_text("project_name: [unclosed\n")
        assert run_experiment.validate_experiment(experiment) is False

    def test_validate_fails_without_config(self, tmp_path: Path) -> None:
        """No config.yaml at all is a validation error."""
        experiment = tmp_path / "exp"
        experiment.mkdir()
        assert run_experiment.validate_experiment(experiment) is False


class TestSeedOption:
    """``--seed tasks.yaml`` puts the explicit task list into project_options."""

    def test_load_seed_tasks_accepts_a_list_or_a_tasks_mapping(
        self, tmp_path: Path
    ) -> None:
        """Both top-level shapes are accepted."""
        f = tmp_path / "s.yaml"
        f.write_text("- name: A\n- name: B\n  depends_on: [A]\n")
        assert [t["name"] for t in run_experiment.load_seed_tasks(f)] == ["A", "B"]
        f.write_text("tasks:\n  - name: Only\n")
        assert run_experiment.load_seed_tasks(f) == [{"name": "Only"}]

    def test_load_seed_tasks_refuses_bad_files(self, tmp_path: Path) -> None:
        """Missing, malformed YAML, or not-a-list all raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            run_experiment.load_seed_tasks(tmp_path / "missing.yaml")
        f = tmp_path / "s.yaml"
        f.write_text("tasks: 3\n")
        with pytest.raises(ValueError, match="non-empty list"):
            run_experiment.load_seed_tasks(f)
        f.write_text("- [unclosed\n")
        with pytest.raises(ValueError, match="not valid YAML"):
            run_experiment.load_seed_tasks(f)

    def test_seed_flag_lands_in_project_options(self, tmp_path: Path) -> None:
        """main() passes the seed list to the launcher through project_options.tasks."""
        experiment = _experiment(tmp_path)
        seed = tmp_path / "seed.yaml"
        seed.write_text("- name: Implement add_one_plus_one\n  labels: [implement]\n")
        with (
            patch.object(
                run_experiment.sys,
                "argv",
                ["run_experiment.py", str(experiment), "--seed", str(seed)],
            ),
            patch.object(run_experiment, "SessionLauncher") as launcher_cls,
            pytest.raises(SystemExit),
        ):
            launcher_cls.return_value.run.return_value = True
            run_experiment.main()
        config = launcher_cls.call_args.args[0]
        assert config.project_options["tasks"] == [
            {"name": "Implement add_one_plus_one", "labels": ["implement"]}
        ]

    def test_example_seed_file_is_valid(self) -> None:
        """The shipped example passes the same validation Marcus applies."""
        from src.integrations.seeded_tasks import validate_seed_specs

        example = (
            Path(__file__).resolve().parents[3]
            / "dev-tools"
            / "experiments"
            / "templates"
            / "seed_tasks.example.yaml"
        )
        specs = run_experiment.load_seed_tasks(example)
        assert validate_seed_specs(specs) == []
        assert len(specs) == 2
