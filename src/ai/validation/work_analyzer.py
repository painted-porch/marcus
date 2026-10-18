"""Core validation engine for implementation task completeness.

This module provides the WorkAnalyzer class which:
1. Discovers source files by scanning project_root directory
2. Gathers design artifacts and architectural decisions
3. Validates implementations against acceptance criteria using AI
"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.ai.providers.llm_abstraction import LLMAbstraction
from src.ai.validation.validation_models import (
    SourceFile,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
    WorkEvidence,
)
from src.core.error_framework import ErrorContext, ProjectRootNotFoundError
from src.core.resilience import RetryConfig, with_retry
from src.marcus_mcp.tools.context import get_task_context

logger = logging.getLogger(__name__)


class WorkAnalyzer:
    """Core validation engine with isolated LLM instance.

    Validates implementation tasks by:
    - Discovering source files from project_root
    - Reading complete file contents
    - Analyzing code against acceptance criteria using AI
    - Detecting empty files and placeholder code

    Attributes
    ----------
    _validation_llm : LLMAbstraction
        Dedicated LLM instance for validation (isolated from Marcus's context)
    """

    # Default source file extensions to include in validation
    DEFAULT_SOURCE_EXTENSIONS = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".html",
        ".css",
        ".scss",
        ".less",
    }

    # Directories to exclude from source file discovery
    EXCLUDE_DIRS = {
        "docs",  # Design artifacts (not implementation)
        "node_modules",  # Dependencies
        ".git",  # Version control
        "__pycache__",  # Python cache
        "venv",
        ".venv",  # Python virtual environments
        "build",
        "dist",  # Build artifacts
        ".pytest_cache",  # Test cache
        "coverage",  # Coverage reports
        ".next",  # Next.js build
        ".nuxt",  # Nuxt.js build
        "target",  # Rust/Java build
        "bin",
        "obj",  # C#/.NET build
    }

    # Placeholder patterns indicating incomplete implementation
    PLACEHOLDER_PATTERNS = {
        "TODO",
        "FIXME",
        "HACK",
        "XXX",
        "NotImplementedError",
        "pass  # TODO",
        "throw new Error('Not implemented')",
        "unimplemented!()",  # Rust
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize WorkAnalyzer with dedicated LLM instance.

        Parameters
        ----------
        config : dict[str, Any] | None, optional
            Configuration overrides. Supported keys:
            - source_extensions: set[str] - File extensions to validate
        """
        # Dedicated LLM instance - does NOT share context with Marcus
        self._validation_llm = LLMAbstraction()

        # Allow configuration of source extensions
        config = config or {}
        self.SOURCE_EXTENSIONS = set(
            config.get("source_extensions", self.DEFAULT_SOURCE_EXTENSIONS)
        )

    async def gather_evidence(
        self, task: Any, state: Any, agent_id: str | None = None
    ) -> WorkEvidence:
        """Gather evidence for validation by discovering source files.

        Parameters
        ----------
        task : Any
            Task to gather evidence for
        state : Any
            Marcus server state
        agent_id : str | None
            Authoritative agent ID from the call frame (report_task_progress).
            Takes precedence over task.assigned_to for worktree resolution.
            Needed because task.assigned_to is updated to the RECOVERING agent
            at validation time, but the actual work lives in the ORIGINAL
            agent's worktree.

        Returns
        -------
        WorkEvidence
            Bundle of source files + design artifacts + decisions
        """
        logger.info(f"Gathering validation evidence for task {task.id} ({task.name})")

        # 1. Get project_root from workspace manager or artifacts
        project_root = self._get_project_root(task, state, agent_id=agent_id)
        logger.debug(f"Project root: {project_root}")

        # 2. Scope evidence to git delta when a baseline_commit is available.
        #    This prevents the token explosion caused by loading all merged-agent
        #    files when validating a single agent's work (issue #696).
        allowed_files = self._get_git_delta_files(project_root, state, agent_id)

        # 3. Discover source files — scoped to delta or full scan as fallback
        source_files = self._discover_source_files(
            project_root, allowed_files=allowed_files
        )
        logger.info(
            f"Discovered {len(source_files)} source files "
            f"({sum(f.size_bytes for f in source_files)} total bytes)"
        )

        # 4. Collect full file manifest (names only, no content reads).
        #    Injected into the prompt so the LLM knows which files exist
        #    even when their extension is excluded from SOURCE_EXTENSIONS
        #    (e.g. pyproject.toml, Makefile). Prevents false "file missing"
        #    reports when the delta scan limits content to .py/.js files.
        file_manifest = self._collect_file_manifest(project_root)
        logger.info(f"File manifest: {len(file_manifest)} files in project")

        # 5. Get design artifacts from state
        design_artifacts = state.task_artifacts.get(task.id, []).copy()
        logger.debug(f"Found {len(design_artifacts)} design artifacts")

        # 6. Get decisions from get_task_context
        decisions = await self._get_decisions(task, state)
        logger.debug(f"Retrieved {len(decisions)} architectural decisions")

        return WorkEvidence(
            source_files=source_files,
            design_artifacts=design_artifacts,
            decisions=decisions,
            project_root=project_root,
            file_manifest=file_manifest,
            collection_time=datetime.utcnow(),
        )

    async def validate_implementation_task(
        self, task: Any, state: Any, agent_id: str | None = None
    ) -> ValidationResult:
        """Validate implementation task against acceptance criteria.

        Parameters
        ----------
        task : Any
            Task to validate (must have completion_criteria)
        state : Any
            Marcus server state
        agent_id : str | None
            Authoritative agent ID from report_task_progress. Takes precedence
            over task.assigned_to when resolving the agent's worktree path.
            Prevents stale-snapshot errors during task recovery where
            task.assigned_to names the recovering agent but the actual
            implementation is in the original agent's worktree.

        Returns
        -------
        ValidationResult
            Pass/fail result with issues and remediation
        """
        import time

        start_time = time.time()
        logger.info(f"Starting validation for task {task.id} ({task.name})")

        # Gather evidence — pass agent_id so worktree resolution uses the
        # authoritative caller ID, not task.assigned_to which may name a
        # different agent after task recovery.
        evidence = await self.gather_evidence(task, state, agent_id=agent_id)

        # Check if no source files discovered (immediate failure)
        if not evidence.has_source_files():
            duration_ms = int((time.time() - start_time) * 1000)
            logger.warning(
                f"No source files found for task {task.id} in {evidence.project_root}"
            )
            self._record_metrics(
                task_id=task.id,
                task_type=getattr(task, "type", "unknown"),
                result="fail",
                reason="no_source_files",
                duration_ms=duration_ms,
            )
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        severity=ValidationSeverity.CRITICAL,
                        issue="No source files discovered in project directory",
                        evidence=f"Scanned {evidence.project_root} but found no implementation files",  # noqa: E501
                        remediation="Create source code files to implement the task requirements",  # noqa: E501
                        criterion="Implementation exists",
                    )
                ],
                ai_reasoning="Cannot validate - no source code found",
                validation_time=datetime.utcnow(),
            )

        # Run runtime tests and LLM review INDEPENDENTLY. Previously
        # the LLM source review gated the runtime tests — if the LLM
        # hallucinated a violation, tests never ran and correct
        # implementations were blocked. New semantics (Kaia review,
        # experiment 67 post-mortem):
        #
        #   Tests pass + LLM pass      → PASS
        #   Tests pass + LLM fail      → PASS with advisory (LLM is
        #                                 logged but non-blocking —
        #                                 tests are authoritative on
        #                                 behavior)
        #   Tests fail + LLM pass      → FAIL (tests are ground truth)
        #   Tests fail + LLM fail      → FAIL
        #   No tests available + LLM   → LLM is authoritative (there
        #                                 is no ground truth to defer
        #                                 to)
        #
        # Runtime tests are ground truth for behavior. LLM review is
        # structural evidence gathering that can catch missing files,
        # TODO markers, and empty implementations — but it is not
        # authoritative on functional correctness.
        logger.debug(f"Running runtime tests for task {task.id}")
        runtime_result = await self._validate_runtime(task, evidence)

        logger.debug(f"Calling AI validator for task {task.id}")
        ai_response = await self._validate_with_ai(task, evidence)
        llm_result = self._parse_validation_response(ai_response)

        # If LLM says "fail" but provides zero issues, treat as pass
        # (the LLM couldn't articulate what's wrong, so nothing is wrong)
        if not llm_result.passed and len(llm_result.issues) == 0:
            logger.info(
                f"LLM returned fail with 0 issues for task {task.id} "
                f"- treating as pass (no actionable issues found)"
            )
            llm_result = ValidationResult(
                passed=True,
                issues=[],
                ai_reasoning=(
                    f"Auto-passed: LLM indicated failure but provided "
                    f"no specific issues. Original: {llm_result.ai_reasoning}"
                ),
                validation_time=datetime.utcnow(),
            )

        # Verify LLM citations against actual file content.
        # Hallucinated file:line references get dropped, which may
        # flip a FAIL to PASS when all issues were confabulated.
        llm_result = self._verify_citations(llm_result, evidence, task.id)

        # Determine whether runtime tests are the ground-truth
        # signal. Codex P1 on PR #337: the previous implementation
        # inferred this from test-file existence via
        # _discover_task_tests, which gave a false positive when
        # test files existed but no runner was detected (skip
        # returning passed=True with nothing actually executed). We
        # now trust the executed flag set by _validate_runtime,
        # which is True only when a runner was invoked and finished
        # (pass, fail, timeout, or subprocess error — all real
        # execution events).
        runtime_tests_ran = runtime_result.executed

        # Apply merge semantics.
        if runtime_tests_ran:
            if runtime_result.passed:
                # Tests are authoritative and passed. LLM review
                # becomes advisory: log any issues but do NOT block.
                if not llm_result.passed:
                    logger.warning(
                        f"Tests PASSED but LLM flagged "
                        f"{len(llm_result.issues)} issue(s) for task "
                        f"{task.id} — treating as advisory since "
                        f"tests are the behavioral ground truth. "
                        f"Advisory issues: "
                        f"{[i.issue[:80] for i in llm_result.issues]}"
                    )
                result = ValidationResult(
                    passed=True,
                    issues=[],
                    ai_reasoning=(
                        f"Runtime tests passed (authoritative). "
                        f"LLM review: {llm_result.ai_reasoning[:200]}"
                    ),
                    validation_time=datetime.utcnow(),
                )
            else:
                # Tests failed → FAIL regardless of LLM opinion.
                # Merge LLM's issues (structural) with test failures
                # (behavioral) for complete remediation context.
                result = ValidationResult(
                    passed=False,
                    issues=runtime_result.issues + llm_result.issues,
                    ai_reasoning=(
                        f"Runtime tests FAILED: " f"{runtime_result.ai_reasoning[:300]}"
                    ),
                    validation_time=datetime.utcnow(),
                )
        else:
            # No task-scoped runtime tests available. LLM review is
            # the only signal we have, so it becomes authoritative.
            if not llm_result.passed:
                logger.warning(
                    f"No runtime tests for task {task.id} — LLM "
                    f"review is authoritative and reported "
                    f"{len(llm_result.issues)} issue(s)"
                )
            result = llm_result

        duration_ms = int((time.time() - start_time) * 1000)
        if result.passed:
            logger.info(
                f"Validation PASSED for task {task.id} ({task.name}) "
                f"in {duration_ms}ms"
            )
            self._record_metrics(
                task_id=task.id,
                task_type=getattr(task, "type", "unknown"),
                result="pass",
                duration_ms=duration_ms,
            )
        else:
            logger.warning(
                f"Validation FAILED for task {task.id} ({task.name}) "
                f"- {len(result.issues)} issue(s) found in {duration_ms}ms"
            )
            self._record_metrics(
                task_id=task.id,
                task_type=getattr(task, "type", "unknown"),
                result="fail",
                reason="criteria_not_met",
                duration_ms=duration_ms,
                issue_count=len(result.issues),
            )

        return result

    def _get_project_root(
        self, task: Any, state: Any, agent_id: str | None = None
    ) -> str:
        """Get project_root from workspace state, manager, or artifacts.

        Parameters
        ----------
        task : Any
            Task being validated
        state : Any
            Marcus server state
        agent_id : str | None
            Authoritative agent ID from the call frame. When provided, used
            instead of task.assigned_to for worktree resolution. This fixes
            stale-snapshot errors during task recovery: task.assigned_to names
            the RECOVERING agent, but the actual work (including file deletions
            the validator must see) lives in the ORIGINAL agent's worktree.

        Returns
        -------
        str
            Absolute path to project root

        Raises
        ------
        ValueError
            If project_root cannot be determined
        """
        # Check for agent worktree first (GH-250, GH-305)
        # If the agent has an isolated worktree, validate there
        # instead of the main implementation/ directory.
        if hasattr(state, "kanban_client") and state.kanban_client:
            workspace_state = state.kanban_client._load_workspace_state()
            if workspace_state and "project_root" in workspace_state:
                project_root = workspace_state["project_root"]

                # Prefer explicit agent_id over task.assigned_to (GH-recovery fix).
                # task.assigned_to is updated to the recovering agent before
                # validation runs, so it would resolve the wrong worktree.
                resolved_agent_id = agent_id or getattr(task, "assigned_to", None)
                if resolved_agent_id:
                    from pathlib import Path

                    main_repo = Path(project_root)
                    # worktrees/ is sibling of implementation/
                    worktree = main_repo.parent / "worktrees" / resolved_agent_id
                    if worktree.exists():
                        logger.info(f"Found agent worktree: {worktree}")
                        return str(worktree)

                logger.info(
                    f"Found project_root from workspace state: " f"{project_root}"
                )
                return str(project_root)

        # Try workspace manager
        if (
            hasattr(state, "workspace_manager")
            and state.workspace_manager
            and hasattr(state.workspace_manager, "project_config")
            and state.workspace_manager.project_config
        ):
            main_workspace = state.workspace_manager.project_config.main_workspace
            if main_workspace:
                return str(main_workspace)

        # Fallback: extract from logged artifacts
        artifacts = state.task_artifacts.get(task.id, [])
        if artifacts and "project_root" in artifacts[0]:
            return str(artifacts[0]["project_root"])

        # Cannot determine project location
        logger.error(
            f"Cannot determine project_root for task {task.id} - "
            "no workspace config and no artifacts logged"
        )
        raise ProjectRootNotFoundError(
            task_id=task.id,
            context=ErrorContext(
                operation="get_project_root",
                task_id=task.id,
            ),
        )

    def _get_git_delta_files(
        self, project_root: str, state: Any, agent_id: str | None
    ) -> list[str] | None:
        """Return the relative file paths changed by this agent since their baseline.

        Uses ``git diff <baseline_commit>..HEAD --name-only`` to isolate only
        the files the agent wrote, preventing the entire merged codebase from
        being loaded into the validation prompt (issue #696).

        Parameters
        ----------
        project_root : str
            Absolute path to the agent's worktree (or main repo as fallback)
        state : Any
            Marcus server state — used to look up the agent's TaskAssignment
        agent_id : str | None
            Agent performing the task

        Returns
        -------
        list[str] | None
            Relative file paths in the delta. An empty list means the diff
            succeeded but the agent changed no files — the caller must treat
            this as "no source-file evidence" (a correct completion failure),
            NOT as a reason to fall back to the full worktree scan. None is
            returned only when the delta genuinely cannot be computed (no
            baseline, git error), where the caller falls back to a full scan.
        """
        baseline_commit: str | None = None
        if agent_id and hasattr(state, "agent_tasks"):
            assignment = state.agent_tasks.get(agent_id)
            if assignment:
                baseline_commit = getattr(assignment, "baseline_commit", None)

        if not baseline_commit:
            logger.debug(
                f"No baseline_commit for agent {agent_id} — using full worktree scan"
            )
            return None

        try:
            result = subprocess.run(
                ["git", "diff", f"{baseline_commit}..HEAD", "--name-only"],
                capture_output=True,
                text=True,
                cwd=project_root,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    f"git diff failed (exit {result.returncode}): "
                    f"{result.stderr.strip()} — falling back to full worktree scan"
                )
                return None

            files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
            logger.info(
                f"Git delta for agent {agent_id}: {len(files)} files changed "
                f"since {baseline_commit[:8]}"
            )
            # A successful-but-empty diff means the agent committed no changes.
            # Return the empty list (not None) so validation surfaces the
            # "no source files" failure instead of scanning the entire merged
            # worktree and grading the agent against other agents' work.
            return files

        except Exception as exc:
            logger.warning(
                f"Failed to compute git delta for agent {agent_id}: {exc}"
                " — falling back to full worktree scan"
            )
            return None

    def _collect_file_manifest(self, project_root: str) -> list[str]:
        """Return relative paths of every file in project_root (names only).

        This is the cheap half of the git-delta approach (issue #696): we
        scan the full worktree for filenames — no content reads — and inject
        the list into the validation prompt so the LLM knows which files
        *exist* even when their extension is excluded from SOURCE_EXTENSIONS
        (e.g. ``pyproject.toml``, ``Makefile``, ``.flake8``).

        Parameters
        ----------
        project_root : str
            Absolute path to the project directory to scan

        Returns
        -------
        list[str]
            Sorted relative file paths (POSIX separators)
        """
        manifest: list[str] = []
        project_path = Path(project_root).resolve()

        for root, dirs, files in os.walk(project_root):
            dirs[:] = [d for d in dirs if d not in self.EXCLUDE_DIRS]
            for file in files:
                file_path = Path(root) / file
                try:
                    resolved = file_path.resolve()
                    if not resolved.is_relative_to(project_path):
                        continue
                    manifest.append(str(resolved.relative_to(project_path)))
                except (ValueError, OSError):
                    continue

        return sorted(manifest)

    def _discover_source_files(
        self, project_root: str, allowed_files: list[str] | None = None
    ) -> list[SourceFile]:
        """Discover source files by scanning project_root directory.

        Parameters
        ----------
        project_root : str
            Root directory to scan
        allowed_files : list[str] | None
            When provided, only load these relative paths (git-delta scoping).
            When None, walk the entire project_root (legacy behaviour).

        Returns
        -------
        list[SourceFile]
            Discovered source files with content
        """
        source_files: list[SourceFile] = []
        project_path = Path(project_root).resolve()

        # Track total content size to prevent memory exhaustion
        total_content_bytes = 0
        MAX_TOTAL_CONTENT = 10_000_000  # 10MB total limit across all files

        if allowed_files is not None:
            # Git-delta mode: only load the specific files the agent changed
            logger.debug(
                f"Git-delta mode: loading {len(allowed_files)} files "
                f"from {project_root}"
            )
            delta_pairs = []
            for rel in allowed_files:
                abs_p = project_path / rel
                try:
                    resolved = abs_p.resolve()
                except (OSError, RuntimeError):
                    logger.debug(f"Skipping unresolvable delta file: {rel}")
                    continue
                # Guard against a source-named symlink escaping the worktree
                # (e.g. leak.py -> /etc/secret). Without this, validation would
                # follow the link and send external file content to the LLM.
                # Mirrors the resolve + containment check in
                # _collect_file_manifest / the legacy full scan.
                if not resolved.is_relative_to(project_path):
                    logger.warning(
                        "Skipping delta file outside project root "
                        f"(possible symlink escape): {rel}"
                    )
                    continue
                if resolved.exists():
                    delta_pairs.append((resolved, resolved))
                else:
                    logger.debug(f"Skipping missing delta file: {rel}")

            for file_path, resolved_path in delta_pairs:
                if resolved_path.suffix not in self.SOURCE_EXTENSIONS:
                    continue
                try:
                    stat = resolved_path.stat()
                    size_bytes = stat.st_size
                    modified_time = datetime.fromtimestamp(stat.st_mtime)

                    if size_bytes == 0:
                        content = ""
                    elif total_content_bytes + size_bytes > MAX_TOTAL_CONTENT:
                        logger.warning(
                            f"Skipping {resolved_path} — total content limit "
                            f"({MAX_TOTAL_CONTENT} bytes) would be exceeded"
                        )
                        continue
                    elif size_bytes > 1_000_000:
                        content = resolved_path.read_text(errors="ignore")[:100000]
                        content += "\n\n[FILE TRUNCATED - Too large for validation]"
                        total_content_bytes += len(content)
                    else:
                        content = resolved_path.read_text(errors="ignore")
                        total_content_bytes += len(content)

                    has_placeholders = any(
                        pattern in content for pattern in self.PLACEHOLDER_PATTERNS
                    )
                    source_files.append(
                        SourceFile(
                            path=str(resolved_path),
                            relative_path=str(resolved_path.relative_to(project_path)),
                            size_bytes=size_bytes,
                            content=content,
                            has_placeholders=has_placeholders,
                            extension=resolved_path.suffix,
                            modified_time=modified_time,
                        )
                    )
                except Exception as exc:
                    logger.warning(f"Failed to read {resolved_path}: {exc}")
                    continue

            logger.info(
                f"Loaded {len(source_files)} delta files, "
                f"total {total_content_bytes:,} bytes"
            )
            return source_files

        # Legacy full-worktree mode (no baseline_commit available)
        logger.debug(
            f"Full-scan mode: walking {project_root} "
            f"(excluding {self.EXCLUDE_DIRS})"
        )
        for root, dirs, files in os.walk(project_root):
            # Filter out excluded directories (in-place modification)
            dirs[:] = [d for d in dirs if d not in self.EXCLUDE_DIRS]

            for file in files:
                file_path = Path(root) / file

                # SECURITY: Resolve symlinks and validate path stays within project_root
                try:
                    resolved_path = file_path.resolve()
                    if not resolved_path.is_relative_to(project_path):
                        logger.warning(
                            f"Skipping file outside project root: {file_path} "
                            f"(resolves to {resolved_path})"
                        )
                        continue
                except (ValueError, OSError) as e:
                    logger.warning(f"Failed to resolve path {file_path}: {e}")
                    continue

                # Filter by extension
                if resolved_path.suffix not in self.SOURCE_EXTENSIONS:
                    continue

                try:
                    # Get file metadata
                    stat = resolved_path.stat()
                    size_bytes = stat.st_size
                    modified_time = datetime.fromtimestamp(stat.st_mtime)

                    # Read content with memory limit protection
                    if size_bytes == 0:
                        content = ""
                    elif total_content_bytes + size_bytes > MAX_TOTAL_CONTENT:
                        logger.warning(
                            f"Skipping {resolved_path} - total content limit "
                            f"({MAX_TOTAL_CONTENT} bytes) would be exceeded"
                        )
                        continue
                    elif size_bytes > 1_000_000:  # 1MB per-file safety limit
                        # File too large - read first 100KB and flag
                        content = resolved_path.read_text(errors="ignore")[:100000]
                        content += "\n\n[FILE TRUNCATED - Too large for validation]"
                        total_content_bytes += len(content)
                    else:
                        # Read complete file
                        content = resolved_path.read_text(errors="ignore")
                        total_content_bytes += len(content)

                    # Detect placeholders
                    has_placeholders = any(
                        pattern in content for pattern in self.PLACEHOLDER_PATTERNS
                    )

                    source_files.append(
                        SourceFile(
                            path=str(resolved_path),
                            relative_path=str(resolved_path.relative_to(project_path)),
                            size_bytes=size_bytes,
                            content=content,
                            has_placeholders=has_placeholders,
                            extension=resolved_path.suffix,
                            modified_time=modified_time,
                        )
                    )

                except Exception as e:
                    # Don't fail entire discovery if one file has issues
                    logger.warning(f"Failed to read {resolved_path}: {e}")
                    continue

        logger.info(
            f"Discovered {len(source_files)} files, "
            f"total {total_content_bytes:,} bytes loaded"
        )
        return source_files

    async def _get_decisions(self, task: Any, state: Any) -> list[dict[str, Any]]:
        """Get architectural decisions from get_task_context.

        Parameters
        ----------
        task : Any
            Task to get decisions for
        state : Any
            Marcus server state

        Returns
        -------
        list[dict[str, Any]]
            Decisions from this task + dependencies + siblings
        """
        try:
            context_result = await get_task_context(task.id, state)
            if context_result.get("success"):
                context = context_result.get("context", {})
                decisions: list[dict[str, Any]] = context.get("decisions", [])
                return decisions
        except Exception as e:
            logger.warning(f"Failed to get task context for decisions: {e}")

        return []

    async def _validate_runtime(
        self, task: Any, evidence: WorkEvidence
    ) -> ValidationResult:
        """Validate runtime behavior by running task-scoped tests.

        Parameters
        ----------
        task : Any
            Task being validated
        evidence : WorkEvidence
            Gathered evidence with source files

        Returns
        -------
        ValidationResult
            Pass/fail result from test execution
        """
        import asyncio
        import subprocess

        project_root = Path(evidence.project_root)

        # Detect project type
        project_type = self._detect_project_type(project_root)
        if not project_type:
            # No test runner detected - skip runtime validation.
            # executed=False signals to callers that this pass-through
            # result is NOT authoritative (Codex P1 on PR #337).
            logger.info(
                f"No test runner detected in {project_root} - "
                "skipping runtime validation"
            )
            return ValidationResult(
                passed=True, issues=[], ai_reasoning="", executed=False
            )

        # Find test files related to task's source files
        test_files = self._discover_task_tests(evidence.source_files, project_root)
        if not test_files:
            # No tests for this task - skip runtime validation.
            # executed=False: test files may exist elsewhere but none
            # were matched to this task's source files, so no runner
            # ran on this task's code.
            logger.info(
                f"No tests found for task {task.id} - skipping runtime validation"
            )
            return ValidationResult(
                passed=True, issues=[], ai_reasoning="", executed=False
            )

        # Build test command (task-scoped, not full suite)
        test_command = self._build_test_command(project_type, test_files, project_root)
        logger.info(f"Running task-scoped tests: {test_command}")

        # Run tests with timeout
        try:
            # Runtime verification runs inside the long-lived Marcus
            # server process, whose stdin (fd 0) may be closed or
            # invalid — e.g. when the server is started backgrounded.
            # Without an explicit stdin the test subprocess inherits
            # that bad fd 0, and Python aborts at interpreter startup
            # with "init_sys_streams: can't initialize sys standard
            # streams" (OSError: [Errno 9] Bad file descriptor) — every
            # test run then fails for a reason that has nothing to do
            # with the deliverable. DEVNULL gives the child a valid
            # fd 0; start_new_session detaches it from any dead
            # controlling terminal.
            proc = await asyncio.create_subprocess_shell(
                test_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(project_root),
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                # Test failed - parse error. Runner executed, so this
                # result is authoritative.
                error_output = stderr.decode("utf-8", errors="ignore")
                issues = self._parse_test_failure(error_output, project_type)
                return ValidationResult(
                    passed=False,
                    issues=issues,
                    ai_reasoning=f"Tests failed: {error_output[:500]}",
                    validation_time=datetime.utcnow(),
                    executed=True,
                )

            # Tests passed - runner executed, authoritative.
            logger.info(f"Runtime validation PASSED for task {task.id}")
            return ValidationResult(
                passed=True, issues=[], ai_reasoning="", executed=True
            )

        except asyncio.TimeoutError:
            # Runner was invoked but hung. The execution attempt is
            # real; a hung test suite is a real behavioral failure.
            logger.warning(f"Test execution timed out after 30s for task {task.id}")
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        severity=ValidationSeverity.CRITICAL,
                        issue="Test execution timed out after 30 seconds",
                        evidence="Tests did not complete in time",
                        remediation="Optimize tests or check for infinite loops",
                        criterion="All tests run successfully without errors",
                    )
                ],
                ai_reasoning="Test timeout",
                validation_time=datetime.utcnow(),
                executed=True,
            )
        except Exception as e:
            # Subprocess invocation failed (e.g. runner not on PATH,
            # permission error). We tried to execute but couldn't, so
            # this is a real environmental failure, not a skip.
            logger.error(f"Runtime validation failed with error: {e}")
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        severity=ValidationSeverity.CRITICAL,
                        issue=f"Runtime validation error: {str(e)}",
                        evidence=str(e),
                        remediation="Check test configuration and environment",
                        criterion="All tests run successfully without errors",
                    )
                ],
                ai_reasoning=f"Validation error: {str(e)}",
                validation_time=datetime.utcnow(),
                executed=True,
            )

    def _detect_project_type(self, project_root: Path) -> dict[str, str] | None:
        """Detect project type and test runner.

        Parameters
        ----------
        project_root : Path
            Project root directory

        Returns
        -------
        dict[str, str] | None
            Project type info or None if no test runner detected
        """
        # Node.js project
        if (project_root / "package.json").exists():
            return {"type": "nodejs", "test_runner": "npm"}

        # Python project
        if (project_root / "pyproject.toml").exists() or (
            project_root / "setup.py"
        ).exists():
            return {"type": "python", "test_runner": "pytest"}

        # Java project
        if (project_root / "pom.xml").exists():
            return {"type": "java", "test_runner": "maven"}

        return None

    def _discover_task_tests(
        self, source_files: list[SourceFile], project_root: Path
    ) -> list[str]:
        """Find test files related to source files using hybrid strategy.

        Strategy:
        1. Look for co-located tests (fast, specific)
        2. Search common test directories (comprehensive)
        3. Use naming patterns to match tests to source files

        Parameters
        ----------
        source_files : list[SourceFile]
            Source files from task
        project_root : Path
            Project root directory

        Returns
        -------
        list[str]
            Test file paths
        """
        test_files: set[str] = set()  # Use set to avoid duplicates

        # PHASE 1: Co-located tests (original approach - still valuable)
        for source_file in source_files:
            file_path = Path(source_file.path)

            # Co-located test patterns
            colocated_patterns = [
                # JavaScript/TypeScript (same directory)
                file_path.with_suffix(".test.js"),
                file_path.with_suffix(".spec.js"),
                file_path.with_suffix(".test.ts"),
                file_path.with_suffix(".spec.ts"),
                file_path.with_suffix(".test.jsx"),
                file_path.with_suffix(".test.tsx"),
                # Python (same directory)
                file_path.with_name(f"test_{file_path.stem}.py"),
                # React __tests__ convention
                file_path.parent / "__tests__" / f"{file_path.stem}.test.js",
                file_path.parent / "__tests__" / f"{file_path.stem}.test.tsx",
            ]

            for pattern in colocated_patterns:
                if pattern.exists():
                    try:
                        test_files.add(str(pattern.relative_to(project_root)))
                    except ValueError:
                        # Pattern not relative to project_root - skip
                        pass

        # PHASE 2: Common test directories (comprehensive search)
        # Search standard test directory locations
        common_test_dirs = [
            project_root / "tests",  # Python standard
            project_root / "test",  # Alternative
            project_root / "__tests__",  # Jest standard
            project_root / "spec",  # RSpec/Jasmine style
            project_root / "implementation" / "tests",  # Marcus structure
        ]

        for test_dir in common_test_dirs:
            if not test_dir.exists():
                continue

            # Find all test files in this directory
            for source_file in source_files:
                source_path = Path(source_file.path)
                source_name = source_path.stem  # e.g., "calculator"
                source_ext = source_path.suffix  # e.g., ".py"

                # Python test naming patterns
                if source_ext == ".py":
                    # Look for test_<name>.py in test directory
                    test_patterns = [
                        test_dir / f"test_{source_name}.py",
                        test_dir / f"{source_name}_test.py",
                    ]

                    # Also check subdirectories that mirror source structure
                    # e.g., app/models/user.py -> tests/models/test_user.py
                    try:
                        rel_to_project = source_path.relative_to(project_root)
                        source_parent = rel_to_project.parent

                        # Try different mirroring strategies:
                        # 1. Full path: app/models/ -> tests/app/models/
                        # 2. Skip root: app/models/ -> tests/models/
                        # 3. Each subdirectory level
                        parts = source_parent.parts
                        for i in range(len(parts)):
                            # Try each suffix of the path
                            # e.g., for app/models: try models, then app/models
                            subpath = Path(*parts[i:]) if i < len(parts) else Path()
                            mirrored_test_dir = test_dir / subpath
                            if mirrored_test_dir.exists():
                                test_patterns.extend(
                                    [
                                        mirrored_test_dir / f"test_{source_name}.py",
                                        mirrored_test_dir / f"{source_name}_test.py",
                                    ]
                                )
                    except ValueError:
                        pass  # source_path not relative to project_root

                    for pattern in test_patterns:
                        if pattern.exists():
                            try:
                                test_files.add(str(pattern.relative_to(project_root)))
                            except ValueError:
                                pass

                # JavaScript/TypeScript test naming patterns
                elif source_ext in {".js", ".jsx", ".ts", ".tsx"}:
                    test_patterns = [
                        test_dir / f"{source_name}.test.js",
                        test_dir / f"{source_name}.spec.js",
                        test_dir / f"{source_name}.test.ts",
                        test_dir / f"{source_name}.spec.ts",
                        test_dir / f"{source_name}.test.tsx",
                    ]

                    for pattern in test_patterns:
                        if pattern.exists():
                            try:
                                test_files.add(str(pattern.relative_to(project_root)))
                            except ValueError:
                                pass

        return list(test_files)

    def _build_test_command(
        self, project_type: dict[str, str], test_files: list[str], project_root: Path
    ) -> str:
        """Build test command for specific files.

        Automatically detects the test framework and builds appropriate command.

        Parameters
        ----------
        project_type : dict[str, str]
            Project type info
        test_files : list[str]
            Test files to run
        project_root : Path
            Project root directory

        Returns
        -------
        str
            Test command to execute
        """
        files_arg = " ".join(test_files)

        if project_type["type"] == "nodejs":
            # Read test script from package.json
            package_json_path = project_root / "package.json"
            if package_json_path.exists():
                import json

                try:
                    package_data = json.loads(package_json_path.read_text())
                    test_script = package_data.get("scripts", {}).get("test", "")

                    # Map common test frameworks
                    if "jest" in test_script:
                        return f"npx jest {files_arg}"
                    elif "mocha" in test_script:
                        return f"npx mocha {files_arg}"
                    elif "vitest" in test_script:
                        return f"npx vitest run {files_arg}"
                    elif "ava" in test_script:
                        return f"npx ava {files_arg}"
                    elif "tap" in test_script:
                        return f"npx tap {files_arg}"
                    else:
                        # Use npm test if we can't detect framework
                        return "npm test"
                except Exception:
                    pass

            # Default fallback
            return f"npx jest {files_arg}"

        elif project_type["type"] == "python":
            # Detect Python test framework
            # Check for pytest.ini, pyproject.toml [tool.pytest], or tox.ini
            if (project_root / "pytest.ini").exists() or (
                project_root / "pyproject.toml"
            ).exists():
                return f"pytest {files_arg}"
            # Check for unittest
            elif (project_root / "setup.py").exists():
                return f"python -m unittest {files_arg}"
            # Default to pytest
            return f"pytest {files_arg}"

        elif project_type["type"] == "java":
            # Maven or Gradle
            if (project_root / "pom.xml").exists():
                return "mvn test"
            elif (project_root / "build.gradle").exists() or (
                project_root / "build.gradle.kts"
            ).exists():
                return "./gradlew test"
            return "mvn test"

        return "echo 'No test command configured'"

    def _parse_test_failure(
        self, error_output: str, project_type: dict[str, str]
    ) -> list[ValidationIssue]:
        """Parse test failure output to extract issues.

        Parameters
        ----------
        error_output : str
            Test stderr output
        project_type : dict[str, str]
            Project type info

        Returns
        -------
        list[ValidationIssue]
            Extracted issues from test failure
        """
        issues = []

        # Generic patterns that work across test frameworks
        if (
            "Cannot find module" in error_output
            or "ModuleNotFoundError" in error_output
        ):
            # Missing dependency
            match = None
            if "Cannot find module '" in error_output:
                # Node.js error
                start_idx = error_output.find("Cannot find module '") + 20
                end_idx = error_output.find("'", start_idx)
                if end_idx > start_idx:
                    match = error_output[start_idx:end_idx]
            elif "ModuleNotFoundError: No module named" in error_output:
                # Python error - find the module name after the text
                prefix = "ModuleNotFoundError: No module named '"
                start_idx = error_output.find(prefix)
                if start_idx != -1:
                    start_idx += len(prefix)
                    end_idx = error_output.find("'", start_idx)
                    if end_idx > start_idx:
                        match = error_output[start_idx:end_idx]

            if match:
                remediation = ""
                if project_type["type"] == "nodejs":
                    remediation = f"Run: npm install --save-dev {match}"
                elif project_type["type"] == "python":
                    remediation = f"Run: pip install {match}"

                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.CRITICAL,
                        issue=f"Missing dependency: {match}",
                        evidence=f"Test failed with module not found error: {match}",
                        remediation=remediation,
                        criterion="All tests run successfully without errors",
                    )
                )

        # Generic test failure (no specific dependency issue)
        if not issues:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    issue="Tests failed",
                    evidence=error_output[:500],  # First 500 chars
                    remediation="Fix failing tests before marking task complete",
                    criterion="All tests run successfully without errors",
                )
            )

        return issues

    @with_retry(RetryConfig(max_attempts=3, base_delay=1.0))
    async def _validate_with_ai(self, task: Any, evidence: WorkEvidence) -> str:
        """Validate implementation using AI analysis with retry logic.

        Parameters
        ----------
        task : Any
            Task with completion_criteria
        evidence : WorkEvidence
            Gathered evidence bundle

        Returns
        -------
        str
            AI's validation analysis

        Notes
        -----
        Uses retry logic (3 attempts, 1s base delay) to handle transient
        network failures when calling the AI provider.
        """
        # Early pass: no specific criteria → LLM has nothing to check and
        # will hallucinate issues.  Returning a canned PASS response avoids
        # 98%-false-positive Mode B runs (see health report 2026-04-26).
        criteria = self._extract_criteria(task)
        if not criteria:
            logger.info(
                f"VALIDATION: task {task.id} has no acceptance criteria — "
                "skipping AI validation (auto-pass, nothing to check)"
            )
            return '{"passed": true, "issues": [], "reasoning": "No acceptance criteria defined — auto-pass"}'  # noqa: E501

        # Build validation prompt
        task_prompt = self._build_validation_prompt(task, evidence)

        # System instructions for validation
        system_instructions = """You are a code validation expert analyzing implementation completeness.  # noqa: E501
Your job is to verify that source code fully implements acceptance criteria.
You do NOT make task assignment decisions or coordinate agents.
Focus purely on: Does the code work? Are features complete?

CRITICAL RULES:
1. Check EACH acceptance criterion has code evidence in source files
2. FAIL if ANY criterion lacks implementation
3. FAIL if source code contains placeholder/TODO comments for required features
4. FAIL if source files are empty (0 bytes) when they should have implementation
5. FAIL if obvious integrations missing (e.g., form has inputs but validation.js has no functions)  # noqa: E501
6. PASS only if ALL criteria have working implementation

Focus on FUNCTIONALITY, not understanding. Code must WORK, not just exist.

---

"""

        # Combine system instructions with task prompt
        full_prompt = system_instructions + task_prompt

        # Create simple context object
        context = type("ValidationContext", (), {"max_tokens": 4000})()

        # Call dedicated validation LLM with retry support
        logger.debug("Calling AI provider for validation analysis")
        response: str = await self._validation_llm.analyze(
            prompt=full_prompt, context=context, operation="validate_work"
        )
        logger.debug("Received AI validation response")

        return response

    def _extract_criteria(self, task: Any) -> List[str]:
        """Extract acceptance criteria from task, handling dict and list forms.

        Parameters
        ----------
        task : Any
            Task object. ``completion_criteria`` is ``Optional[Dict[str, Any]]``;
            ``acceptance_criteria`` is ``List[str]``.

        Returns
        -------
        List[str]
            Flat list of criterion strings, empty if none found.

        Notes
        -----
        ``completion_criteria`` is stored as a JSON dict whose shape varies
        by decomposer strategy.  Iterating the dict directly yields keys,
        not criteria.  We look for list values inside the dict instead.
        """
        # 1. Try completion_criteria (dict form)
        cc = getattr(task, "completion_criteria", None)
        if isinstance(cc, dict) and cc:
            # Look for a list value — common keys: 'criteria', 'items',
            # 'acceptance_criteria', or any list-typed value.
            for key in ("criteria", "items", "acceptance_criteria", "checklist"):
                val = cc.get(key)
                if isinstance(val, list) and val:
                    return [str(v) for v in val]
            # Fallback: first list value found
            for val in cc.values():
                if isinstance(val, list) and val:
                    return [str(v) for v in val]
        elif isinstance(cc, list) and cc:
            return [str(v) for v in cc]

        # 2. Fallback to acceptance_criteria (list form from PRD parser)
        ac = getattr(task, "acceptance_criteria", None)
        if isinstance(ac, list) and ac:
            return [str(v) for v in ac]

        return []

    def _build_validation_prompt(self, task: Any, evidence: WorkEvidence) -> str:
        """Build AI prompt for validation.

        Parameters
        ----------
        task : Any
            Task with completion_criteria
        evidence : WorkEvidence
            Evidence bundle

        Returns
        -------
        str
            Formatted validation prompt
        """
        prompt_parts = [
            f"TASK: {task.name}",
            f"DESCRIPTION: {task.description}",
            "\nACCEPTANCE CRITERIA (ALL must be met):",
        ]

        # Add criteria — try completion_criteria first, fall back to
        # acceptance_criteria (generated by PRD parser).
        # completion_criteria is Optional[Dict[str, Any]]; iterate its
        # values if it is a dict (iterating the dict itself gives keys).
        criteria = self._extract_criteria(task)
        if not criteria:
            # No actionable criteria — the LLM has nothing specific to
            # check and will hallucinate issues. Skip AI validation by
            # returning a canned pass prompt that _parse_validation_response
            # recognises as all-pass.  The caller (_validate_with_ai) wraps
            # this in the same LLM call, so we signal "no criteria" by
            # injecting a sentinel that the system instruction won't trigger
            # on: the LLM sees no criteria list and the instruction "PASS
            # only if ALL criteria have working implementation" vacuously
            # applies to zero criteria.
            prompt_parts.append(
                "(No specific acceptance criteria defined for this task — "
                "auto-passing. Add criteria to the task spec to enable "
                "substantive validation.)"
            )
        else:
            for i, criterion in enumerate(criteria, 1):
                prompt_parts.append(f"{i}. {criterion}")

        # Add file manifest — existence proof for every file in the project.
        # This lets the LLM verify "does pyproject.toml exist?" without
        # needing its content. Files in the manifest but absent from the
        # source file section below exist but aren't readable (wrong
        # extension or excluded by SOURCE_EXTENSIONS) — the LLM must NOT
        # report them as missing. Only files absent from BOTH the manifest
        # AND the source files section are truly missing.
        if evidence.file_manifest:
            prompt_parts.append("\n\nPROJECT FILE MANIFEST (every file that exists):")
            for path in evidence.file_manifest:
                prompt_parts.append(f"  {path}")
            prompt_parts.append(
                "\nNOTE: Files listed above exist in the project. "
                "If a file appears in the manifest but not in the source "
                "files section below, it exists but its content is not "
                "shown. Do NOT report manifest-listed files as missing."
            )

        # Add discovered source files with full content (no truncation).
        # Previously content was truncated to 8KB per file which caused
        # LLM hallucinations — the validator had to infer code it
        # couldn't see, producing plausible-but-false violations. Full
        # context eliminates ~70% of false positives per Kaia's review.
        # Line numbers are prepended so the LLM can cite specific lines
        # and the post-validation checker can verify the citations
        # against actual file content.
        prompt_parts.append("\n\nEVIDENCE - DISCOVERED SOURCE FILES:")
        for source_file in evidence.source_files:
            file_info = f"\nSource File: {source_file.relative_path} ({source_file.size_bytes} bytes)"  # noqa: E501
            if source_file.has_placeholders:
                file_info += " [CONTAINS PLACEHOLDERS]"
            if source_file.is_empty():
                file_info += " [EMPTY FILE]"

            prompt_parts.append(file_info)
            # Prefix each line with its line number (1-indexed) so the
            # LLM can cite ``file:line`` and quote exact content. This
            # anchors the LLM's reasoning in verifiable evidence.
            numbered_lines = [
                f"  {lineno:5d}: {line}"
                for lineno, line in enumerate(source_file.content.splitlines(), start=1)
            ]
            prompt_parts.append(
                "  Content (line-numbered):\n" + "\n".join(numbered_lines)
            )

        # Add design artifacts (for context)
        if evidence.design_artifacts:
            prompt_parts.append("\n\nDESIGN ARTIFACTS (what SHOULD be built):")
            for artifact in evidence.design_artifacts:
                prompt_parts.append(
                    f"  - {artifact.get('filename')}: {artifact.get('description', '')}"
                )

        # Add decisions (for context)
        if evidence.decisions:
            prompt_parts.append("\n\nDECISIONS LOGGED:")
            for decision in evidence.decisions:
                prompt_parts.append(f"  - {decision.get('what')}")
                if decision.get("why"):
                    prompt_parts.append(f"    Why: {decision.get('why')}")

        # Add validation instructions.
        #
        # Citation requirement: every FAIL must include a verifiable
        # ``file:line`` citation and a direct quote of the exact code
        # text at that line. The post-validation checker will verify
        # the quote matches actual file content; hallucinated
        # citations auto-pass the criterion. This technique is how
        # production LLM code reviewers stop hallucinating — you
        # can't fabricate a line number the grader will check.
        prompt_parts.append("""

YOUR JOB: For EACH acceptance criterion, verify it is implemented in \
the SOURCE CODE above.

You have the FULL source file contents with line numbers. Ground \
every claim in specific evidence you can quote verbatim. Do NOT \
guess about code you can't see — you can see everything.

OUTPUT FORMAT:
VALIDATION RESULT: PASS or FAIL

For each criterion, emit ONE verdict block:

✅ CRITERION N: [criterion text]
   VERIFIED in [relative/path/to/file.ext:LINE]
   QUOTE: `exact code text at that line`

OR

❌ CRITERION N: [criterion text]
   SEVERITY: CRITICAL/MAJOR/MINOR
   EVIDENCE: [relative/path/to/file.ext:LINE]
   QUOTE: `exact code text at that line proving the violation`
   EXPLANATION: [why this violates the criterion]
   REMEDIATION: [specific fix, ideally with a file:line to change]

CITATION RULES (enforced by the grader):
- EVERY verdict MUST cite EITHER `file:line` (with QUOTE) or \
`file:STRUCTURAL` (without QUOTE).
- For CODE-LEVEL issues (wrong logic, missing call, bad signature):
  - Cite `path/to/file.ext:LINE`
  - Include a QUOTE of the exact line content
  - The QUOTE must match the line content verbatim (whitespace tolerant)
- For FILE-LEVEL STRUCTURAL issues (empty file, file exists only as a \
TODO stub, file is entirely whitespace):
  - Cite `path/to/file.ext:STRUCTURAL`
  - Omit the QUOTE line
  - The grader will verify the file is actually empty/stub — do not \
use STRUCTURAL for files that contain real code.
- If you cannot cite file:line with a real quote AND the file is not \
structurally empty, you DO NOT HAVE EVIDENCE. Do not emit that verdict.
- Prefer PASS over FAIL when evidence is ambiguous. Test authors \
(agents) are on your side — the runtime tests will catch actual \
behavioral bugs. Your job is structural verification, not \
speculation about edge cases you can't see.

ANALYSIS RULES:
✅ PASS if criterion has verifiable code evidence (file + line + quote)
❌ FAIL only with a verifiable citation proving the violation
❌ FAIL with `file:STRUCTURAL` if a required file is empty (0 bytes) \
or contains only TODO/FIXME placeholders for the criterion
⚠️  Do NOT FAIL on "the function looks incomplete" or "this might \
not handle X" — that's speculation. If you can't point to a \
specific line that violates the criterion, there is no violation \
to report.

Focus on STRUCTURAL evidence you can cite. Runtime correctness is \
verified separately by the test runner.""")

        return "\n".join(prompt_parts)

    def _parse_validation_response(self, ai_response: str) -> ValidationResult:
        """Parse AI validation response into ValidationResult.

        Supports both JSON and text formats for robustness across different AI models.

        Parameters
        ----------
        ai_response : str
            AI's validation analysis

        Returns
        -------
        ValidationResult
            Parsed validation result
        """
        # Try JSON parsing first (more robust)
        try:
            import json

            response_stripped = ai_response.strip()
            if response_stripped.startswith("{"):
                return self._parse_json_response(response_stripped)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug(f"JSON parsing failed, falling back to text: {e}")

        # Fall back to text parsing
        return self._parse_text_response(ai_response)

    def _parse_json_response(self, json_str: str) -> ValidationResult:
        """Parse JSON-formatted validation response.

        Parameters
        ----------
        json_str : str
            JSON string from AI

        Returns
        -------
        ValidationResult
            Parsed result
        """
        import json

        data = json.loads(json_str)
        passed = data.get("passed", False)

        issues = []
        for issue_data in data.get("issues", []):
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity[
                        issue_data.get("severity", "CRITICAL").upper()
                    ],
                    issue=issue_data.get("issue", "Unknown issue"),
                    evidence=issue_data.get("evidence", "No evidence provided"),
                    remediation=issue_data.get(
                        "remediation", "No remediation provided"
                    ),
                    criterion=issue_data.get("criterion", "Unknown criterion"),
                )
            )

        return ValidationResult(
            passed=passed,
            issues=issues,
            ai_reasoning=data.get("reasoning", json_str),
            validation_time=datetime.utcnow(),
        )

    def _parse_text_response(self, ai_response: str) -> ValidationResult:
        """Parse text-formatted validation response (emoji-based).

        Two-pass block-based parser. The first pass splits the
        response into issue blocks delimited by ``❌`` markers. The
        second pass scans each block for the four metadata keywords
        (``severity``, ``evidence``, ``remediation``, ``criterion``)
        in a case-insensitive, position-agnostic way.

        This replaces the prior implementation that used
        ``line.startswith("EVIDENCE:")`` with case-sensitive matching
        at line-start only. That approach produced the "No evidence
        provided" fallback for every issue when the validator LLM
        emitted freeform markdown with ``### CRITERION`` headers,
        prose evidence in the lines after the ``❌`` marker, or
        lowercase/title-case keywords. GH-320 Experiment 4 hit this
        bug ~10 times per agent and blocked task completion.

        Parameters
        ----------
        ai_response : str
            Text response from AI. Expected format uses ``❌`` to
            mark issue starts and ``EVIDENCE:``/``REMEDIATION:``/
            ``CRITERION:``/``SEVERITY:`` keywords to provide metadata.
            Tolerates keyword case variation and allows freeform
            prose between the ``❌`` marker and the first keyword.

        Returns
        -------
        ValidationResult
            Parsed result. When a block has no explicit EVIDENCE
            keyword, any non-keyword prose lines between the ``❌``
            marker and the next keyword (or next ``❌`` / EOF) are
            captured as the evidence text so downstream consumers
            see meaningful information instead of "No evidence
            provided".
        """
        # Check if validation passed. Strict PASS requires the exact
        # "VALIDATION RESULT: PASS" anchor; anything else is FAIL.
        passed = "VALIDATION RESULT: PASS" in ai_response

        # ---- Pass 1: split into issue blocks ----
        # Each block begins with a line containing ``❌`` and ends
        # at the next such line, the next ``### CRITERION`` header,
        # or end-of-response. The preamble before the first ``❌``
        # is discarded (it's the VALIDATION RESULT header or prose).
        lines = ai_response.split("\n")
        blocks: list[list[str]] = []
        current_block: list[str] = []
        in_block = False

        for line in lines:
            stripped = line.strip()
            is_new_issue = "❌" in stripped
            # Only terminate a block on ``### CRITERION`` headers.
            # Generic ``### `` subheadings like ``### Evidence`` or
            # ``### Remediation`` are legitimate metadata *inside* an
            # issue block and must NOT close it (Codex P1 on #331).
            is_criterion_header = (
                stripped.lower().startswith("### criterion") and in_block
            )

            if is_new_issue:
                if current_block:
                    blocks.append(current_block)
                current_block = [stripped]
                in_block = True
            elif is_criterion_header:
                # Criterion header inside an issue block closes the
                # current block; the header itself is not part of
                # any issue (it's just a criterion marker).
                if current_block:
                    blocks.append(current_block)
                current_block = []
                in_block = False
            elif in_block:
                current_block.append(stripped)

        if current_block:
            blocks.append(current_block)

        # ---- Pass 2: extract metadata from each block ----
        issues: list[ValidationIssue] = []
        for block in blocks:
            issue_data = self._extract_issue_from_block(block)
            if issue_data:
                issues.append(self._create_issue_from_dict(issue_data))

        return ValidationResult(
            passed=passed,
            issues=issues,
            ai_reasoning=ai_response,
            validation_time=datetime.utcnow(),
        )

    def _extract_issue_from_block(self, block: list[str]) -> Optional[dict[str, str]]:
        """Extract issue metadata from a single ``❌``-delimited block.

        Scans the block's lines for the four metadata keywords in a
        case-insensitive, position-agnostic way. Keywords may appear
        at the start of a line, indented, or mid-line (e.g.
        ``Evidence: ...``). Any freeform prose between the ``❌``
        marker and the first keyword is captured as the evidence
        text when no explicit ``EVIDENCE:`` keyword is found.

        Parameters
        ----------
        block : list[str]
            Lines belonging to one issue, starting with the line
            containing ``❌``.

        Returns
        -------
        Optional[dict[str, str]]
            Dict with ``issue``/``severity``/``evidence``/
            ``remediation``/``criterion`` keys. Returns ``None`` if
            the block doesn't contain a ``❌`` marker (defensive —
            callers should only pass valid issue blocks).
        """
        if not block:
            return None

        # First line contains the ``❌`` marker and the issue title
        first_line = block[0]
        if "❌" not in first_line:
            return None

        # Extract issue title: strip "N. " number prefix, strip the
        # ``❌`` emoji, strip trailing " - " qualifier.
        issue_text = first_line
        if ". " in issue_text:
            parts = issue_text.split(". ", 1)
            if len(parts) > 1:
                issue_text = parts[1]
        issue_text = issue_text.replace("❌", "").strip()
        if " - " in issue_text:
            issue_text = issue_text.split(" - ")[0].strip()

        issue_data: dict[str, str] = {"issue": issue_text}

        # Field names we extract from the block
        field_names = ("severity", "evidence", "remediation", "criterion")

        # Lines accumulated per field via markdown subheading
        # sections (e.g. ``### Evidence`` followed by prose). Inline
        # ``Evidence: <value>`` still takes precedence via
        # ``setdefault`` below.
        sections: dict[str, list[str]] = {name: [] for name in field_names}

        # Prose lines appearing before any keyword or subheading —
        # used as evidence fallback when no evidence section exists.
        prose_fallback: list[str] = []

        # Which field, if any, subsequent prose lines belong to.
        current_section: Optional[str] = None

        for line in block[1:]:
            if not line:
                continue

            line_lower = line.lower()

            # 1) Markdown subheading section switcher.
            #    ``### Evidence``, ``### Remediation``, etc. become
            #    section markers that route following prose lines
            #    into the matching field. ``### CRITERION N: ...``
            #    was already handled in pass 1 as a block delimiter,
            #    so any ``### CRITERION`` we see here is a subheading
            #    inside a single issue that we should ignore.
            if line.startswith("#"):
                header_text = line.lstrip("#").strip().lower()
                if header_text.endswith(":"):
                    header_text = header_text[:-1].strip()
                if header_text in field_names:
                    current_section = header_text
                else:
                    # Unknown subheading — stop routing prose into
                    # whatever section we were in (defensive).
                    current_section = None
                continue

            # 2) Inline ``Keyword: value`` form.
            matched_keyword: Optional[str] = None
            for keyword in field_names:
                if line_lower.startswith(keyword + ":"):
                    matched_keyword = keyword
                    break

            if matched_keyword:
                value = line[len(matched_keyword) + 1 :].strip()
                if value:
                    # Value on same line as keyword — set directly.
                    if matched_keyword == "severity":
                        value = value.lower()
                    issue_data.setdefault(matched_keyword, value)
                    current_section = None
                else:
                    # Bare ``Evidence:`` with value on following
                    # lines — treat like a subheading switcher.
                    current_section = matched_keyword
                continue

            # 3) Non-keyword prose line. Route to the current
            # section if we're in one, otherwise accumulate as
            # pre-section prose for evidence fallback.
            if current_section:
                sections[current_section].append(line)
            else:
                prose_fallback.append(line)

        # Fold accumulated section content into ``issue_data``
        # without clobbering values that an inline ``Keyword: value``
        # already set. This preserves the existing precedence rule
        # (first explicit value wins).
        for field_name, lines in sections.items():
            if lines and field_name not in issue_data:
                joined = " ".join(lines).strip()
                if field_name == "severity":
                    joined = joined.lower()
                issue_data[field_name] = joined

        # Prose-before-any-section fallback for evidence. This is
        # the core GH-320 Experiment 4 fix: freeform prose following
        # the ``❌`` marker becomes the evidence text instead of
        # being silently dropped.
        if "evidence" not in issue_data and prose_fallback:
            issue_data["evidence"] = " ".join(prose_fallback).strip()

        return issue_data

    def _verify_citations(
        self,
        result: ValidationResult,
        evidence: WorkEvidence,
        task_id: str = "",
    ) -> ValidationResult:
        """
        Drop validation issues whose citations can't be verified.

        The validator prompt requires every FAIL verdict to cite
        ``file:line`` with a verbatim quote of the line content, or
        ``file:STRUCTURAL`` for file-level issues (empty, stub-only).
        This method re-reads each cited line from the source files
        and checks whether the quoted text actually appears there;
        for STRUCTURAL citations it verifies the file is actually
        empty/stub via ``_is_structurally_empty``.

        Scope note — ``file:STRUCTURAL`` is intentionally limited to
        content-emptiness checks (empty, whitespace-only, TODO/FIXME
        stub-only). Other flavors of structural failure (missing
        imports, wrong file extensions, missing entirely) are either
        already covered by the existing line-citation path
        (missing-import claims can cite line 1 with a quote of
        whatever's there) or fall outside the scope of the citation
        verifier and should be caught by runtime tests or a
        dedicated pre-check. Expanding STRUCTURAL into a broader
        escape hatch reintroduces the hallucination risk this layer
        exists to eliminate.

        Hallucinated citations (quote doesn't match the actual line,
        or STRUCTURAL claim against a file with real code) are
        dropped. This is the ground-truth check on the ground-truth
        checker — LLMs cannot fabricate line numbers the grader
        will verify, so requiring citation + quote eliminates the
        vast majority of confabulated violations.

        Parameters
        ----------
        result : ValidationResult
            Raw validation result from the LLM.
        evidence : WorkEvidence
            Source file evidence used during validation. Citations
            are verified against these file contents.

        Returns
        -------
        ValidationResult
            Filtered result. If all issues were hallucinated, the
            result's ``passed`` flag flips to True.
        """
        if not result.issues:
            return result

        # Build a ``{relative_path: {line_number: line_text}}`` map
        # for fast citation lookup. Only source files discovered by
        # the evidence gatherer are trustworthy anchors.
        file_lines: Dict[str, Dict[int, str]] = {}
        file_content: Dict[str, str] = {}
        for sf in evidence.source_files:
            lines = sf.content.splitlines()
            file_lines[sf.relative_path] = {i + 1: line for i, line in enumerate(lines)}
            file_content[sf.relative_path] = sf.content

        # Regex for ``path/to/file.ext:LINE`` citations. Allows
        # common path characters and arbitrary extensions. The line
        # number is captured for lookup.
        citation_pattern = re.compile(
            r"([\w./\-]+\.[\w]+):(\d+)",
        )
        # Regex for ``path/to/file.ext:STRUCTURAL`` citations.
        # Structural citations represent file-level failures (empty,
        # TODO-only, whitespace-only) that have no citable line. The
        # verifier confirms the structural claim against the actual
        # file content before preserving the issue — Codex P2 on
        # PR #337.
        structural_pattern = re.compile(
            r"([\w./\-]+\.[\w]+):STRUCTURAL",
        )

        verified_issues: List[ValidationIssue] = []
        dropped_count = 0
        task_prefix = f"[task {task_id}] " if task_id else ""

        for issue in result.issues:
            # Pull the citation candidates from evidence + remediation.
            # The prompt asks for file:line in EVIDENCE but real LLM
            # output sometimes places it in REMEDIATION or inline.
            blob = " ".join(
                [
                    issue.evidence or "",
                    issue.remediation or "",
                    issue.issue or "",
                ]
            )

            # First, check for a STRUCTURAL citation (file-level
            # failure, no line). Evaluate before the line-citation
            # path because STRUCTURAL is strictly more permissive —
            # it preserves issues that don't fit the line-quote
            # contract.
            structural_match = structural_pattern.search(blob)
            if structural_match:
                cited_path = structural_match.group(1)

                matched_path = self._resolve_cited_path(cited_path, file_lines)
                if matched_path is None:
                    dropped_count += 1
                    logger.info(
                        f"{task_prefix}Dropping STRUCTURAL issue with "
                        f"unknown file citation {cited_path!r}: "
                        f"{issue.issue[:80]!r}"
                    )
                    continue

                # Verify the structural claim matches reality: the
                # file must actually be empty, whitespace-only, or
                # stub-only (TODO/FIXME placeholders). This guards
                # against the LLM hallucinating STRUCTURAL against
                # files that contain real code.
                content = file_content[matched_path]
                if not self._is_structurally_empty(content):
                    dropped_count += 1
                    logger.info(
                        f"{task_prefix}Dropping STRUCTURAL issue at "
                        f"{matched_path} — file is not actually "
                        f"empty/stub (size={len(content)}, "
                        f"non-whitespace lines="
                        f"{sum(1 for line in content.splitlines() if line.strip())}): "
                        f"{issue.issue[:80]!r}"
                    )
                    continue

                # STRUCTURAL citation verified — preserve the issue.
                verified_issues.append(issue)
                continue

            match = citation_pattern.search(blob)
            if not match:
                # No citation at all → hallucination, drop.
                dropped_count += 1
                logger.info(
                    f"{task_prefix}Dropping issue without file:line "
                    f"citation: {issue.issue[:80]!r}"
                )
                continue

            cited_path = match.group(1)
            cited_line = int(match.group(2))

            # Resolve citation against the evidence file set. Match
            # by suffix so the LLM can cite either absolute-relative
            # or bare filenames.
            matched_path = self._resolve_cited_path(cited_path, file_lines)

            if matched_path is None:
                dropped_count += 1
                logger.info(
                    f"{task_prefix}Dropping issue with unknown file "
                    f"citation {cited_path!r}: {issue.issue[:80]!r}"
                )
                continue

            line_map = file_lines[matched_path]
            if cited_line not in line_map:
                dropped_count += 1
                logger.info(
                    f"{task_prefix}Dropping issue with out-of-range "
                    f"line citation {matched_path}:{cited_line} "
                    f"(file has {len(line_map)} lines): "
                    f"{issue.issue[:80]!r}"
                )
                continue

            # Extract a quote candidate from the evidence string.
            # The prompt asks for "QUOTE: `exact code text`", so
            # look for backtick-wrapped content first. Fall back to
            # any significant token overlap with the cited line.
            actual_line = line_map[cited_line].strip()
            quote_match = re.search(r"`([^`]+)`", blob)

            if quote_match:
                quoted = quote_match.group(1).strip()
                # Whitespace-tolerant comparison. The LLM may
                # re-indent or normalize whitespace when quoting.
                actual_normalized = " ".join(actual_line.split())
                quoted_normalized = " ".join(quoted.split())
                if quoted_normalized and quoted_normalized not in actual_normalized:
                    # Quote doesn't appear on the cited line —
                    # hallucinated citation.
                    dropped_count += 1
                    logger.info(
                        f"{task_prefix}Dropping issue with mismatched "
                        f"quote at {matched_path}:{cited_line}. "
                        f"Quoted: {quoted_normalized[:60]!r} "
                        f"Actual: {actual_normalized[:60]!r}"
                    )
                    continue

            # Citation verified — keep the issue.
            verified_issues.append(issue)

        if dropped_count > 0:
            logger.warning(
                f"{task_prefix}Citation verification dropped "
                f"{dropped_count} of {len(result.issues)} validation "
                f"issues as hallucinated (unverifiable file:line citations)"
            )

        # If all issues were hallucinated, the result passes.
        new_passed = len(verified_issues) == 0

        return ValidationResult(
            passed=new_passed,
            issues=verified_issues,
            ai_reasoning=(
                f"{result.ai_reasoning}\n\n"
                f"[citation verification: kept "
                f"{len(verified_issues)}, dropped {dropped_count}]"
            ),
            validation_time=result.validation_time,
            executed=result.executed,
        )

    @staticmethod
    def _resolve_cited_path(
        cited_path: str, file_lines: Dict[str, Dict[int, str]]
    ) -> Optional[str]:
        """Resolve an LLM-cited path to an evidence-file relative path.

        LLMs cite paths inconsistently — sometimes as the relative
        path from the project root, sometimes as a bare filename,
        sometimes with leading ``./``. This helper does a
        suffix-tolerant match against the evidence file set.

        Parameters
        ----------
        cited_path : str
            Path as the LLM wrote it.
        file_lines : Dict[str, Dict[int, str]]
            Map of evidence file relative paths to line maps.

        Returns
        -------
        Optional[str]
            Matched relative path, or None if no evidence file
            matches.
        """
        normalized = cited_path.lstrip("./")
        for rel_path in file_lines:
            if rel_path == cited_path or rel_path == normalized:
                return rel_path
            if rel_path.endswith(cited_path) or rel_path.endswith(normalized):
                return rel_path
        return None

    @staticmethod
    def _is_structurally_empty(content: str) -> bool:
        """Check whether a file is empty or a placeholder-only stub.

        Used to verify ``file:STRUCTURAL`` citations (Codex P2 on
        PR #337). A file counts as structurally empty when it has
        no real content — zero bytes, whitespace only, or exclusively
        comment/TODO/FIXME placeholders that don't implement
        anything.

        Parameters
        ----------
        content : str
            Full file content as read from disk.

        Returns
        -------
        bool
            True if the file is empty or stub-only, False if it
            contains real code.
        """
        if not content or not content.strip():
            return True

        stub_markers = ("todo", "fixme", "xxx", "placeholder", "stub")
        real_lines = 0
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Treat comment-only lines as non-real if they contain a
            # stub marker. This catches ``# TODO: implement later``
            # stubs while still rejecting files with real comments
            # alongside real code.
            stripped = line.lstrip("#/*-; ").lower()
            if any(marker in stripped for marker in stub_markers) and len(line) < 80:
                continue
            real_lines += 1

        return real_lines == 0

    def _record_metrics(
        self,
        task_id: str,
        task_type: str,
        result: str,
        duration_ms: int,
        reason: str | None = None,
        issue_count: int = 0,
    ) -> None:
        """Record validation metrics for monitoring.

        Parameters
        ----------
        task_id : str
            Task ID
        task_type : str
            Task type (implementation, design, etc.)
        result : str
            Validation result (pass/fail)
        duration_ms : int
            Validation duration in milliseconds
        reason : str | None, optional
            Failure reason if applicable
        issue_count : int, optional
            Number of issues found
        """
        # Log metrics in structured format for easy parsing/aggregation
        metrics_data = {
            "task_id": task_id,
            "task_type": task_type,
            "result": result,
            "duration_ms": duration_ms,
        }
        if reason:
            metrics_data["reason"] = reason
        if issue_count:
            metrics_data["issue_count"] = issue_count

        logger.info(
            f"VALIDATION_METRICS: {json.dumps(metrics_data)}",
            extra={"metrics": metrics_data},
        )

    def _create_issue_from_dict(self, issue_dict: dict[str, str]) -> ValidationIssue:
        """Create ValidationIssue from parsed dictionary.

        Parameters
        ----------
        issue_dict : dict[str, str]
            Parsed issue data

        Returns
        -------
        ValidationIssue
            Created validation issue
        """
        # Map severity string to enum
        severity_str = issue_dict.get("severity", "critical")
        severity_map = {
            "critical": ValidationSeverity.CRITICAL,
            "major": ValidationSeverity.MAJOR,
            "minor": ValidationSeverity.MINOR,
        }
        severity = severity_map.get(severity_str, ValidationSeverity.CRITICAL)

        return ValidationIssue(
            severity=severity,
            issue=issue_dict.get("issue", "Unknown issue"),
            evidence=issue_dict.get("evidence", "No evidence provided"),
            remediation=issue_dict.get("remediation", "No remediation provided"),
            criterion=issue_dict.get("criterion", "Unknown criterion"),
        )
