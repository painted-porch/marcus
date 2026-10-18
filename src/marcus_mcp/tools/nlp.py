"""Natural Language Processing Tools for Marcus MCP.

This module contains tools for natural language project/task creation:
- create_project: Create complete project from natural language description
- add_feature: Add feature to existing project using natural language
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.event_loop_utils import EventLoopLockManager
from src.integrations.nlp_tools import add_feature_natural_language

logger = logging.getLogger(__name__)

# Serializes concurrent kanban_client initialization in create_project.
# Without this, simultaneous batch-experiment calls all evaluate
# need_new_client=True and race to overwrite state.kanban_client, leaving
# earlier callers with orphaned connections and triggering the dedup-guard
# loop (~720s stall).
#
# Uses EventLoopLockManager so each event loop gets its own lock — HTTP
# transport may run requests on different event loops, and a module-level
# asyncio.Lock would bind to the first loop that touches it and raise
# "is bound to a different event loop" on the second loop.
_kanban_init_lock_manager: EventLoopLockManager = EventLoopLockManager()

#: Process-wide mutex that serializes the entire ``create_project``
#: body across both event loops AND threads. Issue #610:
#: ``state.kanban_client`` is shared across calls, and
#: ``auto_setup_project`` mutates ``self.project_id`` on it (see
#: ``sqlite_kanban.py:352``). Two concurrent ``create_project`` calls
#: with different names would otherwise overwrite each other's
#: project_id mid-flight, commingling tasks from two projects on the
#: same board.
#:
#: Codex P1 on PR #613: an :class:`EventLoopLockManager` would only
#: serialize per-event-loop; in HTTP deployments with multiple
#: uvicorn workers (each running its own asyncio loop in the same
#: process) two requests on different loops could still race. Using a
#: :class:`threading.Lock` instead makes the mutex process-wide, so
#: every ``create_project`` in this Python process queues on the same
#: lock regardless of event loop.
#:
#: Acquisition is done via :func:`_acquire_create_project_lock`,
#: which polls non-blockingly so the event loop isn't blocked while
#: waiting for the lock — important because ``create_project`` can
#: take up to a minute at enterprise complexity.
_create_project_serialization_lock: threading.Lock = threading.Lock()


async def _acquire_create_project_lock() -> None:
    """Acquire :data:`_create_project_serialization_lock` cooperatively.

    Polls ``acquire(blocking=False)`` and yields to the event loop
    between attempts. ``threading.Lock`` is the right primitive for
    cross-loop / cross-thread mutual exclusion, but its blocking
    ``acquire()`` would freeze the calling event loop. The poll loop
    runs at 50 ms cadence, which is well below human-noticeable
    latency and negligible against the 60-second-plus work it
    protects.
    """
    while not _create_project_serialization_lock.acquire(blocking=False):
        await asyncio.sleep(0.05)


def _local_utc_offset_minutes() -> Optional[int]:
    """Return the host's current UTC offset in minutes, or None.

    A coarse geography proxy for Phase 0 forecasting (Marcus #546) —
    e.g. ``-480`` for US Pacific, ``60`` for Central Europe.  Carries
    no PII: an offset is shared by every machine in a time zone.
    Reads the host's local zone via ``astimezone()``.
    """
    try:
        # now(UTC).astimezone() (no arg) converts the aware UTC time
        # to the host's local zone; utcoffset() then yields the local
        # offset.  Starting from an aware datetime keeps it tz-correct.
        offset = datetime.now(timezone.utc).astimezone().utcoffset()
        if offset is None:
            return None
        return int(offset.total_seconds() // 60)
    except Exception:  # noqa: BLE001 - never break create_project
        return None


def _persist_phase0_open_signals(
    cost_store: Any,
    *,
    run_id: str,
    description: str,
    result: Dict[str, Any],
) -> None:
    """Write Phase 0 forecasting signals onto the ``runs`` row.

    Phase 0 of the ML cost-forecasting umbrella (Marcus #546).  Nine
    of the twelve forecasting columns are knowable by the time
    ``create_project`` returns — this helper persists them via
    ``CostStore.persist_phase0_run_signals``.  The remaining three
    (``did_complete``, ``completion_pct_final``,
    ``total_wall_time_seconds``) are derived later at run-close.

    Critically, this is called *after* ``record_run`` has INSERTed the
    ``runs`` row.  The planner intent-fidelity signals are gathered
    during decomposition but written here, not at emit time — at emit
    time the row does not exist yet and the UPDATE would match zero
    rows (Kaia review of #546).

    Best-effort: every error is swallowed so a persistence hiccup can
    never break ``create_project``.

    Parameters
    ----------
    cost_store : Any
        The ``CostStore`` instance on ``state``.
    run_id : str
        The run row to update — generated at ``create_project`` entry.
    description : str
        The raw project description; only its character length is
        persisted (``prd_length_chars``), never the text itself.
    result : dict
        The ``create_project`` return value.  Reads ``domain``,
        ``structural_category``, ``detected_tech_stack`` and the three
        intent-fidelity signals stashed by the decomposer.
    """
    try:
        from src.config.marcus_config import get_config

        # Shared local-provider taxonomy.  Lazy-imported (not a
        # top-level import) because ``server`` imports this tools
        # module — a load-time import back would be circular.  Using
        # the same set as the ``session_started`` telemetry path keeps
        # Phase 0 ``is_local_llm`` from disagreeing with it: both
        # ``"local"`` and ``"ollama"`` are local providers (Codex P2
        # review — Ollama runs were mislabelled as hosted).
        from src.marcus_mcp.server import _LOCAL_LLM_PROVIDERS

        provider = ""
        try:
            provider = str(get_config().ai.provider or "").lower()
        except Exception:  # noqa: BLE001 - config may be unavailable
            provider = ""

        tech = result.get("detected_tech_stack") or []
        tech_csv = ",".join(tech) if isinstance(tech, list) else None

        cost_store.persist_phase0_run_signals(
            run_id,
            prd_length_chars=len(description),
            detected_tech_stack=tech_csv,
            started_at_tz_offset_min=_local_utc_offset_minutes(),
            is_local_llm=(provider in _LOCAL_LLM_PROVIDERS),
            domain=result.get("domain"),
            structural_category=result.get("structural_category"),
            # Fidelity signals — None when outcome coverage did not
            # run; persist_phase0_run_signals' COALESCE guards leave
            # the columns NULL in that case.
            intent_fidelity_score=result.get("intent_fidelity_score"),
            coverage_before_fill=result.get("coverage_before_fill"),
            coverage_after_fill=result.get("coverage_after_fill"),
        )
    except Exception:  # noqa: BLE001 - never break create_project
        logger.exception("Phase 0 open-signal persistence failed for run %s", run_id)


async def _store_config_snapshot(
    state: Any,
    project_id: str,
    project_name: str,
    provider: str,
    complexity: str,
    options: Optional[Dict[str, Any]],
) -> None:
    """Store an immutable configuration snapshot for a project.

    Captures the AI model, provider settings, experiment complexity,
    and system features at project creation time. Written to marcus.db
    under the ``project_config`` collection for post-hoc analysis.

    Parameters
    ----------
    state : Any
        Marcus server state with config and AI engine.
    project_id : str
        Marcus registry project ID.
    project_name : str
        Human-readable project name.
    provider : str
        Kanban provider name (e.g. ``"sqlite"``, ``"planka"``).
    complexity : str
        Experiment complexity (``"prototype"``, ``"standard"``,
        ``"enterprise"``).
    options : Optional[Dict[str, Any]]
        Experiment options dict from create_project call.
    """
    try:
        from importlib.metadata import version as pkg_version

        from src.config.marcus_config import get_config
        from src.core.persistence import SQLitePersistence

        cfg = get_config()
        snap_db = Path(cfg.data_dir).expanduser() / "marcus.db"
        snap_persistence = SQLitePersistence(db_path=snap_db)

        # Discover available AI providers
        available_providers: list[str] = []
        if (
            hasattr(state, "ai_engine")
            and state.ai_engine
            and hasattr(state.ai_engine, "llm")
        ):
            llm = state.ai_engine.llm
            if hasattr(llm, "providers"):
                available_providers = list(llm.providers.keys())

        try:
            marcus_ver = pkg_version("marcus-mcp")
        except Exception:
            marcus_ver = "unknown"

        config_snapshot = {
            "project_id": project_id,
            "project_name": project_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ai": {
                "provider": cfg.ai.provider,
                "model": cfg.ai.model,
                "temperature": cfg.ai.temperature,
                "available_providers": available_providers,
            },
            "kanban": {
                "provider": provider,
            },
            "experiment": {
                "complexity": complexity,
                "num_agents": (options.get("team_size", 1) if options else 1),
            },
            "system": {
                "marcus_version": marcus_ver,
                "features": {
                    "events": cfg.features.events,
                    "context": cfg.features.context,
                    "memory": cfg.features.memory,
                },
            },
        }

        await snap_persistence.store("project_config", project_id, config_snapshot)
        logger.info(f"Stored config snapshot for project {project_id}")
    except Exception as e:
        logger.warning(f"Failed to store config snapshot: {e}")


# Track recent create_project calls for dedup detection.
# Value is (timestamp, result) — result is None while the call is in-flight,
# populated after successful creation so retries can return cached success.
_recent_create_project_calls: Dict[str, tuple[float, Optional[Dict[str, Any]]]] = {}


async def create_project(
    description: str, project_name: str, options: Optional[Dict[str, Any]], state: Any
) -> Dict[str, Any]:
    """Public entry point. Wraps the heavy work with cost-attribution.

    Marcus exposes ``create_project`` from two MCP entry points:
    legacy stdio (``handlers.py:handle_tool_call``) and FastMCP HTTP
    (``server.py:@app.tool()``). Both ultimately call this function.
    Putting the placeholder/rebind logic here covers every caller —
    including future ones — without depending on whichever wrapper
    happened to fire. Pre-PR-#515 the logic lived only in the legacy
    handler and never ran for HTTP clients, so all planner cost
    silently landed in 'unassigned' (#409 followup).

    Two-phase attribution
    ---------------------
    1. Push a placeholder ``PlannerContext`` with a freshly-generated
       ``run_id`` and a placeholder ``project_id`` (``pending:<uuid_hex>``)
       so heavy decomposition LLM calls land in ``token_events`` with
       a real, traceable run_id and a recoverable project_id rather
       than ``'unassigned'``.
    2. After the underlying work returns the real project_id:

       - Insert the ``runs`` row (``record_run``) carrying the
         pre-generated run_id, the real project_id, and the
         ``path`` value from ``options`` (or ``'direct'`` by default).
       - Rebind every placeholder ``project_id`` row to the canonical
         project_id. On failure both halves are skipped — the
         placeholder rows stay tagged ``pending:*`` for forensic
         inspection and no orphaned run row is created.

    Path discrimination
    -------------------
    Reads ``options.get("path", "direct")`` to label which entry
    point produced this run. Allowed values: ``"direct"`` (a human
    MCP user — the default), ``"marcus"`` (``/marcus`` meta-runner),
    ``"posidonius"`` (Posidonius automated trial). ``spawn_agents.py``
    sets the value in its YAML-driven config; Direct MCP callers
    don't need to do anything and get ``"direct"`` for free.

    Why the run_id is generated at entry rather than rebound later:
    rebinding ``run_id`` post-facto would require a new
    ``rebind_run_id`` method and another UPDATE. Generating the real
    ID from the start means the only post-success work is inserting
    the ``runs`` row and the existing ``rebind_project_id`` — same
    shape as before, one extra INSERT.

    Concurrency-safe by construction: random UUID per call prevents
    collisions; ContextVar scopes the push per asyncio task.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    from src.cost_tracking.cost_recorder import (
        PlannerContext,
        canonical_project_id,
        get_recorder,
    )
    from src.cost_tracking.cost_store import Run

    _placeholder = f"pending:{_uuid.uuid4().hex}"
    _display_name = f"{project_name} (creating)"
    _run_id = _uuid.uuid4().hex
    _started_at = datetime.now(timezone.utc)
    # Path discriminator — see docstring. Defaults to 'direct' so
    # human MCP callers get the right label without supplying it.
    _path = (options or {}).get("path", "direct")
    # Decomposer label for cost slicing (Marcus #519). Resolved here as
    # a fallback only — the authoritative value is the
    # ``actual_decomposer`` the inner function reports on success,
    # because contract_first can silently fall back to feature_based
    # (no project_root, weak contracts, empty domains) and the cost
    # row must reflect what RAN, not what was requested. See the
    # request-vs-reality fix in the result-building block below.
    #
    # resolve_decomposer honors options, the MARCUS_DECOMPOSER env
    # var, and validates the strategy name; unknown values resolve to
    # 'feature_based'. The no-env, no-options default is
    # 'feature_based' (see resolve_decomposer docstring) — but that
    # default never lands as a label, because the inner function
    # always reports its actual_decomposer on success.
    from src.config.decomposer_config import resolve_decomposer

    _requested_decomposer = resolve_decomposer(options)
    logger.debug(
        "cost_recorder: PUSHING placeholder context for create_project "
        "name=%s placeholder=%s run_id=%s path=%s",
        project_name,
        _placeholder,
        _run_id,
        _path,
    )
    # Issue #610: serialize ``_create_project_inner`` across the Marcus
    # process. Two concurrent calls (e.g. retry while a prior call is
    # still running, with a different name) would otherwise race on
    # ``state.kanban_client.project_id`` via ``auto_setup_project``
    # (``sqlite_kanban.py:352``) and commingle tasks from two projects
    # on the same board. Holding the mutex for the entire inner work
    # closes that race. Same-name retries that arrive AFTER the first
    # call completes hit the in-flight/cached dedup logic inside
    # ``_create_project_inner`` and short-circuit without doing real
    # work, so the mutex queue doesn't grow unbounded.
    #
    # Process-wide ``threading.Lock`` (not per-event-loop) so cross-
    # loop concurrency (multiple uvicorn workers) is also covered —
    # Codex P1 on PR #613.
    await _acquire_create_project_lock()
    try:
        with get_recorder().planner_context(
            PlannerContext(
                run_id=_run_id,
                project_id=_placeholder,
                project_name=_display_name,
            )
        ):
            result = await _create_project_inner(
                description, project_name, options, state
            )
    finally:
        _create_project_serialization_lock.release()

    # Phase 2: on success, record the runs row (now that we know the
    # real project_id) and rebind every placeholder project_id row.
    # Failure leaves both the placeholder rows and the orphaned
    # run_id behind for forensic inspection; the dashboard's picker
    # filters placeholder rows out of the main project list.
    #
    # Dedup-cached replays are skipped: the inner function returned a
    # cached success without doing any planner work, so the placeholder
    # context never accumulated token_events. Recording would just
    # insert a phantom zero-cost runs row per retry. Pop the internal
    # marker before returning so it never leaks to the agent. (Codex P2)
    _dedup_cached = (
        isinstance(result, dict) and result.pop("_dedup_cached", False) is True
    )
    if isinstance(result, dict) and result.get("success") and not _dedup_cached:
        _real_id = result.get("project_id")
        if _real_id:
            _canonical = canonical_project_id(str(_real_id))
            # Prefer the actual_decomposer the inner function reports
            # over the resolve-time requested value. Diverges when
            # contract_first falls back to feature_based — the cost row
            # must reflect what ran, not what was asked (Marcus #519
            # Kaia review).
            _actual = result.get("actual_decomposer")
            _decomposer = _actual if _actual else _requested_decomposer
            if _canonical and hasattr(state, "cost_store"):
                try:
                    state.cost_store.record_run(
                        Run(
                            run_id=_run_id,
                            project_id=_canonical,
                            project_name=project_name,
                            started_at=_started_at,
                            path=_path,
                            decomposer=_decomposer,
                        )
                    )
                    n = state.cost_store.rebind_project_id(
                        from_id=_placeholder,
                        to_id=_canonical,
                    )
                    state.cost_store.upsert_project_name(_canonical, project_name)
                    logger.info(
                        "create_project cost: recorded run_id=%s path=%s "
                        "and rebound %d events from placeholder to "
                        "project_id=%s",
                        _run_id,
                        _path,
                        n,
                        _canonical,
                    )
                    # Persist Phase 0 forecasting signals known at
                    # run-open time (Marcus #546).  Best-effort and
                    # COALESCE-guarded — a later fidelity write to the
                    # same row will not be clobbered.
                    _persist_phase0_open_signals(
                        state.cost_store,
                        run_id=_run_id,
                        description=description,
                        result=result,
                    )
                except Exception:
                    logger.exception(
                        "cost rebind/run-record failed for "
                        "create_project placeholder=%s real=%s "
                        "run_id=%s",
                        _placeholder,
                        _canonical,
                        _run_id,
                    )

    # Emit the ``project_created`` telemetry event (Marcus #416,
    # Stage 2 of #9).  Best-effort; the helper swallows all errors
    # so a telemetry hiccup cannot break create_project.
    #
    # Skipped on the dedup-cache replay path: a retry that hits the
    # 10-minute cached-success window did not create a new project,
    # so firing here would double-count one creation as two
    # project_created events — inflating creation / fallback
    # analytics (Codex P2 review).  Gated on the same ``_dedup_cached``
    # flag that already skips the ``record_run`` cost row above.
    if isinstance(result, dict) and result.get("success") and not _dedup_cached:
        from src.telemetry.events import fire_project_created

        fire_project_created(
            result=result,
            options=options,
            actual_decomposer=result.get("actual_decomposer"),
            # Requested strategy — lets the event derive ``was_fallback``
            # (contract_first asked for, feature_based actually ran).
            requested_decomposer=_requested_decomposer,
        )

    return result


def _resolve_project_info_path(options: Dict[str, Any]) -> Optional[str]:
    """Resolve where Marcus writes ``project_info.json`` for the runner.

    The experiment runner waits for ``<experiment_dir>/project_info.json``
    as its go-signal and instructs the project-creator agent to pass
    ``project_info_path`` in the ``create_project`` options. A
    lower-fidelity agent can drop that single field from a long options
    object; when it does, Marcus has no path, silently skips the write,
    and the runner times out without ever spawning a work agent (the
    snake-pr673 / test92 failure).

    To make the handshake independent of agent fidelity, fall back to
    deriving the path from ``project_root`` (which survives reliably): the
    runner sets ``project_root == <experiment_dir>/implementation``, so the
    info file sits one level up.

    Parameters
    ----------
    options : Dict[str, Any]
        The ``create_project`` options dict.

    Returns
    -------
    Optional[str]
        The resolved path, or ``None`` when neither ``project_info_path``
        nor ``project_root`` is usable (caller then skips the write).
    """
    explicit = options.get("project_info_path")
    if explicit:
        return str(explicit)
    project_root = options.get("project_root")
    if project_root:
        return str(Path(project_root).parent / "project_info.json")
    return None


async def _create_project_inner(
    description: str, project_name: str, options: Optional[Dict[str, Any]], state: Any
) -> Dict[str, Any]:
    """
    Create a NEW project from natural language description.

    Internal implementation invoked by :func:`create_project`. The
    public wrapper owns cost attribution; this function does the
    actual decomposition + persistence work.

    This tool ALWAYS creates a new project - it does not search for or reuse
    existing projects. For working with existing projects, use select_project
    or create_tasks tools instead.

    Uses AI to parse natural language project requirements and automatically:
    - Breaks down into tasks and subtasks
    - Assigns priorities and dependencies
    - Estimates time requirements
    - Creates organized kanban board structure
    - Registers the new project in Marcus

    Parameters
    ----------
    description : str
        Natural language project description
    project_name : str
        Name for the new project
    options : Optional[Dict[str, Any]]
        Optional configuration dictionary with the following keys:

        Provider Config:
        - provider (str): Kanban provider - "planka" (default), "github", "linear"
        - planka_project_name (str): Custom Planka project name
          (defaults to project_name)
        - planka_board_name (str): Custom Planka board name
          (defaults to "Main Board")

        Project Settings:
        - project_root (str): **REQUIRED** Absolute path to project directory
          where implementation files will be created
          Example: "/Users/username/experiments/myproject/implementation"
          This is critical for the validation system to locate source files.
        - complexity (str): "prototype", "standard" (default), "enterprise"
        - deployment (str): "none" (default), "internal", "production"
        - team_size (int): Team size 1-20 for estimation (default: 1)
        - tech_stack (List[str]): Technologies to use (e.g., ["React", "Python"])
        - deadline (str): Project deadline in YYYY-MM-DD format
        - tags (List[str]): Tags for project organization
        - decomposer (str): Task decomposition strategy. One of:
            - "contract_first" — generates domain contracts before
              decomposition; each task owns a contract interface.
              Recommended for projects where agents must coordinate
              against shared interfaces (most multi-agent runs).
              Requires ``project_root``.
            - "feature_based" (default) — shapes tasks directly from
              functional requirements. Faster to plan; weaker
              coordination signal. Falls back here automatically if
              ``contract_first`` is selected but ``project_root`` is
              absent or contract generation fails.
          Overrides the ``MARCUS_DECOMPOSER`` environment variable.

        Runner Integration:
        - project_info_path (str): Absolute path where Marcus should write
          project_info.json. When set, Marcus writes project_id, board_id,
          tasks_created, and recommended_agents to this file at the end of
          create_project. The experiment runner reads from this file instead
          of querying Marcus via a second HTTP session (which was racy).

        Legacy Options:
        - deployment_target (str): "local", "dev", "prod", "remote"
          (mapped to deployment setting for backwards compatibility)
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        On success:
        {
            "success": True,
            "project_id": str,  # Marcus project ID
            "tasks_created": int,
            "board": {
                "project_id": str,  # Provider project ID
                "board_id": str,
                "provider": str
            },
            "phases": List[str],
            "estimated_duration": str,
            "complexity_score": float
        }

        On error:
        {
            "success": False,
            "error": str
        }

    Examples
    --------
    Create new project with defaults:
        >>> create_project(
        ...     description="Build a REST API with user authentication",
        ...     project_name="MyAPI"
        ... )

    Create with advanced configuration:
        >>> create_project(
        ...     description="E-commerce platform with payment integration",
        ...     project_name="ShopFlow",
        ...     options={
        ...         "project_root": "/Users/agent/projects/shopflow/implementation",
        ...         "complexity": "enterprise",
        ...         "deployment": "production",
        ...         "team_size": 5,
        ...         "tech_stack": ["React", "Node.js", "PostgreSQL"],
        ...         "tags": ["client:acme", "priority:high"]
        ...     }
        ... )
    """
    # Easter eggs: instant return before any processing or dedup registration.
    _name_lower = project_name.strip().lower()
    if _name_lower == "why":
        from src.marcus_mcp.tools.easter_eggs import easter_egg_why

        return easter_egg_why()
    if _name_lower == "snake":
        from src.marcus_mcp.tools.easter_eggs import easter_egg_snake

        return easter_egg_snake()
    if _name_lower == "quokka":
        from src.marcus_mcp.tools.easter_eggs import easter_egg_quokka_project

        return easter_egg_quokka_project()

    import time
    import traceback

    call_stack = "".join(traceback.format_stack())
    logger.warning(
        f"[CREATE_PROJECT INVOKED] project_name={project_name!r} "
        f"caller_stack:\n{call_stack}"
    )

    # Dedup guard: reject duplicate calls for the same project within 10 minutes.
    # When the first call already succeeded, return the cached result so the
    # agent can proceed (avoids "project created but agent never got the ID"
    # caused by Claude Code MCP timeouts triggering retries during slow creation).
    dedup_key = f"{project_name}:{description[:50]}"
    now = time.time()
    if dedup_key in _recent_create_project_calls:
        ts, cached_result = _recent_create_project_calls[dedup_key]
        elapsed = now - ts
        if elapsed < 600:  # 10 minute window
            if cached_result is not None:
                # First call succeeded — return its result so the agent proceeds
                logger.warning(
                    f"[CREATE_PROJECT DEDUP] Returning cached result for "
                    f"{project_name!r} — first call completed {elapsed:.0f}s ago"
                )
                # Re-write project_info.json so the runner can proceed even
                # when it deleted the file at startup and retried create_project
                # within the 10-minute dedup window (Codex P1 fix).
                if isinstance(options, dict) and options.get("project_info_path"):
                    import json as _json
                    from pathlib import Path as _Path

                    _info_path = _Path(options["project_info_path"])
                    _board_id = ""
                    if hasattr(state, "kanban_client") and state.kanban_client:
                        _board_id = str(
                            getattr(state.kanban_client, "board_id", "") or ""
                        )
                    _info_data: Dict[str, Any] = {
                        "project_id": cached_result.get("project_id", ""),
                        "board_id": _board_id,
                        "tasks_created": cached_result.get("tasks_created", 0),
                        "recommended_agents": cached_result.get(
                            "recommended_agents", 0
                        ),
                    }
                    try:
                        _info_path.parent.mkdir(parents=True, exist_ok=True)
                        _info_path.write_text(_json.dumps(_info_data, indent=2))
                        logger.info(
                            f"[create_project dedup] Re-wrote project_info.json "
                            f"to {_info_path}"
                        )
                    except Exception as _dedup_write_err:
                        logger.warning(
                            f"[create_project dedup] Failed to write "
                            f"project_info.json: {_dedup_write_err}"
                        )
                # Signal to the wrapper that this success is a cached
                # replay, not fresh work. The wrapper uses this to skip
                # `record_run` so retries don't pile phantom zero-cost
                # rows in the `runs` table. Shallow-copy first so the
                # cache entry stays clean for any subsequent dedup hit
                # (and so callers don't see the internal flag if the
                # wrapper somehow forgets to pop it).
                _replay = dict(cached_result)
                _replay["_dedup_cached"] = True
                return _replay
            else:
                # First call still in-flight — block the duplicate
                logger.warning(
                    f"[CREATE_PROJECT DEDUP] Rejecting duplicate call for "
                    f"{project_name!r} — first call is still in progress "
                    f"({elapsed:.0f}s ago)"
                )
                return {
                    "success": False,
                    "error": (
                        f"create_project for '{project_name}' is still running "
                        f"(started {elapsed:.0f}s ago). Wait for it to complete."
                    ),
                }
    # Mark call as in-flight (result=None) so concurrent duplicates are blocked.
    # The entry is overwritten with (timestamp, result) after successful completion.
    # On any non-success exit (validation failure, exception) the key is deleted
    # so that legitimate retries are not blocked for the full 10-minute window.
    _recent_create_project_calls[dedup_key] = (now, None)

    # Validate required parameters
    if (
        not description
        or not description.strip()
        or description.lower() in ["test", "dummy", "example", "help"]
    ):
        del _recent_create_project_calls[dedup_key]
        return {
            "success": False,
            "error": "Please provide a real project description",
            "usage": {
                "description": "Natural language description of what you want to build",
                "project_name": "A short name for your project",
                "options": {
                    "complexity": "prototype | standard | enterprise",
                    "deployment": "none | internal | production",
                    "team_size": "1-20 (optional)",
                    "tech_stack": ["array", "of", "technologies"],
                    "deadline": "YYYY-MM-DD",
                },
            },
            "examples": [
                {
                    "description": (
                        "Create a task management app with user "
                        "authentication and team collaboration"
                    ),
                    "project_name": "TeamTasks",
                    "options": {
                        "complexity": "standard",
                        "deployment": "internal",
                        "planka_project_name": "Team Tasks Project",
                        "planka_board_name": "Development Board",
                    },
                },
                {
                    "description": "Build a simple blog with markdown support",
                    "project_name": "MiniBlog",
                    "options": {"complexity": "prototype", "deployment": "none"},
                },
                {
                    "description": (
                        "Develop an e-commerce platform with inventory "
                        "management, payment processing, and analytics"
                    ),
                    "project_name": "ShopFlow",
                    "options": {
                        "complexity": "enterprise",
                        "deployment": "production",
                        "tech_stack": [
                            "React",
                            "Node.js",
                            "PostgreSQL",
                            "Stripe",
                        ],
                        "team_size": 5,
                        "planka_project_name": "ShopFlow Production",
                    },
                },
            ],
        }

    if not project_name or not project_name.strip():
        return {
            "success": False,
            "error": "Missing required parameter: 'project_name'",
            "hint": "Please provide a name for your project",
        }

    # Validate options if provided
    if options:
        # Validate complexity
        if "complexity" in options:
            valid_complexity = ["prototype", "standard", "enterprise"]
            if options["complexity"] not in valid_complexity:
                return {
                    "success": False,
                    "error": f"Invalid complexity: '{options['complexity']}'",
                    "valid_options": valid_complexity,
                    "description": {
                        "prototype": "Quick MVP with minimal features (3-8 tasks)",
                        "standard": "Full-featured project (10-20 tasks)",
                        "enterprise": "Production-ready with all features (25+ tasks)",
                    },
                }

        # Validate deployment
        if "deployment" in options:
            valid_deployment = ["none", "internal", "production"]
            if options["deployment"] not in valid_deployment:
                return {
                    "success": False,
                    "error": f"Invalid deployment: '{options['deployment']}'",
                    "valid_options": valid_deployment,
                    "description": {
                        "none": "Local development only",
                        "internal": "Deploy for team/staging use",
                        "production": "Deploy for customers with full infrastructure",
                    },
                }

        # Validate team_size
        if "team_size" in options:
            try:
                team_size = int(options["team_size"])
                if team_size < 1 or team_size > 20:
                    return {
                        "success": False,
                        "error": "team_size must be between 1 and 20",
                        "current_value": options["team_size"],
                    }
            except (ValueError, TypeError):
                return {
                    "success": False,
                    "error": "team_size must be a number",
                    "current_value": options["team_size"],
                }

    try:
        # Create project using natural language processing
        from src.integrations.nlp_tools import NaturalLanguageProjectCreator

        # Log before calling the function
        state.log_event("create_project_calling", {"project_name": project_name})

        # Get subtask_manager if available
        subtask_manager = getattr(state, "subtask_manager", None)

        # Extract complexity from options (default to "standard")
        # Restored from deleted pipeline_tracked_nlp.py
        complexity = "standard"
        if options:
            complexity = options.get("complexity", "standard")

        # Initialize or replace kanban client to match requested provider
        from src.config.marcus_config import get_config
        from src.integrations.kanban_factory import KanbanFactory
        from src.integrations.kanban_interface import KanbanProvider

        cfg_provider = get_config().kanban.provider or "sqlite"
        requested_provider = (
            options.get("provider", cfg_provider) if options else cfg_provider
        )

        # Check if current client matches requested provider — serialize under
        # the per-loop kanban init lock so concurrent create_project calls
        # don't race to overwrite state.kanban_client with partially-
        # initialized instances. Resolved at call time so each event loop
        # gets its own lock (HTTP transport may use multiple loops).
        async with _kanban_init_lock_manager.get_lock():
            current_provider = None
            if state.kanban_client and hasattr(state.kanban_client, "provider"):
                current_provider = (
                    state.kanban_client.provider.value
                    if isinstance(state.kanban_client.provider, KanbanProvider)
                    else str(state.kanban_client.provider)
                )

            need_new_client = (
                not state.kanban_client or current_provider != requested_provider
            )

            if need_new_client:
                try:
                    state.kanban_client = KanbanFactory.create(requested_provider)
                    if hasattr(state.kanban_client, "connect"):
                        await state.kanban_client.connect()
                    logger.info(
                        f"Initialized kanban client "
                        f"(provider={requested_provider}, "
                        f"was={current_provider}) "
                        f"for new project '{project_name}'"
                    )
                except Exception as e:
                    logger.error(f"Failed to initialize kanban client: {e}")
                    return {
                        "success": False,
                        "error": (
                            f"Failed to initialize kanban provider "
                            f"'{requested_provider}': {e}"
                        ),
                    }

        # Clear stale project/board IDs to force new project creation
        # Restored from deleted pipeline_tracked_nlp.py
        # The creator checks if these are set and skips creation if they are
        # CRITICAL: This affects task and subtask creation ordering
        # NOTE: SQLite provider doesn't have project_id/board_id attributes
        if state.kanban_client:
            if hasattr(state.kanban_client, "client"):
                state.kanban_client.client.project_id = None
                state.kanban_client.client.board_id = None
            elif hasattr(state.kanban_client, "project_id"):
                state.kanban_client.project_id = None
                state.kanban_client.board_id = None
            logger.info(
                f"Creating NEW project '{project_name}' (complexity={complexity})"
            )

        # Initialize project creator
        creator = NaturalLanguageProjectCreator(
            kanban_client=state.kanban_client,
            ai_engine=state.ai_engine,
            subtask_manager=subtask_manager,
            complexity=complexity,
            state=state,
        )

        # Ensure we await the result properly
        try:
            result: Dict[str, Any] = await creator.create_project_from_description(
                description=description,
                project_name=project_name,
                options=options or {},
            )
        except Exception as e:
            state.log_event(
                "create_project_exception",
                {
                    "project_name": project_name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            # Re-raise to be handled by outer try/catch
            raise
        else:
            # Log after function returns
            state.log_event(
                "create_project_returned",
                {
                    "project_name": project_name,
                    "success": result.get("success", False) if result else False,
                    "task_count": result.get("tasks_created", 0) if result else 0,
                },
            )

        # Log immediately before returning to MCP
        state.log_event(
            "create_project_returning_to_mcp",
            {
                "project_name": project_name,
                "result_keys": (
                    list(result.keys()) if isinstance(result, dict) else "not_dict"
                ),
                "success": (
                    result.get("success", False) if isinstance(result, dict) else False
                ),
            },
        )

        # Register project with Marcus registry
        # (restored from deleted pipeline_tracked_nlp.py)
        if result.get("success") and hasattr(state, "kanban_client"):
            try:
                from src.config.marcus_config import get_config
                from src.core.project_registry import ProjectConfig

                # Default from config, experiment options can override
                default_prov = get_config().kanban.provider or "sqlite"
                provider = (
                    options.get("provider", default_prov) if options else default_prov
                )

                # Get provider-specific project name
                # (may differ from Marcus project name)
                provider_project_name = project_name
                provider_board_name = "Main Board"

                # For Planka, extract actual names if available
                if provider == "planka" and result.get("board"):
                    board_info = result["board"]
                    provider_project_name = board_info.get("project_name", project_name)
                    provider_board_name = board_info.get("board_name", "Main Board")

                # Build provider_config based on provider type
                if provider == "sqlite":
                    kanban_config = get_config().kanban
                    prov_config: Dict[str, Any] = {
                        "db_path": (kanban_config.sqlite_db_path or "./data/kanban.db"),
                        "project_name": provider_project_name,
                        "attachments_dir": (
                            kanban_config.sqlite_attachments_dir or "./data/attachments"
                        ),
                    }
                    # Include project/board IDs and project_root from
                    # the client so project scoping and workspace state
                    # work after project switch creates a new client
                    if hasattr(state.kanban_client, "project_id"):
                        prov_config["project_id"] = state.kanban_client.project_id
                    if hasattr(state.kanban_client, "board_id"):
                        prov_config["board_id"] = state.kanban_client.board_id
                    if hasattr(state.kanban_client, "_project_root"):
                        prov_config["project_root"] = state.kanban_client._project_root
                    can_register = True
                elif (
                    hasattr(state.kanban_client, "project_id")
                    and state.kanban_client.project_id
                    and hasattr(state.kanban_client, "board_id")
                    and state.kanban_client.board_id
                ):
                    prov_config = {
                        "project_id": str(state.kanban_client.project_id),
                        "project_name": provider_project_name,
                        "board_id": str(state.kanban_client.board_id),
                        "board_name": provider_board_name,
                    }
                    can_register = True
                else:
                    can_register = False

                if can_register:
                    # Use the kanban project ID as the single source
                    # of truth (GH-306: dual project ID mismatch).
                    # This ensures decisions/artifacts are stored under
                    # the same ID that tasks reference.
                    kanban_project_id = prov_config.get(
                        "project_id",
                        getattr(state.kanban_client, "project_id", ""),
                    )
                    project_config = ProjectConfig(
                        id=str(kanban_project_id) if kanban_project_id else "",
                        name=f"{provider_project_name} - {provider_board_name}",
                        provider=provider,
                        provider_config=prov_config,
                        created_at=datetime.now(timezone.utc),
                        last_used=datetime.now(timezone.utc),
                        tags=["auto-created", provider],
                    )

                    # Register with Marcus registry
                    if hasattr(state, "project_registry"):
                        marcus_project_id = await state.project_registry.add_project(
                            project_config
                        )
                        logger.info(
                            f"Registered new project in registry: {marcus_project_id}"
                        )

                        # Switch to new project
                        # (this also refreshes state and wires dependencies)
                        if hasattr(state, "project_manager"):
                            await state.project_manager.switch_project(
                                marcus_project_id
                            )
                            state.kanban_client = (
                                await state.project_manager.get_kanban_client()
                            )

                            # Reset migration flag for new project
                            if hasattr(state, "_subtasks_migrated"):
                                state._subtasks_migrated = False

                        # Add Marcus project_id to result for auto-select functionality
                        result["project_id"] = marcus_project_id

                        # Store immutable config snapshot
                        await _store_config_snapshot(
                            state,
                            marcus_project_id,
                            project_name,
                            provider,
                            complexity,
                            options,
                        )

                        # Backfill project_id on tasks just created
                        # (tasks are persisted before registration)
                        try:
                            from src.core.persistence import SQLitePersistence

                            cfg = get_config()
                            db_path = Path(cfg.data_dir).expanduser() / "marcus.db"
                            persistence = SQLitePersistence(db_path=db_path)
                            task_ids = result.get("task_ids", [])
                            if not task_ids:
                                created = result.get("created_tasks", [])
                                task_ids = [
                                    str(
                                        t.get("id", t)
                                        if isinstance(t, dict)
                                        else getattr(t, "id", t)
                                    )
                                    for t in created
                                ]
                            for tid in task_ids:
                                existing = await persistence.retrieve(
                                    "task_metadata", str(tid)
                                )
                                if existing:
                                    existing["project_id"] = marcus_project_id
                                    await persistence.store(
                                        "task_metadata", str(tid), existing
                                    )
                            if task_ids:
                                logger.info(
                                    f"Backfilled project_id on "
                                    f"{len(task_ids)} tasks"
                                )
                        except Exception as backfill_err:
                            logger.warning(
                                f"Failed to backfill project_id: " f"{backfill_err}"
                            )
                    else:
                        logger.warning(
                            "ProjectRegistry not available - project not registered"
                        )
                else:
                    logger.warning(
                        "Could not extract project/board IDs from kanban_client - "
                        "project not registered in Marcus"
                    )
            except Exception as e:
                # Log but don't fail the operation
                logger.warning(f"Failed to register project with Marcus: {str(e)}")

        # Normalize result to include task_count
        if isinstance(result, dict):
            if "tasks_created" in result and "task_count" not in result:
                result["task_count"] = result["tasks_created"]

            # Add Marcus project_id from registry for auto-select
            if result.get("success") and "project_id" not in result:
                if hasattr(state, "project_registry"):
                    active_project = await state.project_registry.get_active_project()
                    if active_project:
                        result["project_id"] = active_project.id
                        logger.info(
                            f"Added project_id to result: {result['project_id']}"
                        )

        # Final log before return
        state.log_event(
            "create_project_final_return",
            {
                "project_name": project_name,
                "returning": True,
                "result_type": type(result).__name__,
            },
        )

        # No need to wait for background tasks
        # They are fire-and-forget for tracking purposes

        # Auto-select the newly created project
        if result.get("success") and result.get("project_id"):
            from .project_management import select_project

            logger.info(
                f"🔄 AUTO-SELECT STARTING for project '{project_name}' "
                f"(ID: {result['project_id']})"
            )
            state.log_event(
                "create_project_auto_select_starting",
                {
                    "project_name": project_name,
                    "project_id": result["project_id"],
                },
            )

            select_result = await select_project(
                state, {"project_id": result["project_id"]}
            )

            # Log result - success or failure
            if select_result.get("success"):
                logger.info(
                    f"✅ AUTO-SELECT SUCCEEDED for project '{project_name}' "
                    f"(ID: {result['project_id']})"
                )
                state.log_event(
                    "create_project_auto_select_succeeded",
                    {
                        "project_name": project_name,
                        "project_id": result["project_id"],
                    },
                )
            else:
                # Log if selection failed, but don't fail the whole operation
                logger.warning(
                    f"⚠️  AUTO-SELECT FAILED for project '{project_name}' "
                    f"(ID: {result['project_id']}): {select_result.get('error')}"
                )
                state.log_event(
                    "create_project_auto_select_failed",
                    {
                        "project_name": project_name,
                        "project_id": result["project_id"],
                        "error": select_result.get("error"),
                    },
                )
        else:
            # Log why auto-select was skipped
            has_success = result.get("success") if isinstance(result, dict) else False
            has_project_id = (
                "project_id" in result if isinstance(result, dict) else False
            )
            logger.warning(
                f"⚠️  AUTO-SELECT SKIPPED for project '{project_name}': "
                f"has_success={has_success}, has_project_id={has_project_id}"
            )
            state.log_event(
                "create_project_auto_select_skipped",
                {
                    "project_name": project_name,
                    "has_success": (
                        result.get("success") if isinstance(result, dict) else False
                    ),
                    "has_project_id": (
                        "project_id" in result if isinstance(result, dict) else False
                    ),
                    "result_type": type(result).__name__,
                },
            )

        # Phase B registration is no longer wired here — it runs
        # inside the background design closure in
        # ``src/integrations/nlp_tools.py::_run_design_phase`` so it
        # stays in lockstep with Phase A. Between GH-297 (April 2,
        # 2026) and GH-314 (April 6, 2026), Phase B ran here reading
        # ``result["design_content"]`` that Phase A had populated
        # synchronously. GH-314 moved Phase A to a background closure
        # and deleted the line that populated that key, orphaning
        # Phase B silently. GH-320 consolidated both phases into
        # ``_run_design_phase`` so the handoff cannot break again.

        # Write project_info.json when the runner passes project_info_path.
        # Marcus writes the file server-side so the spawner gets
        # recommended_agents without a second MCP HTTP session (which was
        # racy — the session could time out before Marcus was ready).
        if result.get("success") and isinstance(options, dict):
            info_path_str = _resolve_project_info_path(options)
            if info_path_str:
                import json as _json
                from pathlib import Path as _Path

                info_path = _Path(info_path_str)
                board_id = ""
                if hasattr(state, "kanban_client") and state.kanban_client:
                    board_id = str(getattr(state.kanban_client, "board_id", "") or "")
                info_data: Dict[str, Any] = {
                    "project_id": result.get("project_id", ""),
                    "project_name": project_name,
                    "board_id": board_id,
                    "tasks_created": result.get("tasks_created", 0),
                    "recommended_agents": result.get("recommended_agents", 0),
                }
                try:
                    info_path.parent.mkdir(parents=True, exist_ok=True)
                    info_path.write_text(_json.dumps(info_data, indent=2))
                    logger.info(
                        f"[create_project] Wrote project_info.json to {info_path} "
                        f"(recommended_agents={info_data['recommended_agents']})"
                    )
                except Exception as _write_err:
                    logger.warning(
                        f"[create_project] Failed to write project_info.json: "
                        f"{_write_err}"
                    )

        # Record dedup entry AFTER successful creation so failed attempts
        # don't poison the cache and block legitimate retries.
        # Store the result so retries (e.g. from MCP timeouts) get the
        # cached success response and can proceed without re-running decomposition.
        if result.get("success"):
            _recent_create_project_calls[dedup_key] = (time.time(), result)
        else:
            # Non-success result (e.g. internal error response) — clear the
            # in-flight entry so retries are not blocked for 10 minutes.
            _recent_create_project_calls.pop(dedup_key, None)

        return result

    except Exception:
        # Clear in-flight entry on exception so legitimate retries can proceed.
        _recent_create_project_calls.pop(dedup_key, None)
        raise


async def add_feature(
    feature_description: str, integration_point: str, state: Any
) -> Dict[str, Any]:
    """
    Add a feature to existing project using natural language.

    Uses AI to understand feature requirements and:
    - Creates appropriate tasks for implementation
    - Integrates with existing project structure
    - Sets dependencies and priorities
    - Updates project timeline

    Parameters
    ----------
    feature_description : str
        Natural language description of the feature
    integration_point : str
        How to integrate (auto_detect, after_current, parallel, new_phase)
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with created feature tasks and integration details
    """
    # Add feature using natural language processing
    return await add_feature_natural_language(
        feature_description=feature_description,
        integration_point=integration_point,
        state=state,
    )


async def create_tasks(
    task_description: str,
    board_name: Optional[str] = None,
    project_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    state: Any = None,
) -> Dict[str, Any]:
    """
    Create tasks on an existing project and board using natural language.

    This tool is similar to create_project but works on existing projects.
    It can either use an existing board or create a new board within the project.

    Parameters
    ----------
    task_description : str
        Natural language description of the tasks to create
    board_name : Optional[str]
        Board name to use. If not provided, uses the active project's board.
        If the board doesn't exist, it will be created.
    project_name : Optional[str]
        Project name to use. If not provided, uses the active project.
    options : Optional[Dict[str, Any]]
        Optional configuration:
        - complexity: "prototype", "standard" (default), "enterprise"
        - team_size: 1-20 for estimation (default: 1)
        - create_board: bool - Force create new board (default: False)
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        On success:
        {
            "success": True,
            "tasks_created": int,
            "board": {
                "project_id": str,
                "board_id": str,
                "board_name": str
            }
        }

        On error:
        {
            "success": False,
            "error": str
        }

    Examples
    --------
    Create tasks on active project's active board:
        >>> create_tasks(
        ...     task_description="Add user authentication with OAuth2"
        ... )

    Create tasks on specific project and board:
        >>> create_tasks(
        ...     task_description="Implement payment processing",
        ...     project_name="E-commerce",
        ...     board_name="Sprint 2"
        ... )

    Create tasks on new board:
        >>> create_tasks(
        ...     task_description="Add admin dashboard",
        ...     board_name="Admin Features",
        ...     options={"create_board": True}
        ... )
    """
    if not task_description or not task_description.strip():
        return {
            "success": False,
            "error": "Please provide a task description",
            "hint": "Describe the tasks you want to create in natural language",
        }

    options = options or {}

    # create_tasks operates on an existing project — contract_first structural
    # scaffolding is not applicable.  Inject feature_based unless the caller
    # has already set a decomposer or supplied a project_root explicitly.
    if "decomposer" not in options and "project_root" not in options:
        options = {**options, "decomposer": "feature_based"}

    # Get or validate project
    if project_name:
        # Select the specified project
        from .project_management import select_project

        select_result = await select_project(
            state, {"name": project_name, "board_name": board_name}
        )
        if not select_result.get("success"):
            return select_result
    else:
        # Use active project
        active_project = await state.project_registry.get_active_project()
        if not active_project:
            return {
                "success": False,
                "error": "No active project. Please select a project first.",
                "hint": "Use select_project or provide name parameter",
            }

    # Get current project info
    current_project = await state.project_registry.get_active_project()
    if not current_project:
        return {
            "success": False,
            "error": "Failed to get current project",
        }

    # Handle board selection/creation
    kanban_client = await state.project_manager.get_kanban_client()
    if not kanban_client:
        return {
            "success": False,
            "error": "No kanban client available",
        }

    project_id = current_project.provider_config.get("project_id")
    board_id = current_project.provider_config.get("board_id")
    current_board_name = current_project.provider_config.get("board_name")

    # Check if we need to use a different board or create one
    if board_name and board_name != current_board_name:
        # Need to switch to or create the specified board
        if options.get("create_board", False):
            # Create new board
            try:
                new_board = await kanban_client.create_board(
                    project_id=project_id, name=board_name
                )
                board_id = new_board.get("id")
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to create board: {str(e)}",
                }
        else:
            # Try to find existing board by name
            try:
                boards = await kanban_client.list_boards(project_id=project_id)
                matching_board = next(
                    (b for b in boards if b.get("name") == board_name), None
                )
                if matching_board:
                    board_id = matching_board.get("id")
                else:
                    return {
                        "success": False,
                        "error": (
                            f"Board '{board_name}' not found in project "
                            f"'{current_project.name}'"
                        ),
                        "hint": "Set options.create_board=True to create a new board",
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to list boards: {str(e)}",
                }

    if not board_id:
        return {
            "success": False,
            "error": "No board_id available",
        }

    # Use AI to intelligently parse and break down the task description
    try:
        # Use the same natural language project creator that create_project uses
        from src.integrations.nlp_tools import NaturalLanguageProjectCreator

        # Create a temporary project name for AI processing
        temp_project_name = current_project.name or "Task Breakdown"

        # Get subtask_manager if available (GH-62 fix)
        subtask_manager = getattr(state, "subtask_manager", None)

        # Initialize project creator
        creator = NaturalLanguageProjectCreator(
            kanban_client=kanban_client,
            ai_engine=state.ai_engine,
            subtask_manager=subtask_manager,
            state=state,
        )

        # Use the creator to parse tasks from description
        result = await creator.create_project_from_description(
            description=task_description,
            project_name=temp_project_name,
            options=options or {},
        )

        if not result.get("success"):
            return result

        return {
            "success": True,
            "tasks_created": result.get("tasks_created", 0),
            "board": {
                "project_id": project_id,
                "board_id": board_id,
                "board_name": board_name or current_board_name,
            },
            "tasks": result.get("tasks", []),
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to create task: {str(e)}",
        }
