"""Process registry and lifecycle management."""

from __future__ import annotations

import asyncio
import codecs
import os
import sys
import time
import uuid
from typing import Any, Iterable

from ..config import Config
from ..policy import (
    FINAL_DRAIN_MS,
    GRACEFUL_STOP_MS,
    MAX_EFFECTIVE_WAIT_MS,
    PENDING_SPAWN_SHUTDOWN_MS,
    PROCESS_GROUP_EXIT_MS,
)
from ..security import (
    StreamingRedactor,
    collect_sensitive_env,
    redact_text,
)
from ..side_effects import validate_side_effects
from ..types import ProcessInfo, ProcessStatus, SideEffect
from .managed import ManagedProcess
from .ring_buffer import RingBuffer, read_buffers
from .spawn import kill_process, spawn_process, terminate_process

_SHUTDOWN_REJECT_ERROR = "Server is shutting down"

class ProcessManager:
    """Registry and lifecycle manager for managed shell processes."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._processes: dict[str, ManagedProcess] = {}
        self._pending_spawns = 0
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_requested = False
        self._shutdown_complete = False

    async def _wait_for_no_pending_spawns(self) -> None:
        deadline = time.monotonic() + PENDING_SPAWN_SHUTDOWN_MS / 1000.0
        while time.monotonic() < deadline:
            async with self._shutdown_lock:
                if self._pending_spawns == 0:
                    return
            await asyncio.sleep(0.01)

    async def _reject_spawned_process(
        self,
        proc: asyncio.subprocess.Process,
        process_group_id: int | None,
        drain_tasks: list[asyncio.Task[None]],
    ) -> None:
        for task in drain_tasks:
            if not task.done():
                task.cancel()
        if drain_tasks:
            await asyncio.gather(*drain_tasks, return_exceptions=True)
        await kill_process(proc, process_group_id)

    def _shutdown_reject_response(self) -> dict[str, Any]:
        return {"status": "failed_to_start", "error": _SHUTDOWN_REJECT_ERROR}

    def _new_id(self) -> str:
        while True:
            process_id = f"proc_{uuid.uuid4().hex[:12]}"
            if process_id not in self._processes:
                return process_id

    def _max_output(self, requested: int | None) -> int:
        cap = self._config.max_output_bytes
        if requested is None or requested <= 0:
            return cap
        return min(requested, cap)

    def _read_snapshot(
        self, mp: ManagedProcess, max_output_bytes: int
    ) -> dict[str, Any]:
        data = read_buffers(
            {"stdout": mp.stdout_buf, "stderr": mp.stderr_buf},
            since_seq=None,
            max_bytes=max_output_bytes,
        )
        return {
            "stdout": self._redact(mp, data["texts"]["stdout"]),
            "stderr": self._redact(mp, data["texts"]["stderr"]),
            "next_seq": data["next_seq"],
            "truncated": data["truncated"],
        }

    def _redact(self, mp: ManagedProcess, text: str) -> str:
        return redact_text(self._config, text, mp.sensitive_env)

    def _clamp_yield_ms(self, requested: int | None) -> int:
        selected = self._config.default_yield_ms if requested is None else requested
        return max(0, min(selected, self._config.max_yield_ms, MAX_EFFECTIVE_WAIT_MS))

    def _clamp_timeout_ms(self, requested: int | None) -> int:
        if requested is None:
            return self._config.default_timeout_ms
        return max(0, requested)

    @staticmethod
    def _is_terminal(mp: ManagedProcess) -> bool:
        return mp.info.status != ProcessStatus.RUNNING

    def _has_live_work(self, mp: ManagedProcess) -> bool:
        return not self._is_terminal(mp) or self._process_group_exists(mp)

    def _reported_status(self, mp: ManagedProcess) -> str:
        """Status exposed to tools; descendants keep the record logically running."""
        if self._has_live_work(mp) and mp.info.status == ProcessStatus.COMPLETED:
            return ProcessStatus.RUNNING.value
        return mp.info.status.value

    def _reap_terminal_processes(self) -> int:
        """Apply configured age and count retention to terminal records."""
        now = time.time()
        retention_ms = self._config.process_retention_ms
        expired = [
            process_id
            for process_id, mp in self._processes.items()
            if self._is_terminal(mp)
            and not self._has_live_work(mp)
            and (now - (mp.info.ended_at or mp.info.started_at)) * 1000 > retention_ms
        ]
        for process_id in expired:
            self._remove_process(process_id)

        terminal = sorted(
            (
                (process_id, mp)
                for process_id, mp in self._processes.items()
                if self._is_terminal(mp) and not self._has_live_work(mp)
            ),
            key=lambda item: (
                item[1].info.ended_at or item[1].info.started_at,
                item[1].info.started_at,
                item[0],
            ),
        )
        overflow = len(terminal) - self._config.max_retained_processes
        for process_id, _ in terminal[: max(0, overflow)]:
            self._remove_process(process_id)
        return len(expired) + max(0, overflow)

    def _remove_process(self, process_id: str) -> None:
        mp = self._processes.pop(process_id, None)
        if mp is None:
            return
        current = asyncio.current_task()
        for task in (mp.timeout_task, mp.group_watch_task):
            if task is not None and task is not current and not task.done():
                task.cancel()

    async def exec_command(
        self,
        command: str,
        side_effects: list[SideEffect] | Iterable[SideEffect],
        cwd: str | None = None,
        env_overlay: dict[str, str] | None = None,
        shell: str | None = None,
        stdin: str | None = None,
        close_stdin: bool = True,
        name: str | None = None,
        yield_ms: int | None = None,
        timeout_ms: int | None = None,
        max_output_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Execute a shell command with auto-yield behavior.

        The ``side_effects`` declaration is validated before any cwd or command
        policy check, before process-limit checks, before environment overlay
        construction, and before subprocess spawn.
        """
        from ..security import build_env, resolve_cwd, validate_command

        # Validate side-effect declaration first so blocked categories never
        # reach cwd resolution, command policy, process limits, env overlay
        # building, or process spawn.
        side_effects_error = validate_side_effects(
            side_effects, self._config.blocked_side_effects
        )
        if side_effects_error:
            return {"status": "failed_to_start", "error": side_effects_error}

        # Validate command and explicit shell policy
        cmd_error = validate_command(self._config, command)
        if cmd_error:
            return {"status": "failed_to_start", "error": cmd_error}
        if shell is not None:
            if not shell.strip():
                return {
                    "status": "failed_to_start",
                    "error": "Shell executable must not be empty",
                }
            shell_error = validate_command(self._config, shell)
            if shell_error:
                return {
                    "status": "failed_to_start",
                    "error": f"Shell rejected by policy: {shell_error}",
                }

        # Resolve and validate cwd
        resolved_cwd, cwd_error = resolve_cwd(self._config, cwd)
        if cwd_error:
            return {"status": "failed_to_start", "error": cwd_error}

        self._reap_terminal_processes()

        # Atomically reserve capacity before environment construction and spawn.
        async with self._shutdown_lock:
            if self._shutdown_complete or self._shutdown_requested:
                return self._shutdown_reject_response()
            running_count = sum(
                1 for p in self._processes.values() if self._has_live_work(p)
            ) + self._pending_spawns
            if running_count >= self._config.max_processes:
                return {
                    "status": "failed_to_start",
                    "error": (
                        f"Maximum process limit ({self._config.max_processes}) reached"
                    ),
                }
            self._pending_spawns += 1

        try:
            env = build_env(self._config, env_overlay)
            effective_yield = self._clamp_yield_ms(yield_ms)
            effective_timeout = self._clamp_timeout_ms(timeout_ms)
            effective_max_output = self._max_output(max_output_bytes)
            proc = await spawn_process(
                command, cwd=resolved_cwd, env=env, shell=shell
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"status": "failed_to_start", "error": str(exc)}
        finally:
            async with self._shutdown_lock:
                self._pending_spawns -= 1

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

        mp = ManagedProcess(
            info,
            proc,
            effective_max_output,
            process_group_id,
            collect_sensitive_env(self._config, env_overlay),
        )

        # Start drain tasks immediately after spawn to prevent blocking on full pipe buffers
        drain_tasks = [
            asyncio.create_task(
                self._drain_stream(proc.stdout, mp.stdout_buf, mp.sensitive_env),
                name=f"drain-stdout-{process_id}",
            ),
            asyncio.create_task(
                self._drain_stream(proc.stderr, mp.stderr_buf, mp.sensitive_env),
                name=f"drain-stderr-{process_id}",
            ),
        ]
        mp.drain_stdout, mp.drain_stderr = drain_tasks

        async with self._shutdown_lock:
            if self._shutdown_complete or self._shutdown_requested:
                await self._reject_spawned_process(proc, process_group_id, drain_tasks)
                return self._shutdown_reject_response()
            # Register before any post-spawn await so shutdown can always find the process.
            self._processes[process_id] = mp

        mp.completion_task = asyncio.create_task(
            self._track_completion(proc, mp), name=f"completion-{process_id}"
        )

        if effective_timeout > 0:
            mp.timeout_task = asyncio.create_task(
                self._handle_timeout(mp, effective_timeout / 1000.0),
                name=f"timeout-{process_id}",
            )

        # Write initial input, then close by default so EOF-driven commands finish.
        try:
            if stdin is not None:
                if proc.stdin is not None:
                    proc.stdin.write(stdin.encode("utf-8"))
                    await proc.stdin.drain()
        except Exception:
            pass
        finally:
            if close_stdin:
                self._close_stdin(mp)

        # Wait up to yield_ms for completion
        try:
            await asyncio.wait_for(
                mp.completion_event.wait(), timeout=effective_yield / 1000.0
            )
        except asyncio.TimeoutError:
            pass

        self._reap_terminal_processes()

        duration_ms = (time.monotonic() - start_time) * 1000

        # Prepare output for response
        snapshot = self._read_snapshot(mp, effective_max_output)
        truncated = snapshot["truncated"]
        stdout_text = snapshot["stdout"]
        stderr_text = snapshot["stderr"]

        if (
            mp.info.status == ProcessStatus.COMPLETED
            and not self._has_live_work(mp)
        ):
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
        if mp.process_group_exited:
            return False
        if sys.platform == "win32":
            exists = mp.proc.returncode is None
            if not exists:
                mp.process_group_exited = True
            return exists
        try:
            os.killpg(self._process_group_id(mp), 0)
        except ProcessLookupError:
            mp.process_group_exited = True
            return False
        except PermissionError:
            return True
        return True

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader | None,
        buf: RingBuffer,
        sensitive_env: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Read from a subprocess stream into a ring buffer."""
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        redactor = StreamingRedactor(sensitive_env)
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk, final=False)
                if text:
                    redacted = redactor.feed(text)
                    if redacted:
                        buf.append(redacted.encode("utf-8"))
        except Exception:
            pass
        finally:
            tail = decoder.decode(b"", final=True)
            redacted = redactor.feed(tail, final=True)
            if redacted:
                buf.append(redacted.encode("utf-8"))

    async def _track_completion(
        self, proc: asyncio.subprocess.Process, mp: ManagedProcess
    ) -> None:
        """Wait for process to exit and update status."""
        wait_completed = False
        try:
            returncode, wait_completed = await self._wait_for_returncode(proc)
            if wait_completed:
                await self._wait_for_process_group_exit(
                    mp, timeout_sec=PROCESS_GROUP_EXIT_MS / 1000.0
                )
                await self._drain_with_timeout(mp, timeout_sec=FINAL_DRAIN_MS / 1000.0)
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
                if not mp._timeout_triggered and not self._process_group_exists(mp):
                    mp.timeout_task.cancel()
            pg_alive = self._process_group_exists(mp)
            if mp._timeout_triggered and pg_alive:
                pass
            elif not wait_completed and pg_alive:
                pass
            else:
                mp.completion_event.set()
            if pg_alive and mp.group_watch_task is None:
                mp.group_watch_task = asyncio.create_task(
                    self._watch_process_group_exit(mp),
                    name=f"group-watch-{mp.info.process_id}",
                )

    async def _wait_for_process_group_exit(
        self, mp: ManagedProcess, timeout_sec: float
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        while self._process_group_exists(mp) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _watch_process_group_exit(self, mp: ManagedProcess) -> None:
        while self._process_group_exists(mp):
            await asyncio.sleep(0.1)
        if mp._timeout_triggered:
            return
        if mp.timeout_task is not None and not mp.timeout_task.done():
            mp.timeout_task.cancel()
        mp.completion_event.set()

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
        # The subprocess may have exited naturally while its completion tracker
        # was waiting to run. Do not relabel that race as a timeout.
        if mp.proc.returncode is not None and not self._process_group_exists(mp):
            return
        if not self._has_live_work(mp):
            return
        mp._timeout_triggered = True
        mp.info.status = ProcessStatus.TIMED_OUT
        # Graceful termination
        await terminate_process(mp.proc, mp.process_group_id)
        grace_period = GRACEFUL_STOP_MS / 1000.0
        await self._wait_for_process_group_exit(mp, timeout_sec=grace_period)

        if self._process_group_exists(mp):
            # Force kill any children that survived graceful termination, even if
            # the shell process already exited.
            await kill_process(mp.proc, mp.process_group_id)
            await self._wait_for_process_group_exit(
                mp, timeout_sec=PROCESS_GROUP_EXIT_MS / 1000.0
            )

        if not self._process_group_exists(mp):
            await self._drain_with_timeout(mp, timeout_sec=FINAL_DRAIN_MS / 1000.0)

        mp.completion_event.set()

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

        selected: dict[str, RingBuffer] = {}
        if streams in ("both", "stdout"):
            selected["stdout"] = mp.stdout_buf
        if streams in ("both", "stderr"):
            selected["stderr"] = mp.stderr_buf
        data = read_buffers(selected, since_seq=since_seq, max_bytes=effective_max)

        result: dict[str, Any] = {
            "process_id": process_id,
            "status": self._reported_status(mp),
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
            "next_seq": data["next_seq"],
            "truncated": data["truncated"],
        }
        for stream, text in data["texts"].items():
            result[stream] = self._redact(mp, text)
        return result

    async def write_input(
        self,
        process_id: str,
        input_data: str,
        newline: bool = False,
        close_stdin: bool = False,
    ) -> dict[str, Any]:
        """Write to stdin of a managed process."""
        mp = self._processes.get(process_id)
        if mp is None:
            return {
                "process_id": process_id, "ok": False,
                "error": f"Unknown process_id: {process_id}",
            }

        if self._reported_status(mp) != ProcessStatus.RUNNING.value:
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
        finally:
            if close_stdin:
                self._close_stdin(mp)

    @staticmethod
    def _close_stdin(mp: ManagedProcess) -> None:
        stdin = mp.proc.stdin
        if stdin is None:
            return
        is_closing = getattr(stdin, "is_closing", None)
        if is_closing is not None and is_closing():
            return
        close = getattr(stdin, "close", None)
        if close is not None:
            close()

    async def wait_process(
        self,
        process_id: str,
        timeout_ms: int = MAX_EFFECTIVE_WAIT_MS,
        max_output_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Wait for a process to exit without killing it."""
        mp = self._processes.get(process_id)
        if mp is None:
            return {"process_id": process_id, "error": f"Unknown process_id: {process_id}"}

        # Cap effective wait below typical MCP request timeout thresholds
        effective_wait_ms = max(0, min(timeout_ms, MAX_EFFECTIVE_WAIT_MS))

        if self._has_live_work(mp):
            try:
                await asyncio.wait_for(
                    mp.completion_event.wait(), timeout=effective_wait_ms / 1000.0
                )
            except asyncio.TimeoutError:
                pass

        effective_max = self._max_output(max_output_bytes)
        snapshot = self._read_snapshot(mp, effective_max)

        return {
            "process_id": process_id,
            "status": self._reported_status(mp),
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
            "stdout": snapshot["stdout"],
            "stderr": snapshot["stderr"],
            "next_seq": snapshot["next_seq"],
            "truncated": snapshot["truncated"],
        }

    async def stop_process(
        self,
        process_id: str,
        signal_name: str = "SIGTERM",
        force_after_ms: int = GRACEFUL_STOP_MS,
    ) -> dict[str, Any]:
        """Stop a running process with graceful termination then force kill."""
        from .spawn import get_signal

        mp = self._processes.get(process_id)
        if mp is None:
            return {
                "process_id": process_id, "stopped": False,
                "error": f"Unknown process_id: {process_id}",
            }

        if not self._has_live_work(mp):
            return {
                "process_id": process_id,
                "stopped": False,
                "error": f"Process is not running (status: {mp.info.status.value})",
            }

        # Send requested signal
        sig = get_signal(signal_name)
        if sig is None:
            return {
                "process_id": process_id,
                "stopped": False,
                "error": f"Invalid signal: {signal_name!r}",
            }
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
        effective_force_ms = max(0, min(force_after_ms, MAX_EFFECTIVE_WAIT_MS))
        await self._wait_for_process_group_exit(
            mp, timeout_sec=effective_force_ms / 1000.0
        )

        if self._process_group_exists(mp):
            # The primary shell may exit before a resistant descendant.
            await kill_process(mp.proc, mp.process_group_id)
            await self._wait_for_process_group_exit(
                mp, timeout_sec=PROCESS_GROUP_EXIT_MS / 1000.0
            )
            await self._drain_with_timeout(mp, timeout_sec=FINAL_DRAIN_MS / 1000.0)

        if self._process_group_exists(mp):
            return {
                "process_id": process_id,
                "stopped": False,
                "signal": signal_name,
                "error": "Process group did not stop after force kill",
            }

        # If the process exited due to our signal, mark it as STOPPED.
        # _track_completion may have set COMPLETED, but since we initiated
        # termination, the correct terminal status is STOPPED.
        if mp.info.status in (ProcessStatus.RUNNING, ProcessStatus.COMPLETED):
            mp.info.status = ProcessStatus.STOPPED
            mp.info.ended_at = time.time()
            mp.info.duration_ms = (
                time.monotonic() - mp.info.start_monotonic
            ) * 1000

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
            reported_status = self._reported_status(mp)
            if not include_completed and reported_status != ProcessStatus.RUNNING.value:
                continue
            processes.append(
                {
                    "process_id": mp.info.process_id,
                    "pid": mp.info.pid,
                    "name": (
                        self._redact(mp, mp.info.name)
                        if mp.info.name is not None
                        else None
                    ),
                    "command": self._redact(mp, mp.info.command),
                    "cwd": mp.info.cwd,
                    "status": reported_status,
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
        return {"processes": processes[: max(0, limit)]}

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
            if self._has_live_work(mp):
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
            self._remove_process(pid)
            removed += 1

        return {"removed": removed}

    async def shutdown(self) -> None:
        """Stop all live managed process groups and settle their tracking tasks."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_requested = True

        await self._wait_for_no_pending_spawns()

        while True:
            live = [
                mp
                for mp in self._processes.values()
                if self._has_live_work(mp)
            ]
            if not live:
                async with self._shutdown_lock:
                    if self._pending_spawns == 0:
                        self._shutdown_complete = True
                return

            running_at_start = {id(mp) for mp in live if not self._is_terminal(mp)}

            await asyncio.gather(
                *(terminate_process(mp.proc, mp.process_group_id) for mp in live),
                return_exceptions=True,
            )

            deadline = time.monotonic() + GRACEFUL_STOP_MS / 1000.0
            while any(self._process_group_exists(mp) for mp in live):
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.05)

            survivors = [mp for mp in live if self._process_group_exists(mp)]
            await asyncio.gather(
                *(kill_process(mp.proc, mp.process_group_id) for mp in survivors),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(
                    self._wait_for_process_group_exit(
                        mp, timeout_sec=PROCESS_GROUP_EXIT_MS / 1000.0
                    )
                    for mp in survivors
                ),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(
                    self._drain_with_timeout(mp, timeout_sec=FINAL_DRAIN_MS / 1000.0)
                    for mp in live
                ),
                return_exceptions=True,
            )

            for mp in live:
                if mp.timeout_task is not None and not mp.timeout_task.done():
                    mp.timeout_task.cancel()
                if mp.completion_task is not None and not mp.completion_task.done():
                    mp.completion_task.cancel()
                if mp.group_watch_task is not None and not mp.group_watch_task.done():
                    mp.group_watch_task.cancel()
                if id(mp) in running_at_start and mp.info.status in (
                    ProcessStatus.RUNNING,
                    ProcessStatus.COMPLETED,
                ):
                    mp.info.status = ProcessStatus.STOPPED
                    if mp.info.ended_at is None:
                        mp.info.ended_at = time.time()
                        mp.info.duration_ms = (
                            time.monotonic() - mp.info.start_monotonic
                        ) * 1000
                mp.completion_event.set()

            tasks = [
                task
                for mp in live
                for task in (mp.timeout_task, mp.completion_task, mp.group_watch_task)
                if task is not None
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            async with self._shutdown_lock:
                self._shutdown_complete = True
            return

    def get_process(self, process_id: str) -> ManagedProcess | None:
        return self._processes.get(process_id)
