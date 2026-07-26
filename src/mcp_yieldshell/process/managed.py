"""State container for one managed subprocess and its control tasks."""

from __future__ import annotations

import asyncio

from ..security import SensitiveEnv
from ..types import ProcessInfo
from .ring_buffer import RingBuffer


class ManagedProcess:
    __slots__ = (
        "info",
        "proc",
        "stdout_buf",
        "stderr_buf",
        "drain_stdout",
        "drain_stderr",
        "completion_event",
        "completion_task",
        "timeout_task",
        "group_watch_task",
        "stdin_task",
        "stdin_error",
        "process_group_id",
        "process_group_exited",
        "last_output_at",
        "_seq_source",
        "_timeout_triggered",
        "sensitive_env",
    )

    def __init__(
        self,
        info: ProcessInfo,
        proc: asyncio.subprocess.Process,
        max_buffer_bytes: int,
        process_group_id: int | None = None,
        sensitive_env: SensitiveEnv = (),
    ) -> None:
        self.info = info
        self.proc = proc
        self.process_group_id = process_group_id
        self.process_group_exited = False
        self.last_output_at: float | None = None
        self._seq_source: list[int] = [1]
        self.stdout_buf = RingBuffer(max_buffer_bytes, seq_source=self._seq_source)
        self.stderr_buf = RingBuffer(max_buffer_bytes, seq_source=self._seq_source)
        self.drain_stdout: asyncio.Task[None] | None = None
        self.drain_stderr: asyncio.Task[None] | None = None
        self.completion_event = asyncio.Event()
        self.completion_task: asyncio.Task[None] | None = None
        self.timeout_task: asyncio.Task[None] | None = None
        self.group_watch_task: asyncio.Task[None] | None = None
        self.stdin_task: asyncio.Task[None] | None = None
        self.stdin_error: str | None = None
        self._timeout_triggered = False
        self.sensitive_env = sensitive_env

    @property
    def latest_seq(self) -> int:
        """Position just past the newest byte captured on either stream."""
        return self._seq_source[0]
