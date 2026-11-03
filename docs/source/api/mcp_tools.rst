MCP Tools Reference
===================

Marcus implements MCP (Model Context Protocol) server tools that AI agents
and human operators use to coordinate and execute tasks.

Tools are role-restricted: **agent tools** are available to coding agents;
**human tools** are available to human operators and Cato. Both sets are
available to authenticated clients with the ``human`` role.

Agent Tools
-----------

These tools are available to all registered AI agents.

Agent Management
~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.agent

.. autofunction:: register_agent

.. autofunction:: get_agent_status


Task Management
~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.task

.. autofunction:: request_next_task

.. autofunction:: report_task_progress

.. autofunction:: report_blocker

.. autofunction:: get_task_context


Context & Collaboration
~~~~~~~~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.context

.. autofunction:: log_decision

.. autofunction:: give_opinion

.. autofunction:: log_artifact


Project Creation
~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.project

.. autofunction:: create_project


Scheduling & Planning
~~~~~~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.scheduling

.. autofunction:: get_optimal_agent_count


System Health
~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.system

.. autofunction:: ping


Project Monitoring
~~~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.project

.. autofunction:: get_project_status


Human Operator Tools
--------------------

These tools are available to human operators (Cato, CLI) but **not** to coding agents.

Agent Administration
~~~~~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.agent

.. autofunction:: list_registered_agents

.. currentmodule:: src.marcus_mcp.tools.system

.. autofunction:: check_assignment_health


Board Health
~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.board_health

.. autofunction:: check_board_health

.. autofunction:: check_task_dependencies


Project Management
~~~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.project_management

.. autofunction:: list_projects

.. autofunction:: switch_project

.. autofunction:: get_current_project

.. autofunction:: add_project

.. autofunction:: remove_project

.. autofunction:: update_project


Natural Language Features
~~~~~~~~~~~~~~~~~~~~~~~~~

.. currentmodule:: src.marcus_mcp.tools.project

.. autofunction:: add_feature


Prediction Tools
----------------

AI-powered predictions for project and task outcomes. Available to human operators.

.. currentmodule:: src.marcus_mcp.tools.predictions

.. autofunction:: predict_completion_time

.. autofunction:: predict_task_outcome

.. autofunction:: predict_blockage_probability

.. autofunction:: predict_cascade_effects

.. autofunction:: get_task_assignment_score


Analytics Tools
---------------

Metrics and analytics for agents, tasks, and projects. Available to human operators.

.. currentmodule:: src.marcus_mcp.tools.analytics

.. autofunction:: get_system_metrics

.. autofunction:: get_agent_metrics

.. autofunction:: get_project_metrics

.. autofunction:: get_task_metrics


Code Metrics Tools
------------------

Code production and quality metrics (GitHub provider). Available to human operators.

.. currentmodule:: src.marcus_mcp.tools.code_metrics

.. autofunction:: get_code_metrics

.. autofunction:: get_repository_metrics

.. autofunction:: get_code_review_metrics

.. autofunction:: get_code_quality_metrics


Auth & Audit Tools
------------------

Authentication and usage auditing. Available to admin clients.

.. currentmodule:: src.marcus_mcp.tools.auth

.. autofunction:: authenticate

.. currentmodule:: src.marcus_mcp.tools.audit_tools

.. autofunction:: get_usage_report
