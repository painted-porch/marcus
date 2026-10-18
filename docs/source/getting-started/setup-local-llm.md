# Setting Up Local LLMs with Marcus

Run Marcus end-to-end on your own hardware — no API keys, no usage costs. This guide covers picking models, configuring Ollama, and running enough capacity to keep multiple agents busy in parallel.

## What you need to set up

Marcus is multi-agent. Two distinct LLM roles must both work:

| Role | What it does | Hard requirement |
|------|--------------|------------------|
| **Planner** | Decomposes a project description into a task graph on the board. Marcus calls it once at `create_project` time. | Strong instruction-following + structured-output reliability |
| **Workers** | Actual coding agents (Claude Code, Codex, Aider, custom). Each pulls tasks from the board and writes code. | **Must support tool / function calling** — Marcus and MCP both depend on it |

> ⚠️ **Worker models without tool-calling will silently fail.** They can't invoke `request_next_task`, `report_task_progress`, `log_artifact`, etc. If you pick a worker model, verify it advertises tool-calling support on its model card.

## Recommended models

### 🏆 Top pick for Apple Silicon — one model, both roles

**`qwen3.5:35b-a3b-coding-nvfp4`** runs comfortably on a 16GB+ M-series Mac and serves as **both planner and worker**. NVFP4 quantization is tuned for Apple Silicon — strong code generation, reliable structured output, and tool-calling support. If you're on a Mac, start here and skip the rest of the matrix.

```bash
ollama pull qwen3.5:35b-a3b-coding-nvfp4
```

Capacity on 16GB unified memory: 1 planner + ~2 workers concurrently.

### Planner — verified working

| Model | Quantization | Notes |
|-------|--------------|-------|
| `qwen3.5:35b-a3b-coding-nvfp4` | NVFP4 | **Best on Apple Silicon.** Doubles as worker. |
| `qwen2.5-coder:7b` | **Q4 or Q5** | **Lowest known-working planner.** Reliable on modest hardware. |
| `ministral:14b` (Ministral-3-14B) | Q4+ | Larger planner option — better task decomposition on complex projects. |
| `qwen2.5-coder:14b` | Q4+ | Higher-quality plans when you have RAM to spare. |

Anything below 7B has not produced reliable plans in our testing.

### Workers — must support tool calling

| Model | Notes |
|-------|-------|
| `qwen3.5:35b-a3b-coding-nvfp4` | **Best on Apple Silicon.** Same model can serve the planner. |
| `qwen2.5-coder:7b` / `:14b` / `:32b` | Tool-calling supported, strong code generation. |
| `deepseek-coder` (instruct variants) | Tool-calling supported. |
| Hosted Claude / GPT via the worker agent itself | The easiest path — let Claude Code or Codex use their normal models. |

If you're unsure whether a model supports tool calling, check the Ollama model page for "Tools" in the capabilities list.

### Running multiple workers in parallel

**One Ollama process serves requests serially per model.** If two workers ask the same `ollama` instance for completions at the same time, the second request waits. To get real parallelism:

- **Option A — multiple Ollama instances.** Launch additional `ollama serve` processes on different ports (`OLLAMA_HOST=127.0.0.1:11435 ollama serve`, then point a worker at `:11435`). One instance per concurrent worker.
- **Option B — `OLLAMA_NUM_PARALLEL`.** Set `export OLLAMA_NUM_PARALLEL=4` before starting Ollama to let a single instance handle multiple requests concurrently. Each parallel slot uses additional VRAM — verify you have headroom.
- **Option C — fewer workers.** If hardware is tight, run 1 planner + 2 workers. Most coordination value shows up before you saturate the box.

Rule of thumb: 16GB unified memory → 1 planner + 2 workers. 32GB+ → 4+ workers comfortably.

## Quick start

### 1. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
# Or download from https://ollama.com/download
```

### 2. Pull a model

```bash
# Apple Silicon — best dual-role pick (planner + workers)
ollama pull qwen3.5:35b-a3b-coding-nvfp4

# Or, the lowest known-working planner for modest hardware
ollama pull qwen2.5-coder:7b

# Or, a larger planner option
ollama pull ministral:14b
```

### 3. Point Marcus at it

Edit `config_marcus.json`:

```json
{
  "ai": {
    "provider": "local",
    "enabled": true,
    "local_model": "qwen3.5:35b-a3b-coding-nvfp4",
    "local_url": "http://localhost:11434/v1",
    "local_key": "none"
  }
}
```

Or override with environment variables (these win over `config_marcus.json`):

```bash
export MARCUS_LLM_PROVIDER=local
export MARCUS_LOCAL_LLM_PATH=qwen3.5:35b-a3b-coding-nvfp4
export MARCUS_LOCAL_LLM_URL=http://localhost:11434/v1
```

### 4. Start Marcus

```bash
./marcus start
./marcus board   # check tasks land on the board
```

### 5. Wire your workers

Each worker is a coding agent — most commonly Claude Code, but any MCP-compatible agent works. Point each worker at its own Ollama endpoint (see "Running multiple workers in parallel" above) and confirm the model supports tool calling.

## Complete configuration example

```json
{
  "auto_find_board": false,
  "kanban": {
    "provider": "sqlite",
    "sqlite_db_path": "./data/kanban.db",
    "sqlite_attachments_dir": "./data/attachments"
  },
  "ai": {
    "provider": "local",
    "enabled": true,
    "local_model": "qwen2.5-coder:7b",
    "local_url": "http://localhost:11434/v1",
    "local_key": "none",
    "anthropic_api_key": "",
    "openai_api_key": ""
  },
  "features": {
    "events": true,
    "context": true,
    "memory": false,
    "visibility": false
  }
}
```

## Advanced

### Non-Ollama OpenAI-compatible servers

Anything that speaks the OpenAI API works (`llama.cpp` server, LocalAI, text-generation-webui, vLLM):

```json
{
  "ai": {
    "provider": "local",
    "local_model": "your-model",
    "local_url": "http://localhost:8080/v1",
    "local_key": "your-api-key-if-needed"
  }
}
```

### Configuration priority

1. Environment variables (`MARCUS_*`)
2. `config_marcus.json`
3. Built-in defaults

### Ollama performance knobs

```bash
export OLLAMA_NUM_CTX=8192        # bigger context window
export OLLAMA_NUM_PARALLEL=4      # concurrent requests per instance
export OLLAMA_KEEP_ALIVE=30m      # keep model resident between calls
```

Local-provider request timeout is 120s by default.

### Switching back to cloud

```bash
export MARCUS_LLM_PROVIDER=anthropic   # or openai
```

Or set `"ai.provider"` in `config_marcus.json`.

## Troubleshooting

**`Failed to connect to local LLM server`**
- `ollama list` — is Ollama actually running?
- `curl http://localhost:11434/api/tags` — does it answer?
- Did you pull the model? `ollama pull <model>`

**Worker silently does nothing / never calls `request_next_task`**
- The model likely lacks tool-calling support. Switch to a model whose card lists Tools as a capability.

**Plans come back malformed / empty**
- Your planner model is too small or too quantized. Try `qwen2.5-coder:7b` at Q5 minimum.

**Second worker stalls when first is busy**
- One Ollama instance, no parallelism. Set `OLLAMA_NUM_PARALLEL` or run a second `ollama serve` on a different port.

**Slow responses**
- Smaller model, GPU acceleration, lower `max_tokens`, or reduce `OLLAMA_NUM_CTX`.

## Why local

- **Privacy** — code never leaves your machine
- **Cost** — zero per-token charges, run as many experiments as you want
- **Offline** — works on a plane
- **Reproducibility** — pin a quantization, get the same outputs

## Next

- Browse [`good first issue`](https://github.com/painted-porch/marcus/labels/good%20first%20issue) and try a contribution end-to-end on local models.
- See [Configuration Reference](../developer/configuration.md) for every option.
- See [PROTOCOL.md](../../../PROTOCOL.md) if you're building a worker runner for a non-Claude agent.
