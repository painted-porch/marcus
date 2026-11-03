"""
Event Distribution System for Marcus.

Simple event system that allows components to publish and subscribe to events
without complex dependencies. Events enable loose coupling between systems.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.core.resilience import with_fallback

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Base event structure for all Marcus events."""

    event_id: str
    timestamp: datetime
    event_type: str
    source: str
    data: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary."""
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        return result

    def to_json(self) -> str:
        """Convert event to JSON string."""
        return json.dumps(self.to_dict())


class Events:
    """
    Simple event distribution system for Marcus.

    Features:
    - Publish/subscribe pattern
    - Async event handling
    - Event history (optional)
    - Error isolation (one subscriber error doesn't affect others)
    - Optional persistence to disk
    """

    def __init__(self, store_history: bool = False, persistence: Optional[Any] = None):
        """
        Initialize the event system.

        Parameters
        ----------
            store_history
                Whether to keep event history in memory.
            persistence
                Optional Persistence instance for storing events.
        """
        self.subscribers: Dict[str, List[Callable[..., Any]]] = {}
        self.store_history = store_history
        self.history: List[Event] = []
        self._event_counter = 0
        self.persistence = persistence

    def subscribe(self, event_type: str, handler: Callable[..., Any]) -> None:
        """
        Subscribe to an event type.

        Parameters
        ----------
            event_type
                Type of event to subscribe to ('*' for all events).
            handler
                Async function to call when event occurs.
        """
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(handler)
        logger.debug(f"Handler subscribed to {event_type} events")

    def unsubscribe(self, event_type: str, handler: Callable[..., Any]) -> None:
        """
        Unsubscribe from an event type.

        Parameters
        ----------
            event_type
                Type of event to unsubscribe from.
            handler
                Handler function to remove.
        """
        if event_type in self.subscribers:
            self.subscribers[event_type] = [
                h for h in self.subscribers[event_type] if h != handler
            ]

    async def publish(
        self,
        event_type: str,
        source: str,
        data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        wait_for_handlers: bool = True,
    ) -> Event:
        """
        Publish an event to all subscribers.

        Parameters
        ----------
            event_type
                Type of event (e.g., 'task_assigned', 'progress_updated').
            source
                Source of the event (e.g., 'marcus', 'agent_123').
            data
                Event data.
            metadata
                Optional metadata (confidence scores, etc.).
            wait_for_handlers
                If False, handlers run asynchronously without waiting (default: True).

        Returns
        -------
            The published Event object
        """
        # Create event
        self._event_counter += 1
        now = datetime.now(timezone.utc)
        event = Event(
            event_id=f"evt_{self._event_counter}_{now.timestamp()}",
            timestamp=now,
            event_type=event_type,
            source=source,
            data=data,
            metadata=metadata or {},
        )

        # Store in history if enabled
        if self.store_history:
            self.history.append(event)
            # Keep only last 1000 events to prevent memory issues
            if len(self.history) > 1000:
                self.history = self.history[-1000:]

        # Store to persistence if available (with graceful degradation)
        if self.persistence:
            await self._persist_event_safe(event)

        # Get handlers for this event type
        handlers = []
        if event_type in self.subscribers:
            handlers.extend(self.subscribers[event_type])
        if "*" in self.subscribers:  # Universal subscribers
            handlers.extend(self.subscribers["*"])

        # Call all handlers asynchronously
        if handlers:
            tasks = []
            for handler in handlers:
                # Wrap in try/except to isolate errors
                async def safe_handler(h: Callable[..., Any], e: Event) -> None:
                    try:
                        await h(e)
                    except Exception as err:
                        logger.error(f"Error in event handler {h.__name__}: {err}")

                tasks.append(safe_handler(handler, event))

            # Run all handlers - either wait or fire-and-forget
            if wait_for_handlers:
                # Current behavior: wait for all handlers to complete
                await asyncio.gather(*tasks)
            else:
                # New behavior: fire-and-forget for performance
                for task in tasks:
                    asyncio.create_task(task)

        logger.debug(f"Published {event_type} event from {source}")
        return event

    @with_fallback(
        lambda self, event: logger.warning(f"Event {event.event_id} not persisted")
    )
    async def _persist_event_safe(self, event: Event) -> None:
        """Persist event with graceful degradation."""
        if self.persistence:
            await self.persistence.store_event(event)

    async def get_history(
        self,
        event_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> List[Event]:
        """
        Get event history with optional filtering.

        Parameters
        ----------
            event_type
                Filter by event type.
            source
                Filter by source.
            limit
                Maximum number of events to return.

        Returns
        -------
            List of events matching the criteria
        """
        # Try persistence first if available
        if self.persistence:
            try:
                result = await self.persistence.get_events(
                    event_type=event_type, source=source, limit=limit
                )
                if result is not None:
                    return result  # type: ignore[no-any-return]
            except Exception as e:
                logger.error(f"Failed to get events from persistence: {e}")

        # Fall back to in-memory history
        if not self.store_history:
            return []

        filtered = self.history

        if event_type:
            filtered = [e for e in filtered if e.event_type == event_type]
        if source:
            filtered = [e for e in filtered if e.source == source]

        return filtered[-limit:]

    def clear_history(self) -> None:
        """Clear event history."""
        self.history = []

    async def publish_nowait(
        self,
        event_type: str,
        source: str,
        data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Event:
        """
        Publish an event without waiting for handlers (performance optimization).

        This is useful for non-critical events like logging or monitoring where
        you don't need to wait for handlers to complete.

        Parameters
        ----------
            event_type
                Type of event.
            source
                Source of the event.
            data
                Event data.
            metadata
                Optional metadata.

        Returns
        -------
            The published Event object
        """
        return await self.publish(
            event_type, source, data, metadata, wait_for_handlers=False
        )

    async def wait_for_event(
        self, event_type: str, timeout: Optional[float] = None
    ) -> Optional[Event]:
        """
        Wait for a specific event type to occur.

        Parameters
        ----------
            event_type
                Event type to wait for.
            timeout
                Maximum time to wait (seconds).

        Returns
        -------
            The event if it occurs, None if timeout
        """
        received_event = None
        event_received = asyncio.Event()

        async def capture_handler(event: Event) -> None:
            nonlocal received_event
            received_event = event
            event_received.set()

        # Subscribe temporarily
        self.subscribe(event_type, capture_handler)

        try:
            # Wait for event with timeout
            await asyncio.wait_for(event_received.wait(), timeout=timeout)
            return received_event
        except asyncio.TimeoutError:
            return None
        finally:
            # Clean up subscription
            self.unsubscribe(event_type, capture_handler)


# Common event types for Marcus
class EventTypes:
    """Standard event types used in Marcus."""

    # Task events
    TASK_REQUESTED = "task_requested"
    TASK_ASSIGNED = "task_assigned"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_BLOCKED = "task_blocked"
    BLOCKER_RESOLVED = "blocker_resolved"

    # Agent events
    AGENT_REGISTERED = "agent_registered"
    AGENT_STATUS_CHANGED = "agent_status_changed"
    AGENT_SKILL_UPDATED = "agent_skill_updated"

    # Project events
    PROJECT_CREATED = "project_created"
    PROJECT_UPDATED = "project_updated"
    PROJECT_COMPLETED = "project_completed"

    # System events
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    KANBAN_CONNECTED = "kanban_connected"
    KANBAN_ERROR = "kanban_error"

    # Context events
    CONTEXT_UPDATED = "context_updated"
    DEPENDENCY_DETECTED = "dependency_detected"
    IMPLEMENTATION_FOUND = "implementation_found"

    # Decision events
    DECISION_LOGGED = "decision_logged"
    # An opinion posted to the board with give_opinion (deliberation layer)
    OPINION_LOGGED = "opinion_logged"
    PATTERN_DETECTED = "pattern_detected"

    # Planning events (issue #449)
    # Emitted by NaturalLanguageProjectCreator after each decomposer
    # path returns, when MARCUS_OUTCOME_COVERAGE is on and outcomes
    # were extracted.  Carries intent_fidelity_score plus coverage
    # maps so Cato can surface intent-fidelity telemetry alongside
    # the planning-phase swim lanes.
    PLANNING_INTENT_FIDELITY = "planning_intent_fidelity"

    # Memory events
    PREDICTION_MADE = "prediction_made"
    AGENT_LEARNED = "agent_learned"

    # Error events
    ERROR = "error"
    WARNING = "warning"
