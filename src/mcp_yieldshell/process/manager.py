"""Process registry and lifecycle management."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from typing import Any

from ..config import Config
from ..security import redact_text
from ..types import ProcessInfo, ProcessStatus
from .ring_buffer import RingBuffer
from .spawn import kill_process, spawn_process, terminate_process


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
        "process_group_id",
        "_seq_source",
        "_timeout_triggered",
    )

    def __init__(
        self,
        info: ProcessInfo,
        proc: asyncio.subprocess.Process,
        max_output_bytes: int,
        process_group_id: int | None = None,
    ) -> None:
        self.info = info
        self.proc = proc
        self.process_group_id = process_group_id
        self._seq_source: list[int] = [1]
        self.stdout_buf = RingBuffer(max_output_bytes, seq_source=self._seq_source)
        self.stderr_buf = RingBuffer(max_output_bytes, seq_source=self._seq_source)
        self.drain_stdout: asyncio.Task[None] | None = None
        self.drain_stderr: asyncio.Task[None] | None = None
        self.completion_event: asyncio.Event = asyncio.Event()
        self.completion_task: asyncio.Task[None] | None = None
        self.timeout_task: asyncio.Task[None] | None = None
        self._timeout_triggered = False


class ProcessManager:
    """Registry and lifecycle manager for managed shell processes."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._processes: dict[str, ManagedProcess] = {}

    def _new_id(self) -> str:
        return f"proc_{uuid.uuid4().hex[:12]}"

    def _max_output(self, requested: int | None) -> int:
        cap = self._config.max_output_bytes
        if requested is None or requested <= 0:
            return cap
        return min(requested, cap)

    def _clamp_yield_ms(self, requested: int | None) -> int:
        if requested is None:
            return self._config.default_yield_ms
        return max(0, min(requested, self._config.max_yield_ms))

    def _clamp_timeout_ms(self, requested: int | None) -> int:
        if requested is None:
            return self._config.default_timeout_ms
        return max(0, requested)

    async def exec_command(
        self,
        command: str,
        cwd: str | None = None,
        env_overlay: dict[str, str] | None = None,
        shell: str | None = None,
        stdin: str | None = None,
        name: str | None = None,
        yield_ms: int | None = None,
        timeout_ms: int | None = None,
        max_output_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Execute a shell command with auto-yield behavior."""
        from ..security import build_env, resolve_cwd, validate_command

        # Validate command policy
        cmd_error = validate_command(self._config, command)
        if cmd_error:
            return {"status": "failed_to_start", "error": cmd_error}

        # Resolve and validate cwd
        resolved_cwd, cwd_error = resolve_cwd(self._config, cwd)
        if cwd_error:
            return {"status": "failed_to_start", "error": cwd_error}

        # Check process count limit
        running_count = sum(
            1 for p in self._processes.values() if p.info.status == ProcessStatus.RUNNING
        )
        if running_count >= self._config.max_processes:
            return {
                "status": "failed_to_start",
                "error": f"Maximum process limit ({self._config.max_processes}) reached",
            }

        # Build environment
        env = build_env(self._config, env_overlay)
        effective_yield = self._clamp_yield_ms(yield_ms)
        effective_timeout = self._clamp_timeout_ms(timeout_ms)
        effective_max_output = self._max_output(max_output_bytes)

        # Spawn process
        try:
            proc = await spawn_process(command, cwd=resolved_cwd, env=env)
        except Exception as exc:
            return {"status": "failed_to_start", "error": str(exc)}

        process_group_id = self._get_process_group_id(proc)
        process_id = self._new_id()
        start_time = time.monotonic()
        start_timestamp = time.time()

        info = ProcessInfo(
            process_id=process_id,
            pid=proc.pid,
            command=command,
            cwd=resolved_cwd,
            name=name,
            status=ProcessStatus.RUNNING,
            started_at=start_timestamp,
            start_monotonic=start_time,
        )

        mp = ManagedProcess(info, proc, effective_max_output, process_group_id)

        # Start drain tasks immediately after spawn to prevent blocking on full pipe buffers
        mp.drain_stdout = asyncio.create_task(
            self._drain_stream(proc.stdout, mp.stdout_buf), name=f"drain-stdout-{process_id}"
        )
        mp.drain_stderr = asyncio.create_task(
            self._drain_stream(proc.stderr, mp.stderr_buf), name=f"drain-stderr-{process_id}"
        )

        # Write initial stdin if provided; keep pipe open for follow-up writes
        if stdin is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(stdin.encode("utf-8"))
                    await proc.stdin.drain()
            except Exception:
                pass

        # Start completion tracking
        mp.completion_task = asyncio.create_task(
            self._track_completion(proc, mp), name=f"completion-{process_id}"
        )

        # Register process
        self._processes[process_id] = mp

        # Start timeout task if requested
        if effective_timeout > 0:
            mp.timeout_task = asyncio.create_task(
                self._handle_timeout(mp, effective_timeout / 1000.0),
                name=f"timeout-{process_id}",
            )

        # Wait up to yield_ms for completion
        try:
            await asyncio.wait_for(
                mp.completion_event.wait(), timeout=effective_yield / 1000.0
            )
        except asyncio.TimeoutError:
            pass

        duration_ms = (time.monotonic() - start_time) * 1000

        # Prepare output for response
        stdout_data = mp.stdout_buf.read(max_bytes=effective_max_output)
        stderr_data = mp.stderr_buf.read(max_bytes=effective_max_output)
        truncated = stdout_data["truncated"] or stderr_data["truncated"]
        stdout_text = redact_text(self._config, stdout_data["text"])
        stderr_text = redact_text(self._config, stderr_data["text"])

        if mp.info.status == ProcessStatus.COMPLETED:
            return {
                "status": "completed",
                "exit_code": mp.info.exit_code,
                "signal": mp.info.signal,
                "duration_ms": round(duration_ms, 1),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": truncated,
            }

        if mp.info.status == ProcessStatus.TIMED_OUT:
            return {
                "status": "timed_out",
                "process_id": process_id,
                "exit_code": mp.info.exit_code,
                "signal": mp.info.signal,
                "duration_ms": round(duration_ms, 1),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": truncated,
            }

        if mp.info.status == ProcessStatus.STOPPED:
            return {
                "status": "stopped",
                "process_id": process_id,
                "exit_code": mp.info.exit_code,
                "signal": mp.info.signal,
                "duration_ms": round(duration_ms, 1),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": truncated,
            }

        if mp.info.status == ProcessStatus.FAILED:
            return {
                "status": "failed",
                "process_id": process_id,
                "exit_code": mp.info.exit_code,
                "signal": mp.info.signal,
                "duration_ms": round(duration_ms, 1),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": truncated,
            }

        # Still running — background it
        return {
            "status": "backgrounded",
            "process_id": process_id,
            "pid": mp.info.pid,
            "duration_ms": round(duration_ms, 1),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": truncated,
            "message": "Process is running in the background. Use read/wait/stop with process_id.",
        }

    def _get_process_group_id(self, proc: asyncio.subprocess.Process) -> int | None:
        if sys.platform == "win32" or proc.pid is None:
            return None
        try:
            return os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return None

    def _process_group_id(self, mp: ManagedProcess) -> int:
        if mp.process_group_id is not None:
            return mp.process_group_id
        if mp.proc.pid is None:
            raise ProcessLookupError
        return os.getpgid(mp.proc.pid)

    def _process_group_exists(self, mp: ManagedProcess) -> bool:
        if sys.platform == "win32":
            return mp.proc.returncode is None
        try:
            os.killpg(self._process_group_id(mp), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _drain_stream(
        self, stream: asyncio.StreamReader | None, buf: RingBuffer
    ) -> None:
        """Read from a subprocess stream into a ring buffer."""
        if stream is None:
            return
        while True:
            try:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                buf.append(chunk)
            except Exception:
                break

    async def _track_completion(
        self, proc: asyncio.subprocess.Process, mp: ManagedProcess
    ) -> None:
        """Wait for process to exit and update status."""
        wait_completed = False
        try:
            returncode, wait_completed = await self._wait_for_returncode(proc)
            if wait_completed:
                await self._wait_for_process_group_exit(mp, timeout_sec=2.0)
                await self._drain_with_timeout(mp, timeout_sec=1.0)
            else:
                self._cancel_drains(mp)
            mp.info.exit_code = returncode
            mp.info.signal = self._exit_signal(proc)
            mp.info.ended_at = time.time()
            mp.info.duration_ms = (time.monotonic() - mp.info.start_monotonic) * 1000
            if mp.info.status == ProcessStatus.RUNNING:
                if mp._timeout_triggered:
                    mp.info.status = ProcessStatus.TIMED_OUT
                else:
                    mp.info.status = ProcessStatus.COMPLETED
        except asyncio.CancelledError:
            raise
        except Exception:
            if mp.info.status == ProcessStatus.RUNNING:
                mp.info.status = ProcessStatus.FAILED
                mp.completion_event.set()
        finally:
            if mp.timeout_task is not None and not mp.timeout_task.done():
                if not mp._timeout_triggered:
                    mp.timeout_task.cancel()
            pg_alive = self._process_group_exists(mp)
            if mp._timeout_triggered and pg_alive:
                pass
            elif not wait_completed and pg_alive:
                pass
            else:
                mp.completion_event.set()

    async def _wait_for_process_group_exit(
        self, mp: ManagedProcess, timeout_sec: float
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        while self._process_group_exists(mp) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _wait_for_returncode(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[int, bool]:
        wait_task = asyncio.create_task(proc.wait())
        try:
            while proc.returncode is None:
                try:
                    return (
                        await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.25),
                        True,
                    )
                except asyncio.TimeoutError:
                    pass
            if not wait_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            return proc.returncode, wait_task.done()
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

    async def _drain_with_timeout(self, mp: ManagedProcess, timeout_sec: float) -> None:
        """Drain stdout/stderr with a timeout; cancel tasks if they block."""
        tasks = [
            task
            for task in (mp.drain_stdout, mp.drain_stderr)
            if task is not None and not task.done()
        ]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cancel_drains(self, mp: ManagedProcess) -> None:
        for task in (mp.drain_stdout, mp.drain_stderr):
            if task is not None and not task.done():
                task.cancel()

    def _exit_signal(self, proc: asyncio.subprocess.Process) -> str | None:
        """Determine signal name from process returncode on POSIX."""
        if proc.returncode is None:
            return None
        if sys.platform == "win32":
            return None
        # On POSIX, negative returncode means killed by signal
        rc = proc.returncode
        if rc < 0:
            import signal as sig_module

            sig_num = -rc
            try:
                return sig_module.Signals(sig_num).name
            except (ValueError, KeyError):
                return f"SIG{sig_num}"
        return None

    async def _handle_timeout(self, mp: ManagedProcess, timeout_sec: float) -> None:
        """Handle total runtime timeout: graceful terminate then force kill."""
        try:
            await asyncio.sleep(timeout_sec)
        except asyncio.CancelledError:
            return
        mp._timeout_triggered = True
        if mp.info.status != ProcessStatus.RUNNING:
            return
        # Graceful termination
        await terminate_process(mp.proc, mp.process_group_id)
        grace_period = 3.0
        try:
            await asyncio.wait_for(mp.completion_event.wait(), timeout=grace_period)
        except asyncio.TimeoutError:
            pass

        if self._process_group_exists(mp):
            # Force kill any children that survived graceful termination, even if
            # the shell process already exited.
            await kill_process(mp.proc, mp.process_group_id)
            await self._wait_for_process_group_exit(mp, timeout_sec=2.0)

        if not self._process_group_exists(mp):
            await self._drain_with_timeout(mp, timeout_sec=1.0)

        mp.completion_event.set()
        if mp.info.status == ProcessStatus.COMPLETED:
            mp.info.status = ProcessStatus.TIMED_OUT

    async def read_output(
        self,
        process_id: str,
        since_seq: int | None = None,
        max_output_bytes: int | None = None,
        streams: str = "both",
    ) -> dict[str, Any]:
        """Read output from a managed process."""
        mp = self._processes.get(process_id)
        if mp is None:
            return {"process_id": process_id, "error": f"Unknown process_id: {process_id}"}

        if streams not in ("both", "stdout", "stderr"):
            return {"process_id": process_id, "error": f"Invalid streams: {streams!r}"}

        effective_max = self._max_output(max_output_bytes)

        stdout_text = None
        stderr_text = None
        next_seq = 1
        truncated = False

        if streams in ("both", "stdout"):
            data = mp.stdout_buf.read(since_seq=since_seq, max_bytes=effective_max)
            stdout_text = redact_text(self._config, data["text"])
            next_seq = max(next_seq, data["next_seq"])
            truncated = truncated or data["truncated"]

        if streams in ("both", "stderr"):
            data = mp.stderr_buf.read(since_seq=since_seq, max_bytes=effective_max)
            stderr_text = redact_text(self._config, data["text"])
            next_seq = max(next_seq, data["next_seq"])
            truncated = truncated or data["truncated"]

        result: dict[str, Any] = {
            "process_id": process_id,
            "status": mp.info.status.value,
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
            "next_seq": next_seq,
            "truncated": truncated,
        }
        if stdout_text is not None:
            result["stdout"] = stdout_text
        if stderr_text is not None:
            result["stderr"] = stderr_text
        return result

    async def write_input(
        self, process_id: str, input_data: str, newline: bool = False
    ) -> dict[str, Any]:
        """Write to stdin of a managed process."""
        mp = self._processes.get(process_id)
        if mp is None:
            return {
                "process_id": process_id, "ok": False,
                "error": f"Unknown process_id: {process_id}",
            }

        if mp.info.status != ProcessStatus.RUNNING:
            return {
                "process_id": process_id,
                "ok": False,
                "error": f"Process is not running (status: {mp.info.status.value})",
            }

        if mp.proc.stdin is None or mp.proc.stdin.is_closing():
            return {
                "process_id": process_id,
                "ok": False,
                "error": "Process stdin is closed",
            }

        try:
            data = input_data.encode("utf-8")
            if newline:
                data += b"\n"
            mp.proc.stdin.write(data)
            await mp.proc.stdin.drain()
            return {"process_id": process_id, "ok": True}
        except Exception as exc:
            return {"process_id": process_id, "ok": False, "error": str(exc)}

    async def wait_process(
        self,
        process_id: str,
        timeout_ms: int = 30000,
        max_output_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Wait for a process to exit without killing it."""
        mp = self._processes.get(process_id)
        if mp is None:
            return {"process_id": process_id, "error": f"Unknown process_id: {process_id}"}

        if mp.info.status != ProcessStatus.RUNNING:
            # Already completed
            effective_max = self._max_output(max_output_bytes)
            stdout_data = mp.stdout_buf.read(max_bytes=effective_max)
            stderr_data = mp.stderr_buf.read(max_bytes=effective_max)
            truncated = stdout_data["truncated"] or stderr_data["truncated"]
            return {
                "process_id": process_id,
                "status": mp.info.status.value,
                "exit_code": mp.info.exit_code,
                "signal": mp.info.signal,
                "stdout": redact_text(self._config, stdout_data["text"]),
                "stderr": redact_text(self._config, stderr_data["text"]),
                "next_seq": max(stdout_data["next_seq"], stderr_data["next_seq"]),
                "truncated": truncated,
            }

        # Wait up to timeout
        try:
            await asyncio.wait_for(
                mp.completion_event.wait(), timeout=timeout_ms / 1000.0
            )
        except asyncio.TimeoutError:
            pass

        effective_max = self._max_output(max_output_bytes)
        stdout_data = mp.stdout_buf.read(max_bytes=effective_max)
        stderr_data = mp.stderr_buf.read(max_bytes=effective_max)
        truncated = stdout_data["truncated"] or stderr_data["truncated"]

        return {
            "process_id": process_id,
            "status": mp.info.status.value,
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
            "stdout": redact_text(self._config, stdout_data["text"]),
            "stderr": redact_text(self._config, stderr_data["text"]),
            "next_seq": max(stdout_data["next_seq"], stderr_data["next_seq"]),
            "truncated": truncated,
        }

    async def stop_process(
        self,
        process_id: str,
        signal_name: str = "SIGTERM",
        force_after_ms: int = 3000,
    ) -> dict[str, Any]:
        """Stop a running process with graceful termination then force kill."""
        from .spawn import get_signal

        mp = self._processes.get(process_id)
        if mp is None:
            return {
                "process_id": process_id, "stopped": False,
                "error": f"Unknown process_id: {process_id}",
            }

        if mp.info.status != ProcessStatus.RUNNING:
            return {
                "process_id": process_id,
                "stopped": False,
                "error": f"Process is not running (status: {mp.info.status.value})",
            }

        # Send requested signal
        sig = get_signal(signal_name)
        if sig is not None and sys.platform != "win32" and mp.proc.pid is not None:
            try:
                os.killpg(self._process_group_id(mp), sig)
            except (ProcessLookupError, PermissionError):
                try:
                    mp.proc.send_signal(sig)
                except Exception:
                    pass
        else:
            await terminate_process(mp.proc)

        # Wait for grace period
        try:
            await asyncio.wait_for(
                mp.completion_event.wait(), timeout=force_after_ms / 1000.0
            )
        except asyncio.TimeoutError:
            # Force kill
            await kill_process(mp.proc, mp.process_group_id)
            try:
                await asyncio.wait_for(mp.completion_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        # If the process exited due to our signal, mark it as STOPPED.
        # _track_completion may have set COMPLETED, but since we initiated
        # termination, the correct terminal status is STOPPED.
        if mp.info.status == ProcessStatus.RUNNING:
            # Process didn't exit even after force kill
            mp.info.status = ProcessStatus.STOPPED
            mp.info.ended_at = time.time()
        elif mp.info.status == ProcessStatus.COMPLETED:
            mp.info.status = ProcessStatus.STOPPED

        stopped = mp.info.status == ProcessStatus.STOPPED

        return {
            "process_id": process_id,
            "stopped": stopped,
            "signal": signal_name,
            "error": None,
        }

    def list_processes(
        self, include_completed: bool = True, limit: int = 50
    ) -> dict[str, Any]:
        """List managed processes."""
        processes = []
        for mp in list(self._processes.values()):
            if not include_completed and mp.info.status in (
                ProcessStatus.COMPLETED,
                ProcessStatus.STOPPED,
                ProcessStatus.TIMED_OUT,
                ProcessStatus.FAILED,
            ):
                continue
            processes.append(
                {
                    "process_id": mp.info.process_id,
                    "pid": mp.info.pid,
                    "name": mp.info.name,
                    "command": mp.info.command,
                    "cwd": mp.info.cwd,
                    "status": mp.info.status.value,
                    "exit_code": mp.info.exit_code,
                    "signal": mp.info.signal,
                    "started_at": mp.info.started_at,
                    "ended_at": mp.info.ended_at,
                    "duration_ms": round(
                        mp.info.duration_ms
                        if mp.info.ended_at is not None
                        else (time.monotonic() - mp.info.start_monotonic) * 1000,
                        1,
                    ),
                    "stdout_bytes": mp.stdout_buf.byte_count,
                    "stderr_bytes": mp.stderr_buf.byte_count,
                }
            )
        processes.reverse()  # Most recent first
        return {"processes": processes[:limit]}

    async def cleanup(
        self,
        completed_older_than_ms: int = 3600000,
        stopped_older_than_ms: int = 3600000,
    ) -> dict[str, Any]:
        """Remove completed/stopped processes older than thresholds."""
        now = time.time()
        removed = 0
        to_remove: list[str] = []

        for pid, mp in self._processes.items():
            if mp.info.status == ProcessStatus.RUNNING:
                continue

            age_ms = (now - (mp.info.ended_at or mp.info.started_at)) * 1000

            if mp.info.status == ProcessStatus.COMPLETED and age_ms > completed_older_than_ms:
                to_remove.append(pid)
            elif mp.info.status in (
                ProcessStatus.STOPPED, ProcessStatus.TIMED_OUT,
                ProcessStatus.FAILED,
            ):
                if age_ms > stopped_older_than_ms:
                    to_remove.append(pid)

        for pid in to_remove:
            del self._processes[pid]
            removed += 1

        return {"removed": removed}

    def get_process(self, process_id: str) -> ManagedProcess | None:
        return self._processes.get(process_id)
