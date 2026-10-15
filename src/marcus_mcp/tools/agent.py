"""Agent Management Tools for Marcus MCP.

This module contains tools for managing AI agents in the Marcus system:
- register_agent: Register a new agent with skills and role
- get_agent_status: Get current status and tasks for an agent
- list_registered_agents: List all registered agents
"""

from typing import Any, Dict, List

from src.core.models import WorkerStatus
from src.logging.agent_events import log_agent_event
from src.logging.conversation_logger import conversation_logger, log_thinking

VALID_PRINCIPALS = ("agent", "human")


def _record_identity_change(
    state: Any, agent_id: str, vendor: str, principal: str
) -> None:
    """Record a re-registration that changes vendor or principal.

    Eligibility (#737) reads the worker's self-declared identity at
    offer time, so swapping vendor or principal between registrations
    is the one way to dodge the verify and approve rules. Each such
    change is logged and appended to ``state.identity_changes`` for
    the audit bundle; an identical re-registration records nothing.

    Parameters
    ----------
    state : Any
        Marcus server state instance.
    agent_id : str
        The re-registering worker's id.
    vendor : str
        The newly declared vendor.
    principal : str
        The newly declared principal.
    """
    from datetime import datetime, timezone

    previous = state.agent_status.get(agent_id)
    if previous is None:
        return
    old_vendor = getattr(previous, "vendor", "")
    old_principal = getattr(previous, "principal", "agent")
    if old_vendor == vendor and old_principal == principal:
        return

    log_thinking(
        "marcus",
        f"Identity change at re-registration for {agent_id}: "
        f"vendor {old_vendor!r} -> {vendor!r}, "
        f"principal {old_principal!r} -> {principal!r}",
        {
            "agent_id": agent_id,
            "old_vendor": old_vendor,
            "new_vendor": vendor,
            "old_principal": old_principal,
            "new_principal": principal,
        },
    )
    changes = getattr(state, "identity_changes", None)
    if not isinstance(changes, list):
        changes = []
        try:
            state.identity_changes = changes
        except AttributeError:
            return
    changes.append(
        {
            "agent_id": agent_id,
            "old_vendor": old_vendor,
            "new_vendor": vendor,
            "old_principal": old_principal,
            "new_principal": principal,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


async def register_agent(
    agent_id: str,
    name: str,
    role: str,
    skills: List[str],
    state: Any,
    project_id: str = "",
    vendor: str = "",
    principal: str = "agent",
) -> Dict[str, Any]:
    """
    Register a new agent with the Marcus system.

    Parameters
    ----------
    agent_id : str
        Unique identifier for the agent
    name : str
        Display name for the agent
    role : str
        Agent's role (e.g., 'Backend Developer')
    skills : List[str]
        List of agent's technical skills
    state : Any
        Marcus server state instance
    project_id : str
        ID of the project this agent is working on.  Used to scope
        request_next_task results so agents from concurrent experiments
        cannot steal tasks across project boundaries (GH-388).
    vendor : str, default=""
        Which vendor's model the worker runs on (e.g. "perplexity",
        "google", "openai").  Issue #737: eligibility refuses to offer
        a verify task to the claim author's vendor.  Self-declared;
        the identity layer beneath the record asserts it in production.
    principal : str, default="agent"
        Who the worker is: "agent" or "human".  Issue #737: an approve
        task is offered only to a human principal.  Any other value is
        rejected before any state mutation.

    Returns
    -------
    Dict[str, Any]
        Dict with success status, registration details, and project_id.
    """
    # Log incoming registration request
    conversation_logger.log_worker_message(
        agent_id,
        "to_pm",
        f"Registering as {role} with skills: {skills}",
        {"name": name, "role": role, "skills": skills, "project_id": project_id},
    )

    try:
        # Log Marcus thinking
        log_thinking(
            "marcus",
            f"New agent registration request from {name}",
            {"agent_id": agent_id, "role": role, "skills": skills},
        )

        # Create worker status with correct field names
        # Validate project_id before any state mutation (P2: keeps
        # registration atomic — no ghost entries on rejected calls).
        # Agents are ephemeral — one project, then terminated (GH-389).
        if not project_id:
            return {
                "success": False,
                "error": (
                    "project_id is required. Agents are ephemeral and must "
                    "register with the project they are working on."
                ),
            }

        # Validate principal before any state mutation, same atomicity
        # rule as project_id above (issue #737).
        if principal not in VALID_PRINCIPALS:
            return {
                "success": False,
                "error": (
                    f"Unknown principal '{principal}'. Valid values: "
                    f"{', '.join(VALID_PRINCIPALS)}."
                ),
            }

        # Identity is self-declared, so a re-registration that changes
        # vendor or principal is the one move that dodges eligibility
        # (#737). The pilot accepts that limit, but the change must
        # land on the record so the audit bundle can show it.
        _record_identity_change(state, agent_id, vendor, principal)

        status = WorkerStatus(
            worker_id=agent_id,
            name=name,
            role=role,
            email=None,
            current_tasks=[],
            completed_tasks_count=0,
            capacity=40,  # Default 40 hours/week
            skills=skills or [],
            availability={
                "monday": True,
                "tuesday": True,
                "wednesday": True,
                "thursday": True,
                "friday": True,
                "saturday": False,
                "sunday": False,
            },
            performance_score=1.0,
            vendor=vendor,
            principal=principal,
        )

        state.agent_status[agent_id] = status

        # Store project scope (GH-388: prevents cross-project task theft).
        if not hasattr(state, "agent_project_map"):
            state.agent_project_map = {}
        state.agent_project_map[agent_id] = project_id

        # Log registration event immediately
        state.log_event(
            "worker_registration",
            {
                "worker_id": agent_id,
                "name": name,
                "role": role,
                "skills": skills,
                "vendor": vendor,
                "principal": principal,
                "source": "mcp_client",
                "target": "marcus",
            },
        )

        # Record in active experiment if one is running
        from src.experiments.live_experiment_monitor import get_active_monitor

        monitor = get_active_monitor()
        if monitor and monitor.is_running:
            monitor.record_agent_registration(
                agent_id=agent_id, name=name, role=role, skills=skills
            )

        # Log conversation event for visualization
        log_agent_event(
            "worker_registration",
            {"worker_id": agent_id, "name": name, "role": role, "skills": skills},
        )

        # Log decision
        conversation_logger.log_pm_decision(
            decision=f"Register agent {name}",
            rationale="Agent skills match project requirements",
            confidence_score=0.95,
            decision_factors={
                "skills_match": True,
                "capacity_available": True,
                "role_needed": True,
            },
        )

        # Log response
        conversation_logger.log_worker_message(
            agent_id,
            "from_pm",
            f"Registration successful. Welcome {name}!",
            {"status": "registered"},
        )

        # Emit agent_registered telemetry (Marcus #416, Stage 5A of
        # #9).  Ships role + skills (user-controlled labels per
        # docs/telemetry.md disclosure note) and agent_model.  Does
        # NOT ship agent name or agent_id (those can identify a
        # human).  Helper swallows its own errors.
        try:
            from src.telemetry.events import fire_agent_registered

            fire_agent_registered(role=role, skills=skills or [])
        except Exception:  # noqa: BLE001
            pass

        return {
            "success": True,
            "message": f"Agent {name} registered successfully",
            "agent_id": agent_id,
            "project_id": project_id,
        }

    except Exception as e:
        conversation_logger.log_worker_message(
            agent_id, "from_pm", f"Registration failed: {str(e)}", {"error": str(e)}
        )
        return {"success": False, "error": str(e)}


async def get_agent_status(agent_id: str, state: Any) -> Dict[str, Any]:
    """
    Get status and current assignment for an agent.

    Parameters
    ----------
    agent_id : str
        The agent's unique identifier
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with agent status, current tasks, and assignment details
    """
    try:
        agent = state.agent_status.get(agent_id)
        if agent:
            result = {
                "success": True,
                "agent": {
                    "id": agent.worker_id,
                    "name": agent.name,
                    "role": agent.role,
                    "skills": agent.skills,
                    "status": (
                        "working" if len(agent.current_tasks) > 0 else "available"
                    ),
                    "current_tasks": [t.id for t in agent.current_tasks],
                    "total_completed": agent.completed_tasks_count,
                    "performance_score": agent.performance_score,
                },
            }

            # Add current assignment details if any
            if len(agent.current_tasks) > 0 and agent.worker_id in state.agent_tasks:
                assignment = state.agent_tasks[agent.worker_id]
                result["current_assignment"] = {
                    "task_id": assignment.task_id,
                    "task_name": assignment.task_name,
                    "assigned_at": assignment.assigned_at.isoformat(),
                    "instructions": assignment.instructions,
                }

            return result
        else:
            return {"success": False, "message": f"Agent {agent_id} not found"}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_registered_agents(state: Any) -> Dict[str, Any]:
    """
    List all registered agents and their current status.

    Parameters
    ----------
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with list of all agents and their details
    """
    try:
        agents = []
        for agent in list(state.agent_status.values()):
            agents.append(
                {
                    "id": agent.worker_id,
                    "name": agent.name,
                    "role": agent.role,
                    "status": (
                        "working" if len(agent.current_tasks) > 0 else "available"
                    ),
                    "skills": agent.skills,
                    "current_tasks": [t.id for t in agent.current_tasks],
                    "total_completed": agent.completed_tasks_count,
                }
            )

        return {"success": True, "agents": agents, "total": len(agents)}

    except Exception as e:
        return {"success": False, "error": str(e)}
