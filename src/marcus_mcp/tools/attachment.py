"""
Artifact management tools with prescriptive storage locations.

This module provides tools to help agents store and track design
artifacts in organized locations while allowing flexibility when needed.
"""

import hashlib
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.core.project_history import ArtifactMetadata, ProjectHistoryPersistence

logger = logging.getLogger(__name__)

# Default artifact storage paths by type
ARTIFACT_PATHS = {
    "specification": "docs/specifications",
    "api": "docs/api",
    "design": "docs/design",
    "architecture": "docs/architecture",
    "documentation": "docs",
    "reference": "docs/references",
    "temporary": "tmp/artifacts",  # Should be .gitignored
}


async def log_artifact(
    task_id: str,
    filename: str,
    content: Union[str, Dict[str, Any], List[Any]],
    artifact_type: str,
    project_root: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,  # Optional override
    artifact_role: Optional[str] = None,
    state: Any = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Store an artifact with prescriptive location management.

    By default, artifacts are stored in standard locations based on
    their type. Marcus accepts ANY artifact type, making it domain-agnostic.

    Standard types (with predefined locations):
    - specification → docs/specifications/
    - api → docs/api/
    - design → docs/design/
    - architecture → docs/architecture/
    - documentation → docs/
    - reference → docs/references/
    - temporary → tmp/artifacts/

    Custom types:
    - Any other type → docs/artifacts/ (default fallback)
    - You can use domain-specific types like "podcast-script", "research",
      "video-storyboard", "marketing-copy", etc.

    Parameters
    ----------
    task_id : str
        The current task ID
    filename : str
        Name for the artifact file
    content : str or dict or list
        The artifact content to store. A ``dict`` or ``list`` is
        serialized to a pretty-printed JSON string before being written;
        a ``str`` is stored verbatim. Accepting structured input lets
        agents log parsed JSON (e.g. ``tsconfig.json``) directly without
        pre-stringifying it (issue #595 Fix 1).
    artifact_type : str
        Type of artifact (determines default location)
    project_root : Optional[str], optional
        Absolute path to the project root directory where artifacts
        will be created. All agents should use the same path.
    description : Optional[str], optional
        Optional description of the artifact
    location : Optional[str], optional
        Optional override for storage location (relative path)
    artifact_role : Optional[str], optional
        Semantic role of the artifact — ``"interface_contract"``,
        ``"implementation_spec"``, or ``"design_guide"``.  Stored on
        the artifact entry so ``_collect_task_artifacts`` can inject
        role-aware ``usage_guidance`` for dependent agents (Option C).
        When ``None``, guidance falls back to label-based detection
        (Option B).
    state : Any, optional
        MCP state object
    force : bool, default=False
        Bypass the size-downgrade guard. When False, writing content
        that is smaller than 50 % of an existing docs/ file whose size
        exceeds 8 KB is rejected to prevent accidental stub overwrites.
        Pass True only when an intentional downsize is desired.

    Returns
    -------
    Dict[str, Any]
        Dict with artifact location and storage details
    """
    try:
        # Issue #595 Fix 1: agents (and the generated MCP schema) may pass
        # structured JSON — e.g. a parsed tsconfig.json — as ``content``.
        # Serialize any dict/list to a JSON string up front, before the
        # size guard and file write, both of which require text.
        if isinstance(content, (dict, list)):
            content = json.dumps(content, indent=2)

        # Validate project_root is provided
        if not project_root:
            return {
                "success": False,
                "error": "project_root is required",
                "data": {"task_id": task_id, "filename": filename},
            }

        # Validate project_root is absolute and exists
        project_root_path = Path(project_root)
        if not project_root_path.is_absolute():
            return {
                "success": False,
                "error": "project_root must be an absolute path",
                "data": {"task_id": task_id, "filename": filename},
            }
        if not project_root_path.exists():
            return {
                "success": False,
                "error": f"project_root directory does not exist: {project_root}",
                "data": {"task_id": task_id, "filename": filename},
            }

        # Log info for non-standard artifact types (but still accept them!)
        standard_types = [
            "specification",
            "api",
            "design",
            "architecture",
            "documentation",
            "reference",
            "temporary",
        ]
        if artifact_type not in standard_types:
            logger.info(
                f"Using custom artifact type '{artifact_type}' for {filename}. "
                f"Will store in docs/artifacts/ (use 'location' parameter to override)"
            )

        # Determine storage location
        if location:
            # Use provided location (but ensure it's relative)
            artifact_path = Path(location)
            if artifact_path.is_absolute():
                return {
                    "success": False,
                    "error": "Location must be a relative path",
                    "data": {"task_id": task_id, "filename": filename},
                }
        else:
            # Use default location based on type
            base_dir = ARTIFACT_PATHS.get(artifact_type, "docs/artifacts")
            artifact_path = Path(base_dir) / filename

        # Create full path using project_root instead of Path.cwd()
        full_path = project_root_path / artifact_path

        # Normalise away any ".." traversal segments so the artifact-root
        # bypass check cannot be subverted via paths like
        # "docs/../src/theme.css".  strict=False because the target file
        # may not exist yet.
        try:
            _resolved_root = project_root_path.resolve()
            _resolved_full = full_path.resolve()
            _normalized_relative = _resolved_full.relative_to(_resolved_root)
        except ValueError:
            # Path resolves outside project root — reject unconditionally.
            return {
                "success": False,
                "error": (
                    f"Location resolves outside project root: {artifact_path}. "
                    "Use a path that stays within the project directory."
                ),
                "data": {"task_id": task_id, "filename": filename},
            }
        # Use the canonicalised paths for all downstream operations so
        # that filesystem writes and the git check target the same file.
        artifact_path = _normalized_relative
        full_path = _resolved_full

        # Guard: refuse to overwrite git-tracked source files.
        # log_artifact's contract is to persist artifacts (docs, reports,
        # design outputs) — not to overwrite source code managed by git.
        # dashboard-v82 post-mortem: Agent 1 overwrote theme.css and
        # design-tokens.json via log_artifact and had to restore from git
        # (commit d44dd5a). ``git ls-files --error-unmatch`` exits 0 only
        # when the path is tracked; non-zero means untracked or outside
        # the repo — both safe to write.
        #
        # Scope: only enforce for paths outside the known artifact output
        # roots (docs/ and tmp/). Files under those roots may legitimately
        # be tracked — a previous run committed them — and iterative
        # artifact refreshes must be allowed. Paths under src/, lib/, or
        # any other root are not artifact outputs and are guarded.
        # NOTE: artifact_path is already normalised above — parts[0] reflects
        # the true root even when the caller supplied ".." segments.
        _artifact_output_roots = {"docs", "tmp"}
        _in_artifact_dir = (
            len(artifact_path.parts) > 0
            and artifact_path.parts[0] in _artifact_output_roots
        )
        if not _in_artifact_dir:
            try:
                git_result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(project_root_path),
                        "ls-files",
                        "--error-unmatch",
                        str(full_path),
                    ],
                    capture_output=True,
                )
                if git_result.returncode == 0:
                    return {
                        "success": False,
                        "error": (
                            f"Refusing to overwrite git-tracked file: {artifact_path}. "
                            "log_artifact is for documentation artifacts, not source "
                            "files. Use a docs/ or tmp/ path for this artifact."
                        ),
                        "data": {"task_id": task_id, "filename": filename},
                    }
            except FileNotFoundError:
                # git not available in this environment — skip the guard.
                logger.debug(
                    "git not found; skipping tracked-file guard for %s", full_path
                )

        # Ensure directory exists
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Guard: refuse to silently replace a large docs/ file with
        # substantially smaller content (stub-overwrite prevention).
        # Threshold: existing >= 8 KB AND new < 50 % of existing.
        # Scoped to docs/ only — tmp/ artifacts are legitimately replaced
        # with compact summaries and must not be gated.
        # Bypassed when force=True so intentional downsizes are allowed.
        _SIZE_GUARD_THRESHOLD = 8_000  # bytes
        _in_docs_dir = len(artifact_path.parts) > 0 and artifact_path.parts[0] == "docs"
        if not force and _in_docs_dir and full_path.exists():
            existing_size = full_path.stat().st_size
            new_size = len(content.encode("utf-8"))
            if (
                existing_size >= _SIZE_GUARD_THRESHOLD
                and new_size < existing_size * 0.5
            ):
                return {
                    "success": False,
                    "error": (
                        f"Refusing to overwrite '{artifact_path}': existing file "
                        f"is {existing_size:,} bytes but new content is only "
                        f"{new_size:,} bytes (< 50 %). This looks like an accidental "
                        "stub overwrite. Pass force=True if this is intentional."
                    ),
                    "data": {"task_id": task_id, "filename": filename},
                }

        # Write content to file
        full_path.write_text(content, encoding="utf-8")

        # Initialize task_artifacts if not exists
        if not hasattr(state, "task_artifacts"):
            state.task_artifacts = {}

        if task_id not in state.task_artifacts:
            state.task_artifacts[task_id] = []

        # Log the artifact
        artifact_entry: Dict[str, Any] = {
            "filename": filename,
            "location": str(artifact_path),
            "artifact_type": artifact_type,
            "description": description,
            "is_default_location": location is None,
        }
        if artifact_role is not None:
            artifact_entry["artifact_role"] = artifact_role

        state.task_artifacts[task_id].append(artifact_entry)

        # v0.3.8.post1: invalidate the foundation-contract cache when an
        # artifact is logged to a foundation task. Without this, agents
        # would see a stale (up to TTL-seconds) foundation contract that
        # is missing the just-logged artifact.
        try:
            task = next(
                (t for t in (state.project_tasks or []) if t.id == task_id),
                None,
            )
            if (
                task is not None
                and getattr(task, "source_type", None) == "pre_fork_synthesis"
            ):
                from src.marcus_mcp.tools.context import (
                    invalidate_foundation_contract_cache,
                )

                pid = getattr(state, "current_project_id", None)
                invalidate_foundation_contract_cache(str(pid) if pid else None)
        except Exception:  # noqa: BLE001 - never break log_artifact on cache miss
            pass

        # Add a comment to the task if kanban is available
        if state.kanban_client and description:
            try:
                # Find the task
                task = next((t for t in state.project_tasks if t.id == task_id), None)
                if task:
                    card_id = getattr(task, "kanban_card_id", None) or task.id
                    location_type = "default" if location is None else "custom"
                    comment_text = (
                        f"📄 Created {artifact_type} artifact: {filename}\n"
                        f"Location: {artifact_path} ({location_type})\n\n"
                        f"{description}"
                    )
                    await state.kanban_client.add_comment(
                        task_id=card_id,
                        comment=comment_text,
                    )
            except Exception as e:
                logger.warning(f"Could not add comment: {e}")

        log_msg = (
            f"Stored {artifact_type} artifact {filename} for task "
            f"{task_id} at {artifact_path}"
        )
        logger.info(log_msg)

        # Record in active experiment if one is running
        from src.experiments.live_experiment_monitor import get_active_monitor

        monitor = get_active_monitor()
        if monitor and monitor.is_running:
            monitor.record_artifact(
                task_id=task_id,
                artifact_type=artifact_type,
                filename=filename,
                description=description or "",
            )

        # Persist to project history for post-project analysis
        await _persist_artifact_to_history(
            task_id=task_id,
            filename=filename,
            artifact_type=artifact_type,
            artifact_path=artifact_path,
            full_path=full_path,
            description=description,
            state=state,
        )

        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "filename": filename,
                "location": str(artifact_path),
                "full_path": str(full_path),
                "artifact_type": artifact_type,
                "is_default_location": location is None,
                "description": description,
            },
        }

    except Exception as e:
        logger.error(f"Error storing artifact: {str(e)}")
        return {
            "success": False,
            "error": f"Failed to store artifact: {str(e)}",
            "data": {"task_id": task_id, "filename": filename},
        }


async def _discover_artifacts_in_standard_locations(
    working_dir: Path,
) -> List[Dict[str, Any]]:
    """
    Scan standard artifact directories for files in project directory.

    Parameters
    ----------
    working_dir : Path
        The project root directory to scan for artifacts

    Returns
    -------
    List[Dict[str, Any]]
        List of discovered artifact dictionaries
    """
    discovered = []

    for artifact_type, base_path in ARTIFACT_PATHS.items():
        # Use working_dir instead of current directory
        path = working_dir / base_path
        if path.exists():
            try:
                for file_path in path.rglob("*"):
                    if file_path.is_file() and not file_path.name.startswith("."):
                        discovered.append(
                            {
                                "filename": file_path.name,
                                "location": str(file_path.relative_to(working_dir)),
                                "artifact_type": artifact_type,
                                "description": f"Discovered {artifact_type} file",
                                "is_default_location": True,
                            }
                        )
            except Exception as e:
                logger.warning(f"Error scanning {base_path}: {e}")

    return discovered


async def _persist_artifact_to_history(
    task_id: str,
    filename: str,
    artifact_type: str,
    artifact_path: Path,
    full_path: Path,
    description: Optional[str],
    state: Any,
) -> None:
    """
    Persist artifact metadata to project history for post-project analysis.

    Stores metadata about the artifact (not the content) to enable tracing
    what was produced during project execution.

    Parameters
    ----------
    task_id : str
        Task that produced the artifact
    filename : str
        Name of the artifact file
    artifact_type : str
        Type of artifact
    artifact_path : Path
        Relative path to artifact
    full_path : Path
        Absolute path to artifact
    description : Optional[str]
        Description of the artifact
    state : Any
        Marcus server state

    Notes
    -----
    Fails gracefully - errors are logged but don't interrupt the main flow.
    """
    try:
        # Get project info from state
        if not hasattr(state, "current_project_id") or not state.current_project_id:
            logger.debug("No active project - skipping project history persistence")
            return

        project_id = state.current_project_id
        project_name = getattr(state, "current_project_name", project_id)

        # Get agent ID from state if available
        agent_id = getattr(state, "current_agent_id", "unknown")

        # Initialize project history persistence if not already done
        if not hasattr(state, "project_history_persistence"):
            state.project_history_persistence = ProjectHistoryPersistence()

        # Calculate file size and hash for integrity checking
        file_size_bytes = 0
        sha256_hash = None
        if full_path.exists():
            file_size_bytes = full_path.stat().st_size
            # Calculate SHA256 hash
            sha256 = hashlib.sha256()
            with open(full_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            sha256_hash = sha256.hexdigest()

        # Get kanban comment URL if artifact was posted to kanban
        kanban_comment_url = None
        if hasattr(state, "last_kanban_comment_url"):
            kanban_comment_url = state.last_kanban_comment_url

        # Generate artifact ID
        now = datetime.now(timezone.utc)
        artifact_id = f"art_{task_id}_{now.timestamp()}"

        # Create artifact metadata
        artifact_metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            task_id=task_id,
            agent_id=agent_id,
            timestamp=now,
            filename=filename,
            artifact_type=artifact_type,
            relative_path=str(artifact_path),
            absolute_path=str(full_path),
            description=description or "",
            file_size_bytes=file_size_bytes,
            sha256_hash=sha256_hash,
            kanban_comment_url=kanban_comment_url,
            project_id=project_id,
        )

        # Persist to project history
        await state.project_history_persistence.append_artifact(
            project_id, project_name, artifact_metadata
        )

        logger.info(
            f"Persisted artifact {filename} to project history for {project_id}"
        )

    except Exception as e:
        # Graceful degradation - log but don't fail
        logger.warning(f"Failed to persist artifact to project history: {e}")
