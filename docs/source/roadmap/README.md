# Roadmap

> **The canonical roadmap is [ROADMAP.md](https://github.com/painted-porch/marcus/blob/main/ROADMAP.md) at the repo root.** It is the source of truth for current shipping status, the next queue, and architectural debt. The documents in this directory are **historical planning artifacts** — kept for context, not maintained against the current state.

## Current state at a glance

- **Latest shipped release:** v0.3.6 (2026-04-26) — parallel experiment isolation, agent auto-termination, DONE-task board integrity guards.
- **In flight on `develop`:** v0.4.0-dev — parallel experiment platform (per-instance SQLite kanban DBs, env-var MCP URLs, Posidonius batch pipeline tests, Epictetus phase reporting).
- **Next queue:** SQLite migration for all data streams (#414), PostHog telemetry (#416), god-files refactor (#363), per-session project isolation (#442).

For full detail and weekly updates, see the [canonical ROADMAP](https://github.com/painted-porch/marcus/blob/main/ROADMAP.md).

## How Marcus is positioned today

Marcus is a board-mediated multi-agent coordination platform. The orchestration server is MIT-licensed and free forever. Sibling products (Cato dashboard, Posidonius experiment platform, Epictetus grader) are open-source and modular — install only what you need.

The long-term commercial vision (Build Kits, marketplace, federation) is documented in the canonical roadmap but rescoped to research-first milestones for the current phase: NeurIPS 2026 coordination-tax submission, validated experiments, contributor growth.

## Historical planning artifacts (this directory)

- **[Evolution](evolution.md)** — earlier four-phase strategy from project creation → universal engineering assistant. Useful for vision context; superseded for shipping plans by the canonical roadmap.
- **[Public Release Roadmap](public-release-roadmap.md)** — earlier MVP-tool selection and 21-week execution plan. Some items shipped, some pivoted; check against the canonical roadmap before treating any item as planned work.
- **[Future Systems](future-systems.md)** — long-term vision and experimental ideas (voice interface, AR/VR, etc.). Aspirational, not on the active queue.

## Contributing to the roadmap

- Discuss priorities on [Discord](https://discord.com/channels/1409498120739487859/1409498121456848907).
- Open issues with the `enhancement` label on [GitHub](https://github.com/painted-porch/marcus/issues).
- See [CONTRIBUTING.md](https://github.com/painted-porch/marcus/blob/main/CONTRIBUTING.md) for development guidelines.

## Next steps

- **Want the current state?** → [Canonical ROADMAP](https://github.com/painted-porch/marcus/blob/main/ROADMAP.md)
- **Want the long-term vision?** → [Evolution](evolution.md) (historical)
- **Want to ship something?** → check [open issues](https://github.com/painted-porch/marcus/issues) labeled `good first issue` or `pycon_2026`
