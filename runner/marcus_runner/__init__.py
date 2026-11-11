"""
The run-side toolkit Marcus extensions share (issue #914).

An extension is a program that seeds a Marcus board and runs agents against
it for one purpose. Every extension so far rebuilt the same scaffolding:
a private server for the run, seeding, guards, a cost watchdog, a board
sampler, measures and a run record. This package is that scaffolding,
lifted from the Minecraft plumbing (``~/marcus-experiments/minecraft-plumbing``)
with the defects its runs found fixed once.

Nothing here imports Marcus's own code under ``src/``: the toolkit talks to
a server over MCP and reads its files, the same way an extension must
(``docs/architecture/extension-contract.md``).
"""

__all__ = [
    "client",
    "pricing",
    "usage",
    "watchdog",
    "sampler",
    "integrity",
    "server",
    "seed",
    "record",
    "measure",
]
