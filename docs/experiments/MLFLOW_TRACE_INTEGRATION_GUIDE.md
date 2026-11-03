# MLflow Trace Integration Guide for Marcus

This guide shows how to add MLflow Trace to your Marcus MCP experiments to capture detailed execution traces of agent workflows.

## Quick Start

### 1. Install MLflow (if not already installed)

```bash
pip install mlflow>=2.9.0
```

### 2. Import Tracing Utilities

```python
from mlflow_tracing import trace_agent_task, trace_llm_call, TracedExperiment
```

### 3. Basic Usage

```python
@trace_agent_task("implement_feature")
def implement_feature(task_description):
    # Your agent logic here
    result = do_work(task_description)
    return result
```

---

## Integration Patterns

### Pattern 1: Trace Marcus Agent Workflow

Add tracing to your Marcus agent registration and task execution:

```python
import mlflow
from mlflow_tracing import trace_agent_task, TracedExperiment

# Start traced experiment
with TracedExperiment("my_marcus_experiment", run_name="agent_run_1") as exp:

    # Register agent (traced)
    @trace_agent_task("agent_registration")
    def register_agent():
        from mcp import marcus
        result = marcus.register_agent(
            agent_id="claude-agent-001",
            name="Claude Code Agent",
            role="Full-stack developer",
            skills=["python", "fastapi", "testing"]
        )
        return result

    agent_result = register_agent()
    exp.log_param("agent_id", agent_result.get("agent_id"))

    # Request and execute tasks (traced)
    @trace_agent_task("task_execution")
    def execute_task(agent_id):
        task = marcus.request_next_task(agent_id=agent_id)

        # Your implementation logic here
        result = implement_task(task)

        # Report progress
        marcus.report_task_progress(
            agent_id=agent_id,
            task_id=task["id"],
            status="completed",
            progress=100,
            message="Task completed successfully"
        )

        return result

    task_result = execute_task("claude-agent-001")
    exp.log_metric("tasks_completed", 1)
```

### Pattern 2: Trace LLM Calls

Capture detailed LLM API interactions:

```python
from mlflow_tracing import trace_llm_call

@trace_llm_call(model="claude-sonnet-4-5", operation="code_generation")
async def generate_code_with_claude(prompt: str):
    # Call Claude API
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4.5-20250929",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000
    )

    return {
        "content": response.content[0].text,
        "usage": {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens
        }
    }

# This automatically logs:
# - Prompt text
# - Response content
# - Token usage (prompt_tokens, completion_tokens, total_tokens)
# - Latency
```

### Pattern 3: Trace Tool Calls

Track tool/MCP server interactions:

```python
from mlflow_tracing import trace_tool_call

@trace_tool_call("marcus_mcp", "mcp_server")
def call_marcus_tool(tool_name: str, arguments: dict):
    # Call MCP tool
    result = mcp_client.call_tool(tool_name, arguments)
    return result

# This automatically logs:
# - Tool name
# - Input arguments
# - Output results
# - Execution time
```

### Pattern 4: Nested Spans for Complex Workflows

Create hierarchical traces for multi-step processes:

```python
import mlflow
from mlflow.entities import SpanType

@trace_agent_task("complete_user_story")
def complete_user_story(story: dict):

    # Design phase
    with mlflow.start_span(name="design_phase", span_type=SpanType.CHAIN) as span:
        span.set_attribute("story_id", story["id"])
        design = create_design(story)
        span.set_outputs({"design": design})

    # Implementation phase
    with mlflow.start_span(name="implementation_phase", span_type=SpanType.CHAIN) as span:
        code = implement_design(design)
        span.set_outputs({"files_created": len(code["files"])})

    # Testing phase
    with mlflow.start_span(name="testing_phase", span_type=SpanType.CHAIN) as span:
        test_results = run_tests(code)
        span.set_attribute("tests_passed", test_results["passed"])
        span.set_attribute("coverage", test_results["coverage"])

    return {"status": "completed", "test_results": test_results}
```

---

## Integration with Existing Code

### experiments/run_experiment.py

Add tracing to your experiment runner:

```python
# At the top of the file
from mlflow_tracing import TracedExperiment, trace_agent_task

def run_experiment(config):
    with TracedExperiment(
        experiment_name=config["experiment_name"],
        run_name=config.get("run_name"),
        tags={"config": str(config)}
    ) as exp:

        # Log experiment parameters
        for key, value in config.items():
            exp.log_param(key, value)

        # Your existing experiment logic
        results = execute_experiment_logic(config)

        # Log final metrics
        exp.log_metric("success_rate", results["success_rate"])
        exp.log_metric("total_tasks", results["total_tasks"])

        return results
```

### 1. Start MLflow UI

```bash
mlflow ui --port 5000
```

### 2. Navigate to http://localhost:5000

### 3. View Traces

- Click on your experiment
- Select a run
- Click the "Traces" tab
- Explore the trace tree:
  - See hierarchical execution
  - View timing for each span
  - Inspect inputs/outputs
  - Analyze token usage

### 4. Filter and Search

- Filter by span type (AGENT, LLM, TOOL, CHAIN)
- Search by attributes
- Compare traces across runs
- Export trace data

---

## Advanced Features

### Custom Attributes

Add custom metadata to spans:

```python
with mlflow.start_span(name="my_operation") as span:
    span.set_attribute("user_id", user_id)
    span.set_attribute("environment", "production")
    span.set_attribute("model_version", "v2.1")
    span.set_attribute("custom_metric", 42.5)
```

### Error Tracking

Errors are automatically captured:

```python
@trace_agent_task("risky_operation")
def risky_operation():
    try:
        result = might_fail()
        return result
    except Exception as e:
        # Error is automatically logged to trace
        # Including error type, message, and traceback
        raise
```

### Performance Analysis

Analyze performance across traces:

```python
# Query traces programmatically
from mlflow.tracking import MlflowClient

client = MlflowClient()
traces = client.search_traces(
    experiment_ids=["your_experiment_id"],
    filter_string="attributes.span_type = 'LLM'"
)

# Analyze LLM call latencies
latencies = [trace.duration_ms for trace in traces]
avg_latency = sum(latencies) / len(latencies)
print(f"Average LLM latency: {avg_latency}ms")
```

### Token Usage Tracking

Track cumulative token usage:

```python
from mlflow_tracing import TracedExperiment

with TracedExperiment("token_tracking_experiment") as exp:
    total_tokens = 0

    for task in tasks:
        result = execute_llm_task(task)  # Uses @trace_llm_call
        total_tokens += result["usage"]["total_tokens"]

    # Log total usage
    exp.log_metric("total_tokens", total_tokens)
    exp.log_metric("estimated_cost", total_tokens * 0.00002)  # Example pricing
```

---

## Best Practices

### 1. Use Descriptive Names

```python
# Good
@trace_agent_task("implement_user_authentication")
def implement_auth(spec):
    pass

# Bad
@trace_agent_task("task1")
def do_thing(data):
    pass
```

### 2. Add Meaningful Attributes

```python
with mlflow.start_span(name="api_call") as span:
    span.set_attribute("endpoint", "/api/users")
    span.set_attribute("method", "POST")
    span.set_attribute("status_code", 201)
    span.set_attribute("response_time_ms", 45.2)
```

### 3. Balance Granularity

- Trace important operations (LLM calls, API requests, database queries)
- Avoid tracing trivial operations (variable assignments, simple calculations)
- Group related operations into spans

### 4. Use Hierarchical Spans

```python
with mlflow.start_span(name="complete_feature") as root_span:
    with mlflow.start_span(name="step1") as step1:
        result1 = do_step1()

    with mlflow.start_span(name="step2") as step2:
        result2 = do_step2(result1)

    return combine_results(result1, result2)
```

### 5. Log Both Inputs and Outputs

```python
with mlflow.start_span(name="data_processing") as span:
    span.set_inputs({"raw_data": raw_data})
    processed = process_data(raw_data)
    span.set_outputs({"processed_count": len(processed)})
```

---

## Example: Complete Integration

Here's a complete example integrating tracing into a Marcus agent:

```python
from mlflow_tracing import TracedExperiment, trace_agent_task, trace_llm_call
import mlflow
from mlflow.entities import SpanType

class TracedMarcusAgent:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.experiment_name = f"marcus_agent_{agent_id}"

    def run_autonomous_loop(self):
        with TracedExperiment(
            experiment_name=self.experiment_name,
            run_name=f"session_{int(time.time())}",
            tags={"agent_id": self.agent_id}
        ) as exp:

            # Register
            self._register(exp)

            # Work loop
            while True:
                task = self._request_next_task(exp)
                if not task:
                    break

                result = self._execute_task(task, exp)
                self._report_completion(task, result, exp)

            # Finalize
            exp.set_tag("status", "completed")

    @trace_agent_task("register_agent")
    def _register(self, exp):
        # Registration logic
        exp.log_param("agent_id", self.agent_id)

    @trace_agent_task("request_task")
    def _request_next_task(self, exp):
        # Task request logic
        task = marcus.request_next_task(agent_id=self.agent_id)
        if task:
            exp.log_metric("tasks_received", 1)
        return task

    @trace_agent_task("execute_task")
    def _execute_task(self, task, exp):
        with mlflow.start_span(
            name=f"task_{task['id']}",
            span_type=SpanType.AGENT
        ) as span:
            span.set_attribute("task_name", task["name"])

            # Implementation
            result = self._implement_task(task)

            # Testing
            test_results = self._test_implementation(result)

            span.set_outputs({
                "status": "completed",
                "tests_passed": test_results["passed"]
            })

            return result

    @trace_llm_call(model="claude-sonnet-4-5", operation="implementation")
    def _implement_task(self, task):
        # LLM implementation logic
        pass

    @trace_agent_task("test_implementation")
    def _test_implementation(self, implementation):
        # Testing logic
        pass

    @trace_agent_task("report_completion")
    def _report_completion(self, task, result, exp):
        # Report to Marcus
        exp.log_metric("tasks_completed", 1)
```

---

## Troubleshooting

### Traces Not Appearing

1. Ensure MLflow UI is running: `mlflow ui --port 5000`
2. Check experiment name is correct
3. Verify mlruns directory exists
4. Check for errors in console output

### High Overhead

If tracing slows down experiments:
1. Reduce span granularity
2. Disable tracing for trivial operations
3. Use sampling for high-frequency operations
4. Consider async trace logging

### Missing Attributes

Make sure to set attributes before span ends:

```python
# Good
with mlflow.start_span(name="operation") as span:
    span.set_attribute("important_data", data)
    result = do_work()

# Bad - attribute set after span ends
with mlflow.start_span(name="operation") as span:
    result = do_work()
span.set_attribute("important_data", data)  # Won't work!
```

---

## Resources

- **MLflow Trace Docs**: https://mlflow.org/docs/latest/llms/tracing/index.html
- **View Traces**: http://localhost:5000
- **Example Code**: `mlflow_tracing.py`, `experiments/traced_agent_example.py`
- **Run Example**: `python experiments/traced_agent_example.py`

---

## Next Steps

1. **Run the Example**:
   ```bash
   python experiments/traced_agent_example.py
   ```

2. **View Traces**:
   - Open http://localhost:5000
   - Navigate to "traced_multi_agent_experiment"
   - Click "Traces" tab

3. **Integrate into Your Code**:
   - Add `@trace_agent_task` to agent methods
   - Add `@trace_llm_call` to LLM API calls
   - Wrap experiments with `TracedExperiment`

4. **Customize**:
   - Add custom attributes for your use case
   - Create custom span types
   - Build dashboards from trace data
