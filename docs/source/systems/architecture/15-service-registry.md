# Service Registry System

## Overview

The Marcus Service Registry is a lightweight, filesystem-based service discovery mechanism that enables multiple clients (like Cato, Claude Desktop, and other integrations) to automatically discover and connect to running Marcus instances without manual configuration.

## What the System Does

The Service Registry provides a decentralized approach to service discovery by:

1. **Service Advertisement**: Each Marcus instance registers itself in a discoverable location when it starts
2. **Automatic Discovery**: Clients can find available Marcus instances without knowing connection details beforehand
3. **Health Monitoring**: Tracks service health and automatically cleans up stale registrations
4. **Connection Information**: Provides MCP command strings and metadata for establishing connections
5. **Multi-Instance Support**: Handles multiple concurrent Marcus instances across different projects

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Marcus        │    │  Service         │    │   Client        │
│   Instance 1    │───▶│  Registry        │◀───│   (Cato)      │
│                 │    │  (~/.marcus/     │    │                 │
└─────────────────┘    │   services/)     │    └─────────────────┘
                       │                  │
┌─────────────────┐    │  ┌─────────────┐ │    ┌─────────────────┐
│   Marcus        │───▶│  │ marcus_1234 │ │    │   Client        │
│   Instance 2    │    │  │ .json       │ │    │   (Claude       │
│                 │    │  └─────────────┘ │    │    Desktop)     │
└─────────────────┘    │                  │    │                 │
                       │  ┌─────────────┐ │    └─────────────────┘
┌─────────────────┐    │  │ marcus_5678 │ │
│   Marcus        │───▶│  │ .json       │ │
│   Instance 3    │    │  └─────────────┘ │
│                 │    │                  │
└─────────────────┘    └──────────────────┘
```

### Core Components

1. **MarcusServiceRegistry Class**: Main registry management
2. **Service Files**: JSON files in `~/.marcus/services/` directory
3. **Global Registry Instance**: Singleton pattern for process-wide access
4. **Convenience Functions**: Simplified API for common operations

## Marcus Ecosystem Integration

The Service Registry serves as the **discovery backbone** for the Marcus ecosystem:

- **Marcus Server**: Registers itself on startup, updates heartbeat periodically
- **Cato**: Discovers available Marcus instances for GUI connections
- **Claude Desktop**: Can auto-connect to running Marcus without manual MCP configuration
- **CLI Tools**: Development tools can find running instances for debugging
- **Monitoring Systems**: External monitoring can discover and health-check Marcus instances

## Workflow Integration

In the typical Marcus workflow, the Service Registry operates **parallel to the main task flow**:

```
create_project → register_agent → request_next_task → report_progress → report_blocker → finish_task
        ↓              ↓                ↓                    ↓               ↓             ↓
   [Service Registration] ────────── [Heartbeat Updates] ──────────────── [Cleanup]
```

### When It's Invoked

1. **Startup Registration**: When Marcus MCP server starts (`src/marcus_mcp/server.py`)
2. **Heartbeat Updates**: Periodic updates during operation (optional)
3. **Shutdown Cleanup**: Automatic cleanup via `atexit` handler
4. **Client Discovery**: When external clients need to find Marcus instances

## What Makes This System Special

### 1. Zero-Configuration Discovery
Unlike traditional service discovery that requires service registries or configuration files, this system works automatically:
- No external dependencies (Redis, Consul, etc.)
- No network configuration required
- Works across different user sessions and environments

### 2. Process-Aware Cleanup
```python
# Automatic stale service detection
if cls._is_process_running(service_info.get("pid")):
    services.append(service_info)
else:
    # Clean up stale service file
    service_file.unlink()
```

### 3. Cross-Platform Compatibility
```python
def _get_registry_dir(self) -> Path:
    if platform.system() == "Windows":
        base_dir = Path(os.environ.get("APPDATA", tempfile.gettempdir()))
    else:
        base_dir = Path.home()

    registry_dir = base_dir / ".marcus" / "services"
```

### 4. Rich Service Metadata
Each service registration includes:
- Connection details (MCP command)
- Project context (current project, provider)
- Runtime information (PID, working directory, Python version)
- Lifecycle timestamps (started_at, last_heartbeat)

## Technical Implementation Details

### Registration Process
```python
def register_service(self, mcp_command: str, log_dir: str, project_name: str = None,
                    provider: str = None, **kwargs) -> Dict[str, Any]:
    service_info = {
        "instance_id": self.instance_id,
        "pid": os.getpid(),
        "mcp_command": mcp_command,  # Key for client connections
        "log_dir": str(Path(log_dir).absolute()),
        "project_name": project_name,
        "provider": provider,
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "last_heartbeat": datetime.now().isoformat(),
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "working_directory": str(Path.cwd()),
        **kwargs,
    }

    # Atomic write to prevent corruption
    with open(self.registry_file, "w") as f:
        json.dump(service_info, f, indent=2)
```

### Discovery Algorithm
```python
@classmethod
def discover_services(cls) -> List[Dict[str, Any]]:
    services = []

    # Scan all service files
    for service_file in registry.registry_dir.glob("marcus_*.json"):
        try:
            with open(service_file, "r") as f:
                service_info = json.load(f)

            # Health check via process existence
            if cls._is_process_running(service_info.get("pid")):
                services.append(service_info)
            else:
                try:
                    service_file.unlink()  # Cleanup stale entries
                except (OSError, PermissionError):
                    pass

        except (json.JSONDecodeError, FileNotFoundError):
            try:
                service_file.unlink()  # Cleanup corrupted files
            except (OSError, PermissionError):
                pass

    return sorted(services, key=lambda x: x.get("started_at", ""))
```

### Instance Identification
```python
def __init__(self, instance_id: str = None):
    # Uses PID for uniqueness across restarts
    self.instance_id = instance_id or f"marcus_{os.getpid()}"
    self.registry_file = self.registry_dir / f"{self.instance_id}.json"
```

## Pros and Cons

### Advantages

1. **Simplicity**: No external dependencies or complex setup
2. **Reliability**: File system operations are atomic and reliable
3. **Performance**: Fast discovery via filesystem globbing
4. **Debugging**: Human-readable JSON files for troubleshooting
5. **Security**: Uses user's home directory with standard file permissions
6. **Multi-Platform**: Works consistently across Windows, macOS, Linux

### Disadvantages

1. **Local Only**: Cannot discover services across network boundaries
2. **File System Dependency**: Requires writable filesystem access
3. **Cleanup Timing**: Stale entries persist until next discovery operation
4. **Concurrency**: No locking mechanism for concurrent registration/discovery
5. **Scale Limitations**: Not designed for high-frequency operations or many services

## Why This Approach Was Chosen

### Design Rationale

1. **Developer Experience**: Eliminates manual MCP server configuration for common use cases
2. **Zero Dependencies**: Avoids external service registry dependencies that would complicate deployment
3. **Debugging Friendly**: Service files can be inspected directly for troubleshooting
4. **Graceful Degradation**: System continues working even if some service files are corrupted

### Alternative Approaches Considered

- **Network-based discovery** (mDNS/Bonjour): Too complex for local development use case
- **Database registry**: Overkill and would require database setup
- **Configuration files**: Would require manual management and updates
- **Environment variables**: Not dynamic enough for multiple instances

## Evolution and Future Directions

### Planned Enhancements

1. **Network Discovery**: Support for remote Marcus instances via optional network protocols
2. **Service Metadata**: Enhanced metadata for capability-based discovery
3. **Health Monitoring**: More sophisticated health checks beyond process existence
4. **Load Balancing**: Client-side load balancing for multiple available instances

### Potential Improvements

```python
# Future: Enhanced service metadata
service_info = {
    # Current fields...
    "capabilities": ["project_management", "ai_analysis", "kanban_integration"],
    "load_metrics": {"active_agents": 3, "cpu_usage": 15.2, "memory_mb": 128},
    "api_version": "2.1.0",
    "supported_providers": ["github", "jira", "trello"],
}

# Future: Service selection by capability
def find_service_with_capability(capability: str) -> Optional[Dict[str, Any]]:
    services = discover_services()
    return next((s for s in services if capability in s.get("capabilities", [])), None)
```

### Integration with Service Mesh

As Marcus scales, the Service Registry could evolve to integrate with service mesh technologies:
- **Service discovery integration** with Consul, etcd
- **Health check endpoints** for external monitoring
- **Metrics exposure** for observability platforms
- **Circuit breaker integration** for resilience

## Task Complexity Handling

The Service Registry operates **independently of task complexity**:

### Simple Tasks
- Registration occurs once at startup regardless of task complexity
- Same discovery mechanism for all clients
- No task-specific metadata in service registration

### Complex Tasks
- Service registration includes project context that may be relevant for complex, multi-project scenarios
- Heartbeat updates could include progress information for long-running operations
- Multiple Marcus instances can handle different complexity levels simultaneously

## Board-Specific Considerations

### Provider Integration
```python
# Service registration includes provider information.
# Note: register_marcus_service() is a module-level convenience wrapper with
# signature register_marcus_service(**kwargs: Any) -> Dict[str, Any].
# The named parameters (mcp_command, log_dir, project_name, provider) are
# forwarded to MarcusServiceRegistry.register_service(), not accepted by
# the wrapper's own signature.
register_marcus_service(
    mcp_command=command,
    log_dir=log_directory,
    project_name="my_project",
    provider="github",  # or "jira", "trello", etc.
)
```

### Multi-Board Support
- Each Marcus instance can register with different provider information
- Clients can discover instances by provider type
- Supports scenarios where different boards require different Marcus configurations

### Board-Aware Discovery
```python
# Future: Board-specific service discovery
def discover_services_by_provider(provider: str) -> List[Dict[str, Any]]:
    all_services = discover_services()
    return [s for s in all_services if s.get("provider") == provider]
```

## Cato Integration

The Service Registry is **crucial for Cato's auto-connection capability**:

### Discovery Flow
1. A discovery client calls `MarcusServiceRegistry.discover_services()`
2. Gets list of available Marcus instances with connection details
3. Uses `mcp_command` from service info to establish MCP connection
4. Can present user with choice of multiple available instances

### Connection Establishment
```python
# Cato discovers Marcus instances
services = MarcusServiceRegistry.discover_services()
preferred = MarcusServiceRegistry.get_preferred_service()

if preferred:
    mcp_command = preferred["mcp_command"]
    # Use mcp_command to establish connection
    client = MCPClient(command=mcp_command.split())
```

### GUI Integration
- Service metadata provides rich information for Cato's GUI
- Project names, providers, and status information for user selection
- Log directory paths for integrated log viewing

## Monitoring and Observability

### Health Monitoring
```python
def _is_process_running(pid: int) -> bool:
    """Check if a process is running by PID"""
    if not pid:
        return False

    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False
```

### Service Lifecycle Tracking
- `started_at`: Service startup timestamp
- `last_heartbeat`: Most recent activity indicator
- `status`: Current service state
- Automatic cleanup of dead services

### Debugging Support
- Human-readable JSON service files
- Rich metadata for troubleshooting connection issues
- Log directory references for detailed investigation

## Error Handling and Resilience

### Graceful Degradation
- Continues operation if some service files are corrupted
- Automatic cleanup of invalid registrations
- No cascading failures from registry issues

### Recovery Mechanisms
```python
try:
    with open(service_file, "r") as f:
        service_info = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    # Clean up invalid service files
    try:
        service_file.unlink()
    except (OSError, PermissionError):
        pass  # Fail silently for cleanup operations
```

## Security Considerations

### File System Security
- Uses user's home directory with standard file permissions
- No network exposure reduces attack surface
- JSON format prevents code injection through service files

### Process Isolation
- PID-based health checking ensures process ownership
- Each service registration is isolated to its own file
- No shared state between different Marcus instances

## Performance Characteristics

### Discovery Performance
- O(n) file system scan where n = number of registered services
- Typically very fast for expected number of services (< 10)
- Caching possible at client level for high-frequency discovery

### Registration Performance
- Single file write operation (atomic)
- No network round-trips required
- Minimal overhead during Marcus startup

### Resource Usage
- Minimal memory footprint (small JSON files)
- No persistent connections or background processes
- Clean automatic cleanup prevents resource leaks

## Integration Testing

The Service Registry system should be tested with:

1. **Multi-instance scenarios**: Multiple Marcus instances registering simultaneously
2. **Crash recovery**: Service cleanup after ungraceful shutdown
3. **Client discovery**: Various clients finding and connecting to services
4. **Cross-platform**: Registry behavior on Windows, macOS, Linux
5. **File corruption**: Recovery from corrupted service files
6. **Permission issues**: Handling of read-only filesystems or permission errors

This service registry system provides the foundational infrastructure that makes Marcus's multi-client ecosystem possible, enabling seamless service discovery while maintaining simplicity and reliability.
