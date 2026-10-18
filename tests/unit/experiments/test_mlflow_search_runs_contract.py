"""
Unit test pinning the ``search_runs`` experiment_ids contract.

``MarcusExperiment.compare_runs`` passed a bare ``experiment.experiment_id``
to ``MlflowClient.search_runs``, whose signature is
``experiment_ids: list[str]``. A string is iterable, so mlflow would have
treated it as a sequence of single-character experiment IDs rather than
one ID — returning nothing, or querying IDs that do not exist.

It went unnoticed because ``compare_runs`` has exactly one caller
(``generate_report``) which itself has none, so the path is currently
unreached. CI caught it only when the unpinned ``mlflow>=2.10.0``
dependency drifted to 3.16.0 and mypy began seeing the annotation.

This test pins the call shape so the fix cannot silently regress, and so
the contract is checked without needing the path to be reachable.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


class TestSearchRunsReceivesAList:
    """experiment_ids is a list, not a bare string."""

    def test_compare_runs_wraps_the_experiment_id(self) -> None:
        """The single experiment id is passed as a one-element list.

        Passing the bare string makes mlflow iterate its characters.
        """
        from src.experiments.mlflow_tracker import MarcusExperiment

        tracker = MarcusExperiment.__new__(MarcusExperiment)
        tracker.experiment_name = "marcus-test"

        client = MagicMock()
        experiment = MagicMock()
        experiment.experiment_id = "42"
        client.get_experiment_by_name.return_value = experiment
        client.search_runs.return_value = []
        tracker.client = client

        tracker.compare_runs()

        client.search_runs.assert_called_once()
        passed = client.search_runs.call_args.args[0]
        assert passed == ["42"], (
            f"experiment_ids must be a list of ids; got {passed!r}. "
            "A bare string is iterated character-by-character by mlflow."
        )
