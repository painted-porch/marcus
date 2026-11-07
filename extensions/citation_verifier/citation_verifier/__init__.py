"""
The AI-citation verifier: Marcus's first extension cut (ADR-0013 K11).

Seeds a ``find -> verify -> approve -> synthesize`` chain onto a SQLite board
(``runner``), runs the finder and verifier as pull-loop MCP clients
(``finder``, ``verifier``, sharing ``worker_common``), decides verdicts
deterministically (``checks``, ``fetch``), exports and redacts audit bundles,
and carries the experiment's measurement tooling (``grader``,
``condition_a``, ``condition_c``, ``rot_sweep``, ``engines``).

Every import of Marcus's own code goes through ``citation_verifier.kernel``.
"""
