Marcus AI Documentation
========================

**Board-Mediated Coordination for AI Coding Agents**

Marcus is an open-source orchestration server that lets multiple AI agents collaborate
on software projects through a shared kanban board — never through chat. Any
MCP-compatible agent works: Claude Code, Codex, Gemini CLI, Kimi, AutoGen, LangGraph,
or a custom runtime.

.. grid:: 2
    :gutter: 3

    .. grid-item-card:: 🚀 Quickstart
        :link: getting-started/quickstart
        :link-type: doc

        Install Marcus and run your first project in 5 minutes

    .. grid-item-card:: 💡 Core Concepts
        :link: getting-started/core-concepts
        :link-type: doc

        Agents, tasks, projects, the board pattern

    .. grid-item-card:: 📖 API Reference
        :link: api/index
        :link-type: doc

        Auto-generated API and MCP tool reference

    .. grid-item-card:: 🏗️ Systems Architecture
        :link: systems/README
        :link-type: doc

        Marcus internals — coordination, intelligence, infrastructure


Why Marcus?
-----------

.. tab-set::

    .. tab-item:: Board-Mediated Coordination

        Agents pull from a shared kanban board — no group chats, no message passing.
        A modern take on the classical Blackboard pattern (Hayes-Roth, 1985), applied
        to autonomous LLM agents over MCP. Context is preserved per-task; failures
        recover from board state; throughput scales with the number of agents.

    .. tab-item:: Bring Your Own Agent

        Any MCP-compatible runtime works: Claude Code, Codex, Gemini CLI, Kimi,
        AutoGen, LangGraph, or a custom agent. Two operating modes:
        **Runner mode** (one-command via the ``/marcus`` skill in Claude Code) and
        **Attach mode** (any agent connects to ``http://localhost:4298/mcp``).

    .. tab-item:: SQLite by Default

        Zero-setup kanban — Marcus creates ``data/kanban.db`` on first project.
        No Docker, no Postgres, no external services. Optional providers: Planka
        (drag-and-drop UI via Docker), GitHub Projects, Linear.

    .. tab-item:: Built-in Observability

        Every action is on the board: tasks, dependencies, decisions, artifacts,
        progress. Pair with **Cato** for a real-time dashboard, **Posidonius** for
        multi-run experiments, and **Epictetus** for post-run grading.


Quick Example
-------------

The fastest path from idea to working software, using the ``/marcus`` skill in Claude Code:

.. code-block:: bash

    # One-time install
    git clone https://github.com/painted-porch/marcus.git
    cd marcus && pip install -e .
    cp -r skills/marcus ~/.claude/skills/marcus

    # Configure your LLM provider
    cp .env.example .env
    cp config_marcus.example.json config_marcus.json
    # Edit .env: set CLAUDE_API_KEY=sk-ant-...

    # Start Marcus
    ./marcus start

    # Inside Claude Code, in your project directory:
    #   /marcus Build a todo app with authentication using 3 agents
    #
    # The skill registers the MCP server, injects the agent prompt,
    # decomposes the project, and spawns 3 agents in tmux panes.

For other runtimes (Codex, Gemini CLI, AutoGen, custom), use **Attach mode** — connect
your agent to ``http://localhost:4298/mcp`` and follow the work loop in
``prompts/Agent_prompt.md``. See `PROTOCOL.md
<https://github.com/painted-porch/marcus/blob/main/PROTOCOL.md>`_ for the full agent protocol spec.


.. toctree::
   :hidden:
   :maxdepth: 2
   :caption: Getting Started

   getting-started/introduction
   getting-started/quickstart
   getting-started/core-concepts
   getting-started/setup-local-llm

.. toctree::
   :hidden:
   :maxdepth: 2
   :caption: Guides

   guides/index
   guides/agent-workflows/agent-workflow
   guides/agent-workflows/registration
   guides/agent-workflows/requesting-tasks
   guides/agent-workflows/reporting-progress
   guides/agent-workflows/checking-dependencies
   guides/agent-workflows/getting-context
   guides/agent-workflows/handling-blockers
   guides/project-management/creating-projects
   guides/project-management/monitoring-status
   guides/project-management/analyzing-health
   guides/project-management/hierarchical-task-decomposition
   guides/project-management/sqlite-kanban-provider
   guides/collaboration/logging-decisions
   guides/collaboration/tracking-artifacts
   guides/advanced/memory-system
   guides/advanced/agent-status
   guides/advanced/agent-support-tools
   guides/advanced/ping-system

.. toctree::
   :hidden:
   :maxdepth: 1
   :caption: Developer Guide

   developer/contributing
   developer/local-development
   developer/development-workflow
   developer/configuration

.. toctree::
   :hidden:
   :maxdepth: 1
   :caption: API Reference

   api/index
   api/mcp_tools
   api/python_api
   api/data_models
   api/error_handling

.. toctree::
   :hidden:
   :maxdepth: 2
   :caption: Systems

   systems/README
   systems/intelligence/index
   systems/coordination/index
   systems/project-management/index
   systems/architecture/index
   systems/infrastructure/index
   systems/quality/index

.. toctree::
   :hidden:
   :maxdepth: 1
   :caption: Concepts & Roadmap

   concepts/README
   concepts/philosophy
   concepts/core-values
   concepts/contract-first-decomposition
   roadmap/index
   roadmap/evolution
   roadmap/public-release-roadmap
