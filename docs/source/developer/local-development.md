# Local Development Setup

> **📖 See Also:**
> - [Configuration Reference](configuration.md) — all configuration options
> - [Development Workflow](development-workflow.md) — daily development workflows
> - [Quickstart](../getting-started/quickstart.md) — five-minute install

This guide covers first-time setup for developing **Marcus itself** locally. If you just want to *use* Marcus to run agents on a project, follow the [Quickstart](../getting-started/quickstart.md) instead.

---

## Choose Your Path

Marcus runs locally in all paths. The difference is which kanban backend you use during development.

| Path | When to choose | External dependencies |
|------|----------------|----------------------|
| **A: SQLite** (default) | Default. Recommended for nearly all development. | None. |
| **B: Planka** | When you specifically need to test the drag-and-drop kanban UI or the Planka integration. | Docker (Planka + Postgres + kanban-mcp). |

Path A is what you want unless you're touching the Planka integration code or kanban-mcp itself.

---

## Path A: SQLite (default, zero external dependencies)

### 1. Clone Marcus

```bash
cd ~/projects   # or wherever you keep code
git clone https://github.com/painted-porch/marcus.git
cd marcus
```

### 2. Install in editable mode

```bash
pip install -e .
pip install -r requirements-dev.txt
```

> Requires Python 3.11+.

### 3. Configure your LLM provider

```bash
cp .env.example .env
cp config_marcus.example.json config_marcus.json
echo "CLAUDE_API_KEY=sk-ant-..." >> .env
```

`config_marcus.example.json` already defaults to SQLite — no kanban edits needed.

### 4. Start Marcus

```bash
./marcus start
./marcus status
./marcus logs --tail 50
```

Marcus runs at `http://localhost:4298/mcp`. SQLite database is created automatically at `./data/kanban.db` on first project creation.

### 5. Iterate

```bash
# Edit Marcus code
./marcus stop
./marcus start
```

That's the whole loop. Tests:

```bash
pytest tests/unit -m unit
```

Inspect the board from the terminal:

```bash
sqlite3 data/kanban.db "SELECT name, status, assigned_to FROM tasks"
```

---

## Path B: Planka (drag-and-drop kanban UI)

Use this path when you're working on the Planka integration itself, the kanban-mcp client, or you want a visual UI during development.

> Docker is **infrastructure only** — runs Planka + Postgres + kanban-mcp. Marcus itself still runs locally via `./marcus start`.

### 1. Clone marcus and kanban-mcp as siblings

```bash
cd ~/projects
git clone https://github.com/painted-porch/marcus.git
git clone https://github.com/lwgray/kanban-mcp.git

# Structure:
# ~/projects/
# ├── marcus/
# └── kanban-mcp/
```

Marcus auto-detects `kanban-mcp` as a sibling. If you can't use sibling directories, set `KANBAN_MCP_PATH`:

```bash
export KANBAN_MCP_PATH="/custom/path/to/kanban-mcp/dist/index.js"
```

### 2. Build kanban-mcp

```bash
cd ~/projects/kanban-mcp
npm install
npm run build
```

> This builds kanban-mcp only. Postgres and Planka run via Docker.

### 3. Install Marcus and configure for Planka

```bash
cd ~/projects/marcus
pip install -e .
pip install -r requirements-dev.txt
cp .env.example .env
cp config_marcus.example.json config_marcus.json
echo "CLAUDE_API_KEY=sk-ant-..." >> .env
```

Edit `config_marcus.json`:

```json
{
  "kanban": {
    "provider": "planka",
    "planka_base_url": "http://localhost:3333",
    "planka_email": "demo@demo.demo",
    "planka_password": "demo"  // pragma: allowlist secret
  }
}
```

### 4. Start Planka in Docker

```bash
cd ~/projects/marcus
docker compose up -d postgres planka
# Wait ~10–15 seconds, then open http://localhost:3333
# Login: demo@demo.demo / demo
# Create at least one list (Backlog / In Progress / Done) before creating projects
```

### 5. Start Marcus locally

```bash
./marcus start
./marcus status
```

### Iterate on kanban-mcp

```bash
cd ~/projects/kanban-mcp
# Edit operations/projects.ts (or wherever)
npm run build

cd ~/projects/marcus
./marcus stop && ./marcus start
```

### Stop everything

```bash
./marcus stop
docker compose down                # stops Planka + Postgres
docker compose down -v             # also wipes Planka data
```

---

## Auto-Detection (technical details)

Marcus detects environment + kanban-mcp path automatically. Useful when something breaks.

### Environment detection

Marcus checks if it's running inside Docker by inspecting:
- `/.dockerenv` file presence
- `/proc/1/cgroup` for `docker` or `containerd`
- Hostname pattern (12-char hex string)

### kanban-mcp path resolution (Planka path only)

Priority order:

1. `KANBAN_MCP_PATH` environment variable (supports `~` expansion)
2. Docker-internal path: `/app/kanban-mcp/dist/index.js`
3. Sibling directory: `../kanban-mcp/dist/index.js` relative to Marcus root

### Planka URL auto-adjustment

The same `config_marcus.json` works inside Docker and locally:

| Config value | Inside Docker | Running locally |
|--------------|---------------|-----------------|
| `http://planka:1337` | `http://planka:1337` | `http://localhost:3333` |
| `http://planka` | unchanged | `http://localhost:3333` |
| `http://localhost:3333` | unchanged | unchanged |
| Custom IP/domain | unchanged | unchanged |

---

## Troubleshooting

### `./marcus start` fails — `Connection refused` or port in use

```bash
./marcus status                       # is Marcus already running?
./marcus stop
./marcus start --port 5000            # use a different port
```

### `Module not found`

```bash
pip install -e .
pip install -r requirements-dev.txt
python --version                      # must be 3.11+
```

### SQLite path errors

Confirm the directory exists and is writable:

```bash
ls -la ./data
mkdir -p ./data && chmod u+w ./data
```

### Planka path: `Could not find kanban-mcp`

```bash
ls ~/projects/                                       # marcus/ and kanban-mcp/ both present?
ls ~/projects/kanban-mcp/dist/index.js               # built?
cd ~/projects/kanban-mcp && npm run build            # if not, build
# Or set explicit path:
export KANBAN_MCP_PATH="/custom/path/kanban-mcp/dist/index.js"
```

### Planka path: `Failed to create any tasks` / `find_target_list failed`

Open `http://localhost:3333` and create at least one list (Backlog / In Progress / Done) before creating projects.

### Configuration issues

See [Configuration Reference](configuration.md) for the full schema.

---

## Best Practices

### 1. Use the CLI

```bash
# Use these
./marcus start
./marcus stop
./marcus status
./marcus logs --tail 50

# Avoid
python -m src.marcus_mcp.server
kill -9 $(ps aux | grep marcus)
```

### 2. Default to SQLite during development

Faster restarts, no Docker overhead, no external dependencies. Switch to Planka only when testing kanban-mcp or Planka integration.

### 3. Keep Planka in Docker (when you do use it)

Stable, persistent data, easy to reset (`docker compose down -v`). Don't try to install Planka natively.

### 4. Run tests before commits

```bash
pytest -m unit               # fast unit tests (always run these)
pytest tests/integration     # if your change touches integration paths
```

### 5. Use sibling directories for Planka path

```
~/projects/
├── marcus/
└── kanban-mcp/
```

No environment variable needed.

---

## Common Scenarios

### Scenario 1 — Quick bug fix in Marcus (SQLite)

```bash
# Edit src/integrations/kanban_factory.py (or wherever)
./marcus stop && ./marcus start
pytest tests/unit -m unit -k kanban_factory
git commit -am "fix: short reason"
```

### Scenario 2 — Touching the Planka integration

```bash
cd ~/projects/marcus
docker compose up -d postgres planka
# Edit code that uses the Planka client
./marcus stop && ./marcus start
# Verify against Planka UI at http://localhost:3333
```

### Scenario 3 — Touching kanban-mcp itself

```bash
cd ~/projects/kanban-mcp
# Edit operations/*.ts
npm run build

cd ~/projects/marcus
./marcus stop && ./marcus start
# Test, commit both repos
```

---

## Summary

```bash
# SQLite (default, recommended)
git clone https://github.com/painted-porch/marcus.git
cd marcus && pip install -e . && pip install -r requirements-dev.txt
cp .env.example .env && cp config_marcus.example.json config_marcus.json
echo "CLAUDE_API_KEY=sk-ant-..." >> .env
./marcus start

# Planka (only if you need the UI or are touching kanban-mcp)
# (clone kanban-mcp as a sibling, npm run build,
#  docker compose up -d postgres planka,
#  edit config_marcus.json to provider=planka,
#  ./marcus start)
```

**Key commands:**

- `./marcus start` — start Marcus
- `./marcus stop` — stop Marcus
- `./marcus status` — is it running?
- `./marcus logs --tail 50` — recent logs
- `./marcus board` — terminal view of the kanban board
- `pytest -m unit` — unit tests
