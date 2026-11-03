# 💻 Local Development Setup

This guide covers running Marcus locally on your machine for development or testing.

## Prerequisites

- **Python 3.11+** ([Download](https://www.python.org/downloads/))
- **Node.js 18+** (for kanban-mcp integration)
- **Claude Code** or another MCP-compatible client
- **AI Provider**: Choose one:
  - Anthropic API key (recommended)
  - OpenAI API key
  - Local model with Ollama (free)

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/lwgray/marcus.git
cd marcus

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Set up environment variables
cp .env.example .env
# Edit .env with your API keys

# 4. Set up configuration
cp config_marcus.example.json config_marcus.json
# Edit config_marcus.json with your settings

# 5. Start Marcus
./marcus start --http
```

## Detailed Setup

### 1. Environment Variables

Create a `.env` file in the project root:

```bash
# Required: AI Provider API Key
# NOTE: use CLAUDE_API_KEY (not ANTHROPIC_API_KEY) so Marcus's key doesn't
# override Claude Code's subscription auth when both run on the same machine.
CLAUDE_API_KEY=sk-ant-api03-your-key-here
# OR
OPENAI_API_KEY=sk-your-key-here

# Required: Planka Connection (if using Planka backend)
PLANKA_BASE_URL=http://localhost:3333
PLANKA_EMAIL=demo@demo.demo
PLANKA_PASSWORD=demo

# Optional: GitHub Integration
GITHUB_TOKEN=ghp_your-token-here
GITHUB_OWNER=your-username
GITHUB_REPO=your-repo

# Optional: Linear Integration
LINEAR_API_KEY=lin_api_your-key-here
LINEAR_TEAM_ID=your-team-id
```

**Security Note:** Never commit your `.env` file to version control. It's already in `.gitignore`.

### 2. Configuration File

Marcus uses `config_marcus.json` for project and board configuration. Example configs are provided:

```bash
# Use the example that matches your setup
cp config_marcus.json.anthropic config_marcus.json

# Or start from the base example
cp config_marcus.example.json config_marcus.json
```

**Minimal config_marcus.json:**

```json
{
  "ai": {
    "provider": "anthropic",
    "model": "claude-sonnet-4",
    "anthropic_api_key": "${CLAUDE_API_KEY}"
  },
  "kanban": {
    "provider": "planka",
    "board_name": "My Project",
    "planka_base_url": "${PLANKA_BASE_URL}",
    "planka_email": "${PLANKA_EMAIL}",
    "planka_password": "${PLANKA_PASSWORD}"
  }
}
```

**Environment Variable Substitution:**
- Use `${VAR_NAME}` syntax in config to reference `.env` variables
- This keeps secrets out of your config file

### 3. Install Dependencies

**Using pip:**
```bash
pip install -r requirements.txt
```

**Using uv (faster):**
```bash
pip install uv
uv pip install -r requirements.txt
```

### 4. Start Planka (Optional)

If using Planka as your kanban backend, start it with Docker:

```bash
# Start just Planka and its database
docker-compose up -d postgres planka

# Access Planka at http://localhost:3333
# Default login: demo@demo.demo / demo
```

Alternatively, see [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md) for full Docker setup.

### 5. Start Marcus

```bash
# Start with HTTP transport (recommended)
./marcus start --http

# Start on custom port
./marcus start --http --port 5000

# Run in foreground to see logs
./marcus start --http --foreground

# Start with all tools available
./marcus start --http --all-tools
```

**Verify it's running:**
```bash
./marcus status
```

Expected output:
```
✅ Marcus is running
   PID: 12345
   HTTP endpoint: http://localhost:4298/mcp
   Mode: Agent Tools (10 tools)
```

## Connecting from Claude Code

### Configure MCP in Claude

Add Marcus to your Claude MCP settings (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "marcus": {
      "url": "http://localhost:4298/mcp"
    }
  }
}
```

### Test the Connection

In Claude Code:
```
Can you ping the Marcus server?
```

If connected, Claude will have access to Marcus tools like `create_project`, `get_project_status`, etc.

## Using Multi-Endpoint Mode

For advanced setups, you can run Marcus with separate endpoints for humans, agents, and analytics:

```bash
# Start multi-endpoint mode with default ports
./marcus start --multi

# Custom ports
./marcus start --multi \
  --human-port 5001 \
  --agent-port 5002 \
  --analytics-port 5003
```

Endpoints:
- **Human** (http://localhost:4298/mcp) - Full access for human developers
- **Agent** (http://localhost:4299/mcp) - Limited tools for AI agents
- **Analytics** (http://localhost:4300/mcp) - All tools for analytics/monitoring

See [tool_groups.py](src/marcus_mcp/tool_groups.py) for tool assignments per endpoint.

## Development Workflow

### Making Changes

1. **Edit code** in your IDE
2. **Restart Marcus** to apply changes:
   ```bash
   ./marcus restart
   ```
3. **Test** with Claude Code or experiments

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_server.py

# Run with coverage
pytest --cov=src

# Run integration tests (requires Planka)
pytest tests/integration/
```

### Viewing Logs

```bash
# View recent logs
./marcus logs --tail 50

# Follow logs in real-time
./marcus logs --follow

# View all logs
./marcus logs
```

Logs are stored in: `logs/marcus_YYYYMMDD_HHMMSS.log`

### Debugging

**Enable debug logging:**

Set in `config_marcus.json`:
```json
{
  "log_level": "DEBUG"
}
```

Or via environment variable:
```bash
export LOG_LEVEL=DEBUG
./marcus restart
```

**Check Marcus internals:**

```bash
# View current config
./marcus config

# Edit config file
./marcus config --edit
```

## Troubleshooting

### Marcus won't start

**Check if already running:**
```bash
./marcus status
./marcus stop
./marcus start --http
```

**Check for port conflicts:**
```bash
# Find what's using port 4298
lsof -i :4298

# Use a different port
./marcus start --http --port 5000
```

**Check logs for errors:**
```bash
./marcus logs --tail 100
```

### Environment variables not loading

**Verify .env file exists:**
```bash
ls -la .env
cat .env
```

**Check if variables are set:**
```bash
./marcus start --foreground
# Look for "CLAUDE_API_KEY not found" errors
```

**Test manually:**
```bash
python3 -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); print(os.getenv('CLAUDE_API_KEY'))"
```

### Can't connect to Planka

**Check Planka is running:**
```bash
curl http://localhost:3333
```

**Verify credentials in .env:**
```bash
grep PLANKA .env
```

**Test Planka login manually:**
```bash
curl -X POST http://localhost:3333/api/access-tokens \
  -H "Content-Type: application/json" \
  -d '{"emailOrUsername":"demo@demo.demo","password":"demo"}'  # pragma: allowlist secret
```

### Claude Code can't find Marcus

**Check Marcus status:**
```bash
./marcus status
# Should show: ✅ Marcus is running
```

**Test MCP endpoint:**
```bash
curl http://localhost:4298/mcp
# Should return MCP protocol info
```

**Restart Claude Code** after updating MCP config

## Alternative: Using Docker

If you prefer containerized development, see [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md) for running Marcus in Docker.

## Next Steps

- **Create a project:** See [experiments/](experiments/) for example prompts
- **Run experiments:** Use [dev-tools/experiments/runners/run_experiment.py](dev-tools/experiments/runners/run_experiment.py)
- **Explore examples:** Check [experiments/snake_game_single_agent_prompt.md](experiments/snake_game_single_agent_prompt.md)
- **Read architecture:** See [demo/marcus_origin_story.docx](demo/marcus_origin_story.docx)

## Getting Help

- **Discord:** https://discord.gg/marcus
- **GitHub Issues:** https://github.com/lwgray/marcus/issues
- **Documentation:** Check other .md files in this repo
