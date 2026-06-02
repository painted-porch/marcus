"""Data models for the validation system.

This module defines type-safe data structures for validating implementation tasks
against their acceptance criteria.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ValidationSeverity(str, Enum):
    """Severity levels for validation issues."""

    CRITICAL = "critical"  # Prevents task completion (missing core feature)
    MAJOR = "major"  # Significant issue but task might be usable
    MINOR = "minor"  # Improvement needed but not blocking


@dataclass
class SourceFile:
    """Discovered source file with content and metadata.

    Attributes
    ----------
    path : str
        Absolute path to the source file
    relative_path : str
        Path relative to project_root for display
    size_bytes : int
        File size in bytes (0 indicates empty file)
    content : str
        Complete file content (up to 1MB safety limit)
    has_placeholders : bool
        True if file contains TODO, FIXME, NotImplementedError
    extension : str
        File extension (e.g., '.py', '.js')
    modified_time : datetime
        Last modification timestamp
    """

    path: str
    relative_path: str
    size_bytes: int
    content: str
    has_placeholders: bool
    extension: str
    modified_time: datetime

    def is_empty(self) -> bool:
        """Check if file is empty (0 bytes or whitespace only).

        Returns
        -------
        bool
            True if file is empty
        """
        return self.size_bytes == 0 or not self.content.strip()


@dataclass
class WorkEvidence:
    """Bundle of evidence collected for validation.

    Attributes
    ----------
    source_files : list[SourceFile]
        Discovered source files from project_root
    design_artifacts : list[dict[str, Any]]
        Design documents logged via log_artifact()
    decisions : list[dict[str, Any]]
        Architectural decisions from get_task_context()
    project_root : str
        Root directory where source files were discovered
    collection_time : datetime
        When evidence was collected
    """

    source_files: list[SourceFile]
    design_artifacts: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    project_root: str
    file_manifest: list[str] = field(default_factory=list)
    collection_time: datetime = field(default_factory=lambda: datetime.utcnow())

    def has_source_files(self) -> bool:
        """Check if any source files were discovered.

        Returns
        -------
        bool
            True if source files exist
        """
        return len(self.source_files) > 0

    def get_total_source_bytes(self) -> int:
        """Calculate total bytes across all source files.

        Returns
        -------
        int
            Sum of all source file sizes
        """
        return sum(f.size_bytes for f in self.source_files)


@dataclass
class ValidationIssue:
    """Single validation issue found during analysis.

    Attributes
    ----------
    severity : ValidationSeverity
        How critical this issue is
    issue : str
        Description of what's wrong
    evidence : str
        What evidence supports this issue
    remediation : str
        Specific actionable fix (tactical code-level suggestion)
    criterion : str
        Which acceptance criterion this relates to
    """

    severity: ValidationSeverity
    issue: str
    evidence: str
    remediation: str
    criterion: str

    def to_dict(self) -> dict[str, Any]:
        """Convert issue to dictionary for serialization.

        Returns
        -------
        dict[str, Any]
            Dictionary representation
        """
        return {
            "severity": self.severity.value,
            "issue": self.issue,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "criterion": self.criterion,
        }

    def get_fingerprint(self) -> str:
        """Generate unique fingerprint for issue comparison.

        Used by RetryTracker to detect repeated failures with same issues.

        Returns
        -------
        str
            Stable identifier based on severity + criterion + issue
        """
        import hashlib

        # Use severity + criterion + issue for stable fingerprint
        # Excludes evidence and remediation which may vary
        data = f"{self.severity.value}|{self.criterion}|{self.issue}"
        # MD5 used for fingerprinting only, not security
        return hashlib.md5(data.encode(), usedforsecurity=False).hexdigest()


@dataclass
class ValidationResult:
    """Result of validating a task implementation.

    Attributes
    ----------
    passed : bool
        Whether validation passed
    issues : list[ValidationIssue]
        List of issues found (empty if passed=True)
    ai_reasoning : str
        AI's explanation of the validation decision
    validation_time : datetime
        When validation was performed
    executed : bool
        True when a real validation action ran to completion (test
        runner executed, LLM produced a verdict, etc). Distinguishes
        "ran and passed" from "skipped because nothing was runnable."
        Callers that merge multiple validation signals use this to
        decide which signal is authoritative — a skipped runner must
        not be treated as ground truth just because its pass-through
        result happens to say ``passed=True``.
    """

    passed: bool
    issues: list[ValidationIssue]
    ai_reasoning: str
    validation_time: datetime = field(default_factory=lambda: datetime.utcnow())
    executed: bool = False

    def has_critical_issues(self) -> bool:
        """Check if any critical issues exist.

        Returns
        -------
        bool
            True if any issue has CRITICAL severity
        """
        return any(i.severity == ValidationSeverity.CRITICAL for i in self.issues)

    def get_issue_fingerprints(self) -> set[str]:
        """Get fingerprints of all issues for comparison.

        Returns
        -------
        set[str]
            Set of issue fingerprints
        """
        return {issue.get_fingerprint() for issue in self.issues}

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for serialization.

        Returns
        -------
        dict[str, Any]
            Dictionary representation
        """
        return {
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "ai_reasoning": self.ai_reasoning,
            "validation_time": self.validation_time.isoformat(),
        }


@dataclass
class ValidationAttemptRecord:
    """Record of a single validation attempt for retry tracking.

    Attributes
    ----------
    task_id : str
        Task being validated
    attempt_number : int
        Sequential attempt number (1, 2, 3, ...)
    result : ValidationResult
        Validation result for this attempt
    timestamp : datetime
        When this attempt occurred
    """

    task_id: str
    attempt_number: int
    result: ValidationResult
    timestamp: datetime = field(default_factory=lambda: datetime.utcnow())

    def get_issue_fingerprints(self) -> set[str]:
        """Get fingerprints from this attempt's issues.

        Returns
        -------
        set[str]
            Set of issue fingerprints
        """
        return self.result.get_issue_fingerprints()
