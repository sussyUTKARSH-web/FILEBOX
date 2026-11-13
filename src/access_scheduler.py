"""Per-file reader/writer scheduling with priority aging and FIFO tie breaks."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


EventSink = Callable[[dict], None]


@dataclass(eq=False)
class _Request:
    mode: str
    priority: int
    sequence: int
    user_id: int | None
    file_id: int | None
    queued_at: float = field(default_factory=time.monotonic)


@dataclass
class _FileState:
    condition: threading.Condition = field(default_factory=lambda: threading.Condition(threading.Lock()))
    writer_semaphore: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(1))
    readers: int = 0
    writer: bool = False
    pending: list[_Request] = field(default_factory=list)


class AccessLease:
    def __init__(self, scheduler: "ReaderWriterScheduler", key: str, state: _FileState,
                 request: _Request, waited: float):
        self.scheduler, self.key, self.state, self.request = scheduler, key, state, request
        self.waited = waited
        self._released = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release(success=exc_type is None)

    def release(self, *, success: bool = True):
        if not self._released:
            self._released = True
            self.scheduler._release(self, success)


class ReaderWriterScheduler:
    """Readers share a file; writers are exclusive. Priority ages while waiting.

    Priority levels are 0 (low), 1 (normal), and 2 (high). Every aging interval
    spent waiting raises effective priority by one, up to 2. Requests at equal
    effective priority are ordered by arrival sequence (FIFO). Readers ahead of
    the first queued writer may join the active read group; readers behind that
    writer wait, preventing writer starvation while retaining concurrent reads.
    """

    def __init__(self, *, aging_interval: float = 15.0, event_sink: EventSink | None = None):
        if aging_interval <= 0:
            raise ValueError("aging_interval must be positive")
        self.aging_interval = aging_interval
        self.event_sink = event_sink
        self._states: dict[str, _FileState] = {}
        self._states_lock = threading.Lock()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._logger = logging.getLogger("filebox.scheduler")

    def _state(self, key: str) -> _FileState:
        with self._states_lock:
            return self._states.setdefault(str(key), _FileState())

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            self._sequence += 1
            return self._sequence

    def _effective_priority(self, request: _Request, now: float) -> int:
        age_steps = int((now - request.queued_at) / self.aging_interval)
        return min(2, request.priority + age_steps)

    def _ordered(self, pending: list[_Request], now: float) -> list[_Request]:
        return sorted(pending, key=lambda item: (-self._effective_priority(item, now), item.sequence))

    def acquire(self, key: str | int, mode: str, *, priority: int = 1,
                user_id: int | None = None, file_id: int | None = None) -> AccessLease:
        if mode not in {"read", "write"}:
            raise ValueError("mode must be 'read' or 'write'")
        if priority not in {0, 1, 2}:
            raise ValueError("priority must be 0 (low), 1 (normal), or 2 (high)")
        file_key = str(key)
        state = self._state(file_key)
        request = _Request(mode, priority, self._next_sequence(), user_id, file_id)
        with state.condition:
            state.pending.append(request)
            self._emit("ACCESS_QUEUED", request, wait_ms=0)
            while True:
                now = time.monotonic()
                ordered = self._ordered(state.pending, now)
                can_enter = False
                if mode == "read" and not state.writer:
                    first_writer = next((i for i, waiting in enumerate(ordered) if waiting.mode == "write"), len(ordered))
                    can_enter = request in ordered[:first_writer]
                elif mode == "write" and not state.writer and state.readers == 0:
                    can_enter = bool(ordered) and ordered[0] is request
                if can_enter:
                    state.pending.remove(request)
                    if mode == "read":
                        state.readers += 1
                    else:
                        if not state.writer_semaphore.acquire(blocking=False):
                            raise RuntimeError("Per-file writer semaphore invariant failed")
                        state.writer = True
                    waited = time.monotonic() - request.queued_at
                    self._emit("LOCK_ACQUIRED", request, wait_ms=round(waited * 1000))
                    return AccessLease(self, file_key, state, request, waited)
                # Wake periodically to recalculate aging even if no lock releases.
                state.condition.wait(timeout=min(self.aging_interval, 1.0))

    def _release(self, lease: AccessLease, success: bool):
        state, request = lease.state, lease.request
        with state.condition:
            if request.mode == "read":
                state.readers -= 1
            else:
                state.writer = False
                state.writer_semaphore.release()
            self._emit("LOCK_RELEASED", request, wait_ms=round(lease.waited * 1000),
                       completion="success" if success else "failed")
            state.condition.notify_all()

    def _emit(self, action: str, request: _Request, **extra):
        event = {
            "action": action,
            "user_id": request.user_id,
            "file_id": request.file_id,
            "mode": request.mode,
            "priority": request.priority,
            **extra,
        }
        self._logger.info("%s %s", action, event)
        if self.event_sink:
            try:
                self.event_sink(event)
            except Exception:
                self._logger.exception("Could not enqueue scheduler audit event")

    def snapshot(self, key: str | int) -> dict:
        state = self._state(str(key))
        with state.condition:
            return {"readers": state.readers, "writer": state.writer, "queued": len(state.pending)}
