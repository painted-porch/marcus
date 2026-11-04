"""
Marcus Service Registry.

Manages service advertisement and discovery for Marcus instances.
Allows multiple clients (Cato, Claude Desktop, etc.) to discover
and connect to running Marcus instances.
"""

import json
import os
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil


class MarcusServiceRegistry:
    """
    Manages Marcus service advertisement and discovery.

    When Marcus starts, it registers itself in a discoverable location.
    Clients like Cato can find running Marcus instances automatically.
    """

    def __init__(self, instance_id: Optional[str] = None):
        """
        Initialize service registry.

        Parameters
        ----------
        instance_id : str, optional
            Unique identifier for this Marcus instance
        """
        self.instance_id = instance_id or f"marcus_{os.getpid()}"
        self.registry_dir = self._get_registry_dir()
        self.registry_file = self.registry_dir / f"{self.instance_id}.json"

    def _get_registry_dir(self) -> Path:
        """Get the directory for service registry files."""
        if platform.system() == "Windows":
            base_dir = Path(os.environ.get("APPDATA", tempfile.gettempdir()))
        else:
            base_dir = Path.home()

        registry_dir = base_dir / ".marcus" / "services"
        registry_dir.mkdir(parents=True, exist_ok=True)
        return registry_dir

    def register_service(
        self,
        mcp_command: str,
        log_dir: str,
        project_name: Optional[str] = None,
        provider: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Register this Marcus instance as available service.

        Parameters
        ----------
        mcp_command : str
            Command to connect to this Marcus MCP server
        log_dir : str
            Directory where Marcus writes logs
        project_name : str, optional
            Current project name
        provider : str, optional
            Kanban provider being used
        **kwargs
            Additional service metadata

        Returns
        -------
        Dict[str, Any]
            Service registration info
        """
        service_info = {
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "mcp_command": mcp_command,
            "log_dir": str(Path(log_dir).absolute()),
            "project_name": project_name,
            "provider": provider,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "working_directory": str(Path.cwd()),
            **kwargs,
        }

        # Write service file
        with open(self.registry_file, "w") as f:
            json.dump(service_info, f, indent=2)

        return service_info

    def update_heartbeat(self, **updates: Any) -> None:
        """Update service heartbeat and optional fields."""
        if not self.registry_file.exists():
            return

        try:
            with open(self.registry_file, "r") as f:
                service_info = json.load(f)

            service_info["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            service_info.update(updates)

            with open(self.registry_file, "w") as f:
                json.dump(service_info, f, indent=2)

        except (json.JSONDecodeError, FileNotFoundError):
            pass

    def unregister_service(self) -> None:
        """Remove service registration."""
        if self.registry_file.exists():
            self.registry_file.unlink()

    @classmethod
    def discover_services(cls) -> List[Dict[str, Any]]:
        """
        Discover all running Marcus services.

        Returns
        -------
        List[Dict[str, Any]]
            List of available Marcus services
        """
        registry = cls()
        services: List[Dict[str, Any]] = []

        if not registry.registry_dir.exists():
            return services

        # Read all service files
        for service_file in registry.registry_dir.glob("marcus_*.json"):
            try:
                with open(service_file, "r") as f:
                    service_info = json.load(f)

                # Check if process is still running
                if cls._is_process_running(service_info.get("pid")):
                    services.append(service_info)
                else:
                    # Clean up stale service file
                    try:
                        service_file.unlink()
                    except (OSError, PermissionError) as e:
                        # Log specific file system errors but continue discovery
                        import logging

                        logger = logging.getLogger(__name__)
                        logger.debug(
                            f"Could not remove stale service file {service_file}: {e}"
                        )
                    except Exception as e:
                        # Log unexpected errors but continue discovery
                        import logging

                        logger = logging.getLogger(__name__)
                        logger.warning(
                            f"Unexpected error removing stale service file "
                            f"{service_file}: {e}"
                        )

            except (json.JSONDecodeError, FileNotFoundError):
                # Clean up invalid service files
                try:
                    service_file.unlink()
                except (OSError, PermissionError) as e:
                    # Log specific file system errors but continue discovery
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.debug(
                        f"Could not remove invalid service file {service_file}: {e}"
                    )
                except Exception as e:
                    # Log unexpected errors but continue discovery
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(
                        f"Unexpected error removing service file {service_file}: {e}"
                    )

        return sorted(services, key=lambda x: x.get("started_at", ""))

    @classmethod
    def get_preferred_service(cls) -> Optional[Dict[str, Any]]:
        """
        Get the preferred Marcus service to connect to.

        Returns most recently started service, or None if none available.

        Returns
        -------
        Optional[Dict[str, Any]]
            Preferred service info, or None
        """
        services = cls.discover_services()
        return services[-1] if services else None

    @staticmethod
    def _is_process_running(pid: int) -> bool:
        """Check if a process is running by PID."""
        if not pid:
            return False

        try:
            return psutil.pid_exists(pid)  # type: ignore[no-any-return]
        except Exception:
            return False


# Global registry instance
_service_registry = None


def get_service_registry(instance_id: Optional[str] = None) -> MarcusServiceRegistry:
    """Get or create global service registry instance."""
    global _service_registry
    if _service_registry is None:
        _service_registry = MarcusServiceRegistry(instance_id)
    return _service_registry


def register_marcus_service(**kwargs: Any) -> Dict[str, Any]:
    """Register Marcus service."""
    registry = get_service_registry()
    return registry.register_service(**kwargs)


def unregister_marcus_service() -> None:
    """Unregister Marcus service."""
    registry = get_service_registry()
    registry.unregister_service()
