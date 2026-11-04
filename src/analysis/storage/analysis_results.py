"""
Analysis Result Store for Phase 2 analysis engine.

Provides persistent storage for LLM analysis results with caching, versioning,
and efficient retrieval. Prevents re-analyzing the same data multiple times.

Usage
-----
```python
store = AnalysisResultStore()

# Store analysis result
result = AnalysisResult(
    analysis_id="anl_001",
    project_id="proj-123",
    task_id="task-456",
    analysis_type="requirement_divergence",
    timestamp=datetime.now(timezone.utc),
    version="1.0",
    result={"fidelity_score": 0.85, "divergences": [...]},
)
await store.store_result(result)

# Retrieve latest result
latest = await store.get_latest_result("proj-123", "task-456", "requirement_divergence")
if latest:
    print(f"Cached result: {latest.result['fidelity_score']}")
```
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.paths import marcus_db_path
from src.core.persistence import SQLitePersistence

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """
    Result of an LLM analysis.

    Attributes
    ----------
    analysis_id : str
        Unique identifier for this analysis
    project_id : str
        Project this analysis belongs to
    task_id : Optional[str]
        Task this analysis is for (None for project-level analysis)
    analysis_type : str
        Type of analysis: "requirement_divergence", "decision_impact",
        "instruction_quality", "failure_diagnosis", "overall_assessment"
    timestamp : datetime
        When this analysis was performed
    version : str
        Version of analysis algorithm used (for schema evolution)
    result : dict[str, Any]
        The actual analysis output (LLM response + raw data)
    """

    analysis_id: str
    project_id: str
    task_id: Optional[str]
    analysis_type: str
    timestamp: datetime
    version: str
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "analysis_id": self.analysis_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "analysis_type": self.analysis_type,
            "timestamp": self.timestamp.isoformat(),
            "version": self.version,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        """Create from dictionary loaded from storage."""
        # Parse timestamp and ensure it's timezone-aware
        ts = datetime.fromisoformat(data["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        return cls(
            analysis_id=data["analysis_id"],
            project_id=data["project_id"],
            task_id=data.get("task_id"),
            analysis_type=data["analysis_type"],
            timestamp=ts,
            version=data["version"],
            result=data.get("result", {}),
        )


class AnalysisResultStore:
    """
    Persistent storage for analysis results.

    Stores LLM analysis outputs in SQLite for caching and retrieval.
    Supports versioning to handle algorithm changes over time.

    Benefits
    --------
    - Avoid re-analyzing same data (expensive LLM calls)
    - Support incremental analysis (analyze new tasks only)
    - Track analysis history (multiple versions)
    - Fast retrieval by project, task, or type

    Parameters
    ----------
    marcus_root : Optional[Path]
        Root directory of MARCUS installation. Auto-detects if None.
    """

    def __init__(self, marcus_root: Optional[Path] = None):
        """
        Initialize analysis result store.

        Parameters
        ----------
        marcus_root : Optional[Path]
            Root directory containing data/marcus.db. If None, auto-detects
            repository root.
        """
        # Issue #792: the auto-detected root is the code checkout, so
        # passing it on sent marcus.db there even when MARCUS_DATA_DIR
        # asked for isolation. A root the caller passed on purpose still
        # wins, for everything.
        explicit_root = marcus_root is not None
        if marcus_root is None:
            # Auto-detect Marcus root
            marcus_root = Path(__file__).parent.parent.parent.parent

        self.marcus_root = Path(marcus_root)
        self.db_path = (
            self.marcus_root / "data" / "marcus.db"
            if explicit_root
            else marcus_db_path()
        )

        # Initialize persistence backend
        self.persistence = SQLitePersistence(db_path=self.db_path)

    async def store_result(self, result: AnalysisResult) -> None:
        """
        Store an analysis result.

        If a result with the same analysis_id already exists, it will be
        updated (replaced).

        Parameters
        ----------
        result : AnalysisResult
            The analysis result to store

        Examples
        --------
        ```python
        result = AnalysisResult(
            analysis_id="anl_001",
            project_id="proj-123",
            task_id="task-456",
            analysis_type="requirement_divergence",
            timestamp=datetime.now(timezone.utc),
            version="1.0",
            result={"fidelity_score": 0.85},
        )
        await store.store_result(result)
        ```
        """
        await self.persistence.store(
            "analysis_results", result.analysis_id, result.to_dict()
        )
        logger.debug(
            f"Stored analysis result {result.analysis_id} "
            f"({result.analysis_type} for {result.task_id or 'project'})"
        )

    async def get_result(self, analysis_id: str) -> Optional[AnalysisResult]:
        """
        Retrieve a specific analysis result by ID.

        Parameters
        ----------
        analysis_id : str
            The unique analysis identifier

        Returns
        -------
        Optional[AnalysisResult]
            The analysis result, or None if not found

        Examples
        --------
        ```python
        result = await store.get_result("anl_001")
        if result:
            print(f"Fidelity score: {result.result['fidelity_score']}")
        ```
        """
        data = await self.persistence.retrieve("analysis_results", analysis_id)
        if data is None:
            return None

        # Remove internal persistence fields
        data.pop("_key", None)
        data.pop("_stored_at", None)

        return AnalysisResult.from_dict(data)

    async def get_results_for_project(self, project_id: str) -> list[AnalysisResult]:
        """
        Get all analysis results for a project.

        Parameters
        ----------
        project_id : str
            Project identifier

        Returns
        -------
        list[AnalysisResult]
            All analysis results for this project

        Examples
        --------
        ```python
        results = await store.get_results_for_project("proj-123")
        print(f"Found {len(results)} cached analyses")
        ```
        """
        all_results = await self.persistence.query("analysis_results", limit=100000)

        project_results = []
        for data in all_results:
            if data.get("project_id") == project_id:
                # Remove internal fields
                data.pop("_key", None)
                data.pop("_stored_at", None)
                project_results.append(AnalysisResult.from_dict(data))

        return project_results

    async def get_results_for_task(
        self, project_id: str, task_id: str
    ) -> list[AnalysisResult]:
        """
        Get all analysis results for a specific task.

        Parameters
        ----------
        project_id : str
            Project identifier
        task_id : str
            Task identifier

        Returns
        -------
        list[AnalysisResult]
            All analysis results for this task

        Examples
        --------
        ```python
        results = await store.get_results_for_task("proj-123", "task-456")
        for result in results:
            print(f"{result.analysis_type}: {result.version}")
        ```
        """
        all_results = await self.persistence.query("analysis_results", limit=100000)

        task_results = []
        for data in all_results:
            if data.get("project_id") == project_id and data.get("task_id") == task_id:
                # Remove internal fields
                data.pop("_key", None)
                data.pop("_stored_at", None)
                task_results.append(AnalysisResult.from_dict(data))

        return task_results

    async def get_latest_result(
        self, project_id: str, task_id: Optional[str], analysis_type: str
    ) -> Optional[AnalysisResult]:
        """
        Get the most recent analysis result for a specific type.

        Useful for getting cached results when you want the latest version
        of a particular analysis.

        Parameters
        ----------
        project_id : str
            Project identifier
        task_id : Optional[str]
            Task identifier (None for project-level analysis)
        analysis_type : str
            Type of analysis to retrieve

        Returns
        -------
        Optional[AnalysisResult]
            The most recent result of this type, or None if not found

        Examples
        --------
        ```python
        # Get latest requirement divergence analysis for a task
        latest = await store.get_latest_result(
            "proj-123", "task-456", "requirement_divergence"
        )
        if latest:
            print(f"Using cached result from {latest.timestamp}")
        else:
            print("No cached result, running fresh analysis")
        ```
        """
        if task_id is not None:
            results = await self.get_results_for_task(project_id, task_id)
        else:
            results = await self.get_results_for_project(project_id)

        # Filter by analysis type
        matching = [r for r in results if r.analysis_type == analysis_type]

        if not matching:
            return None

        # Return most recent
        return max(matching, key=lambda r: r.timestamp)

    async def delete_result(self, analysis_id: str) -> bool:
        """
        Delete a specific analysis result.

        Parameters
        ----------
        analysis_id : str
            The analysis identifier to delete

        Returns
        -------
        bool
            True if deleted, False if not found

        Examples
        --------
        ```python
        deleted = await store.delete_result("anl_001")
        if deleted:
            print("Result deleted")
        ```
        """
        # Check if it exists first
        existing = await self.get_result(analysis_id)
        if existing is None:
            return False

        try:
            await self.persistence.delete("analysis_results", analysis_id)
            logger.debug(f"Deleted analysis result {analysis_id}")
            return True
        except Exception as e:
            logger.debug(f"Failed to delete {analysis_id}: {e}")
            return False

    async def clear_project_results(self, project_id: str) -> int:
        """
        Clear all analysis results for a project.

        Useful when you want to force re-analysis of an entire project.

        Parameters
        ----------
        project_id : str
            Project identifier

        Returns
        -------
        int
            Number of results deleted

        Examples
        --------
        ```python
        count = await store.clear_project_results("proj-123")
        print(f"Cleared {count} cached results")
        ```
        """
        results = await self.get_results_for_project(project_id)

        deleted_count = 0
        for result in results:
            if await self.delete_result(result.analysis_id):
                deleted_count += 1

        logger.info(f"Cleared {deleted_count} results for project {project_id}")
        return deleted_count
