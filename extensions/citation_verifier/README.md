# marcus-citation-verifier

The AI-citation verifier, packaged as Marcus's first extension (ADR-0013 K11).

## What it does

It seeds a `find -> verify -> approve -> synthesize` chain for a set of article excerpts onto a Marcus SQLite board, runs the finder and the verifier as pull-loop MCP agents, decides verdicts with deterministic checks the finder never sees, and exports one audit bundle per case.
The board enforces the rules: nothing closes without its evidence, no agent verifies its own work, and a human signs off on a sample.
The study it was built for is described in `../../REPRODUCING.md`.

## Install

From the Marcus repository root:

```bash
pip install -e extensions/citation_verifier
```

or, without installing, export `PYTHONPATH=extensions/citation_verifier` and run the modules with `python -m citation_verifier.<module>`.

Console scripts after the install: `tow-seed`, `tow-finder`, `tow-verifier`, `tow-export`, `tow-redact`, `tow-grade`, `tow-condition-a`, `tow-condition-c`, `tow-rot-sweep`.

## Run the free rehearsal

Every moving part runs end to end with a fake engine and zero paid calls; the exact commands are in `../../REPRODUCING.md`, "The free rehearsal".
A scripted version that also records the result lives at `~/marcus-experiments/citation-extension-live-check/run_rehearsal.sh`.

## Where it touches the kernel

Only `citation_verifier/kernel.py` imports Marcus's own code, and a guard test (`tests/unit/extensions/citation_verifier/test_kernel_surface.py`) keeps it that way.
The contract behind that list is `../../docs/architecture/extension-contract.md`.

## Modules

| Module | Purpose |
| --- | --- |
| `runner` | seeds cases, registers the board, writes the manifest and Cato's metadata rows |
| `worker_common` | the shared pull-loop shell both workers use |
| `finder`, `verifier` | the two live workers |
| `checks`, `fetch` | the deterministic verdict and the robots-respecting fetch cache |
| `engines` | vendor API adapters, including the fake engine |
| `export_bundles`, `redact_bundles` | audit bundles out of the board, and dataset text out of the bundles |
| `grader`, `condition_a`, `condition_c`, `rot_sweep` | the experiment's measurement tooling |
