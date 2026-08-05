"""The one interface every instrument sits behind.

The scheduler never imports a concrete driver. It asks the registry for the
driver bound to a device and talks only through these five methods. A real
Hamilton/Gator driver and the simulator implement the same contract; swapping
one for the other is a registry entry, not a scheduler change.

* `start` returns an opaque handle and nothing else, and it must be safe to
  lose the process the moment it returns.
* `probe` is pull-based on purpose: a restarted scheduler has no callbacks in
  flight and no in-memory futures, only handles it can re-probe.
* `probe` may answer UNKNOWN, which is the honest answer when an instrument
  cannot say whether the operation happened, and the case that has to escalate
  rather than be guessed at.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class JobState(str, enum.Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    #  The instrument has no record of this handle, or cannot determine the
    #  outcome. The scheduler must not assume either way.
    UNKNOWN = "unknown"


class DeviceHealth(str, enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"      # answering, but results are suspect
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class JobSpec:
    step_id: str
    capability: str
    duration_s: int
    sample_id: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobStatus:
    state: JobState
    #  Present when DONE: opaque instrument output, never interpreted here.
    result: dict[str, Any] | None = None
    #  Machine-readable failure classification, one of faults.FaultKind.
    fault_kind: str | None = None
    message: str | None = None
    progress: float = 0.0          # 0..1, for display only
    finish_at: datetime | None = None


@dataclass(frozen=True)
class Heartbeat:
    health: DeviceHealth
    at: datetime | None = None
    message: str | None = None


class TransientDriverError(Exception):
    """Comms blip. Caller should retry with backoff before doing anything drastic."""


class DeviceDriver(ABC):
    """Abstract instrument. One instance may serve many devices of its kind."""

    kind: str = "abstract"

    @abstractmethod
    async def heartbeat(self, device_id: str) -> Heartbeat:
        """Cheap liveness + health probe. Called on a fixed cadence."""

    @abstractmethod
    async def start(self, device_id: str, job: JobSpec) -> str:
        """Begin an operation. Returns an opaque handle. Must be crash-safe:
        once this returns the handle is discoverable via `probe`, even if the
        caller dies before persisting it. See `find_handle_for_step`."""

    @abstractmethod
    async def probe(self, device_id: str, handle: str) -> JobStatus:
        """Current status of a previously started job."""

    @abstractmethod
    async def cancel(self, device_id: str, handle: str) -> None:
        """Best-effort abort. Must be idempotent."""

    @abstractmethod
    async def reset(self, device_id: str) -> None:
        """Clear a fault condition / re-home the instrument. Idempotent."""

    async def find_handle_for_step(self, device_id: str, step_id: str) -> str | None:
        """Recovery hook: if we crashed between `start` returning and writing
        the handle down, ask the instrument whether it is already working on
        this step. Drivers that cannot answer return None, and the scheduler
        falls back to treating the step as UNKNOWN."""
        return None


class DriverRegistry:
    """Maps device id -> driver. In a real deployment this is where a device's
    connection string / serial port / vendor SDK handle would be bound."""

    def __init__(self) -> None:
        self._by_kind: dict[str, DeviceDriver] = {}
        self._by_device: dict[str, DeviceDriver] = {}

    def register_kind(self, kind: str, driver: DeviceDriver) -> None:
        self._by_kind[kind] = driver

    def bind_device(self, device_id: str, driver: DeviceDriver) -> None:
        self._by_device[device_id] = driver

    def for_device(self, device_id: str, kind: str) -> DeviceDriver:
        if device_id in self._by_device:
            return self._by_device[device_id]
        try:
            return self._by_kind[kind]
        except KeyError:
            raise LookupError(f"no driver registered for device {device_id} (kind {kind})") from None
