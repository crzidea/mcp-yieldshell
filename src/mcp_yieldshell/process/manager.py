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
from .ring_buffer import RingBuffer, read_buffers, tail_start_seq
from .spawn import kill_process, spawn_process, terminate_process

_SHUTDOWN_REJECT_ERROR = "Server is shutting down"

class ProcessManager:
    """Registry and lifecycle manager for managed shell processes."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._processes: dict[str, ManagedProcess] = {}
        self._pending_spawns = 0
        self._pending_spawn_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_requested = False
        self._shutdown_complete = False
        self._shutdown_task: asyncio.Task[None] | None = None

    async def _wait_for_no_pending_spawns(self) -> None:
        deadline = time.monotonic() + PENDING_SPAWN_SHUTDOWN_MS / 1000.0
        while time.monotonic() < deadline:
            async with self._shutdown_lock:
                if self._pending_spawns == 0:
                    return
            await asyncio.sleep(0.01)

    async def _cancel_pending_spawns(self) -> None:
        """Cancel and settle spawn calls that exceeded the shutdown wait."""
        current = asyncio.current_task()
        async with self._shutdown_lock:
            tasks = [
                task
                for task in self._pending_spawn_tasks
                if task is not current and not task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _release_spawn_reservation(
        self, spawn_owner: asyncio.Task[Any] | None
    ) -> None:
        """Release capacity without introducing a cancellation point."""
        if spawn_owner is None or spawn_owner not in self._pending_spawn_tasks:
            return
        self._pending_spawns -= 1
        self._pending_spawn_tasks.discard(spawn_owner)

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
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        self._close_process_pipes(proc)

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
        self,
        mp: ManagedProcess,
        max_output_bytes: int,
        since_seq: int | None = None,
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        buffers = {"stdout": mp.stdout_buf, "stderr": mp.stderr_buf}
        data = self._read_for_response(
            mp, buffers, max_output_bytes, since_seq, tail_lines
        )
        snapshot = {
            "stdout": self._redact(mp, data["texts"]["stdout"]),
            "stderr": self._redact(mp, data["texts"]["stderr"]),
        }
        snapshot.update(self._cursor_fields(mp.info.process_id, data))
        return snapshot

    @staticmethod
    def _read_for_response(
        mp: ManagedProcess,
        buffers: dict[str, RingBuffer],
        max_output_bytes: int,
        since_seq: int | None,
        tail_lines: int | None,
        resumable: bool = True,
    ) -> dict[str, Any]:
        """Read buffers, resuming from the process cursor when none is given.

        A request with no ``since_seq`` and no ``tail_lines`` continues from
        where the last such read stopped and then advances that cursor, so a
        caller can poll repeatedly without tracking ``next_seq`` itself.

        Requests that name an explicit ``since_seq``, ask for ``tail_lines``,
        or narrow ``streams`` are treated as out-of-band inspections: they
        neither consult nor move the cursor. That keeps a peek at the tail
        from silently skipping output the polling stream has not seen, and
        keeps a narrowed read from advancing the shared cursor past bytes on
        the stream it excluded.
        """
        resume = resumable and since_seq is None and tail_lines is None
        if resume:
            since_seq = mp.read_cursor
        elif tail_lines is not None:
            since_seq = tail_start_seq(buffers, tail_lines, max_output_bytes)
        data = read_buffers(
            buffers, since_seq=since_seq, max_bytes=max_output_bytes
        )
        if resume:
            mp.read_cursor = data["next_seq"]
        return data

    @staticmethod
    def _cursor_fields(process_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Cursor and withheld-output fields shared by exec, read, and wait."""
        fields: dict[str, Any] = {
            "start_seq": data["start_seq"],
            "next_seq": data["next_seq"],
            "latest_seq": data["latest_seq"],
            "capped": data["capped"],
            "evicted": data["evicted"],
        }
        hints = []
        if data["capped"]:
            hints.append(
                "More output is available; read again to continue, or pass "
                f"since_seq={data['next_seq']} explicitly "
                f"(process_id={process_id!r})."
            )
        if data["evicted"]:
            hints.append(
                "Older output was dropped from the buffer before this read and "
                "cannot be recovered; poll more often or raise "
                "YIELDSHELL_MAX_BUFFER_BYTES."
            )
        if hints:
            fields["hint"] = " ".join(hints)
        return fields

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
    def _clamp_stop_grace_ms(requested: int) -> int:
        reserved_ms = PROCESS_GROUP_EXIT_MS + FINAL_DRAIN_MS + 1_000
        max_grace_ms = max(0, MAX_EFFECTIVE_WAIT_MS - reserved_ms)
        return max(0, min(requested, max_grace_ms))

    @staticmethod
    def _is_terminal(mp: ManagedProcess) -> bool:
        return mp.info.status != ProcessStatus.RUNNING

    @staticmethod
    def _mark_ended(mp: ManagedProcess) -> None:
        mp.info.ended_at = time.time()
        mp.info.duration_ms = (
            time.monotonic() - mp.info.start_monotonic
        ) * 1000

    def _has_live_work(self, mp: ManagedProcess) -> bool:
        return not self._is_terminal(mp) or self._process_group_exists(mp)

    def _reported_status(
        self,
        mp: ManagedProcess,
        has_live_work: bool | None = None,
    ) -> str:
        """Status exposed to tools; descendants keep the record logically running."""
        live = self._has_live_work(mp) if has_live_work is None else has_live_work
        if live and mp.info.status == ProcessStatus.COMPLETED:
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
        for task in (mp.timeout_task, mp.group_watch_task, mp.stdin_task):
            if task is not None and task is not current and not task.done():
                task.cancel()

    @staticmethod
    def _with_stdin_error(
        mp: ManagedProcess, result: dict[str, Any]
    ) -> dict[str, Any]:
        if mp.stdin_error is not None:
            result["stdin_error"] = mp.stdin_error
        return result

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
            spawn_owner = asyncio.current_task()
            if spawn_owner is not None:
                self._pending_spawn_tasks.add(spawn_owner)

        try:
            env = build_env(self._config, env_overlay)
            effective_yield = self._clamp_yield_ms(yield_ms)
            effective_timeout = self._clamp_timeout_ms(timeout_ms)
            effective_max_output = self._max_output(max_output_bytes)
            proc = await spawn_process(
                command, cwd=resolved_cwd, env=env, shell=shell
            )
        except asyncio.CancelledError:
            self._release_spawn_reservation(spawn_owner)
            raise
        except Exception as exc:
            self._release_spawn_reservation(spawn_owner)
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

        mp = ManagedProcess(
            info,
            proc,
            self._config.max_buffer_bytes,
            process_group_id,
            collect_sensitive_env(self._config, env_overlay),
        )

        # Start drain tasks immediately after spawn to prevent blocking on full pipe buffers
        drain_tasks = [
            asyncio.create_task(
                self._drain_stream(proc.stdout, mp.stdout_buf, mp),
                name=f"drain-stdout-{process_id}",
            ),
            asyncio.create_task(
                self._drain_stream(proc.stderr, mp.stderr_buf, mp),
                name=f"drain-stderr-{process_id}",
            ),
        ]
        mp.drain_stdout, mp.drain_stderr = drain_tasks

        try:
            async with self._shutdown_lock:
                reject_for_shutdown = (
                    self._shutdown_complete or self._shutdown_requested
                )
                if not reject_for_shutdown:
                    # Register before releasing the pending-spawn reservation so
                    # shutdown can find every subprocess throughout the handoff.
                    self._processes[process_id] = mp
                self._release_spawn_reservation(spawn_owner)
        except asyncio.CancelledError:
            self._release_spawn_reservation(spawn_owner)
            await asyncio.shield(
                self._reject_spawned_process(proc, process_group_id, drain_tasks)
            )
            raise

        if reject_for_shutdown:
            await self._reject_spawned_process(proc, process_group_id, drain_tasks)
            return self._shutdown_reject_response()

        mp.completion_task = asyncio.create_task(
            self._track_completion(proc, mp), name=f"completion-{process_id}"
        )

        if effective_timeout > 0:
            mp.timeout_task = asyncio.create_task(
                self._handle_timeout(mp, effective_timeout / 1000.0),
                name=f"timeout-{process_id}",
            )

        # Initial input is tracked independently so pipe backpressure cannot
        # prevent exec from honoring its auto-yield deadline.
        if stdin is not None:
            mp.stdin_task = asyncio.create_task(
                self._write_initial_input(mp, stdin, close_stdin),
                name=f"stdin-{process_id}",
            )
            # Let immediately successful writes and failures settle before the
            # response path without waiting for a backpressured pipe.
            await asyncio.sleep(0)
        elif close_stdin:
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
        withheld = snapshot["capped"] or snapshot["evicted"]
        # Every branch carries the cursor so a caller can keep reading without
        # replaying output it already received.
        common = {"duration_ms": round(duration_ms, 1), **snapshot}
        exited = {
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
        }

        if (
            mp.info.status == ProcessStatus.COMPLETED
            and not self._has_live_work(mp)
        ):
            result: dict[str, Any] = {"status": "completed", **exited, **common}
            if withheld and self._processes.get(process_id) is mp:
                result["process_id"] = process_id
            return self._with_stdin_error(mp, result)

        if mp.info.status == ProcessStatus.TIMED_OUT:
            return self._with_stdin_error(mp, {
                "status": "timed_out",
                "process_id": process_id,
                **exited,
                **common,
            })

        if mp.info.status == ProcessStatus.STOPPED:
            return self._with_stdin_error(mp, {
                "status": "stopped",
                "process_id": process_id,
                **exited,
                **common,
            })

        if mp.info.status == ProcessStatus.FAILED:
            return self._with_stdin_error(mp, {
                "status": "failed",
                "process_id": process_id,
                **exited,
                **common,
            })

        # Still running — background it
        return self._with_stdin_error(mp, {
            "status": "backgrounded",
            "process_id": process_id,
            "pid": mp.info.pid,
            **common,
            "message": "Process is running in the background. Use read/wait/stop with process_id.",
        })

    async def _write_initial_input(
        self, mp: ManagedProcess, input_data: str, close_stdin: bool
    ) -> None:
        stdin = mp.proc.stdin
        try:
            if stdin is None:
                mp.stdin_error = "Process stdin is unavailable"
                return
            stdin.write(input_data.encode("utf-8"))
            await stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mp.stdin_error = str(exc)
        finally:
            if close_stdin:
                self._close_stdin(mp)

    def _get_process_group_id(self, proc: asyncio.subprocess.Process) -> int | None:
        if sys.platform == "win32" or proc.pid is None:
            return None
        # spawn_process() uses start_new_session=True on POSIX, so the spawned
        # PID is also the new process-group ID. Deriving it directly avoids a
        # race where a fast shell exits before getpgid() runs while descendants
        # in its process group are still alive.
        return proc.pid

    def _process_group_id(self, mp: ManagedProcess) -> int:
        if mp.process_group_id is not None:
            return mp.process_group_id
        if mp.proc.pid is None:
            raise ProcessLookupError
        return mp.proc.pid

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
        mp: ManagedProcess | None = None,
    ) -> None:
        """Read from a subprocess stream into a ring buffer."""
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        redactor = StreamingRedactor(mp.sensitive_env if mp is not None else ())
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
                if mp is not None:
                    # Tracked on raw arrival so a fully redacted chunk still
                    # counts as activity for idle reporting.
                    mp.last_output_at = time.time()
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
            self._mark_ended(mp)
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

    async def _await_live_work_end(
        self, mp: ManagedProcess, timeout_sec: float
    ) -> bool:
        """Wait until no live work remains; ``True`` if it ended in time.

        ``completion_event`` latches when the tracked shell exits, which can
        happen while descendants still hold the process group open. Waiting on
        the event alone would then return instantly on every call even though
        the record still reports ``running``, so the live-work check is what
        actually gates the deadline here.
        """
        deadline = time.monotonic() + timeout_sec
        while True:
            if not self._has_live_work(mp):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if mp.completion_event.is_set():
                # Latched early: poll the process group until it drains.
                await asyncio.sleep(min(0.05, remaining))
            else:
                try:
                    await asyncio.wait_for(mp.completion_event.wait(), remaining)
                except asyncio.TimeoutError:
                    return not self._has_live_work(mp)

    async def _wait_for_process_group_exit(
        self, mp: ManagedProcess, timeout_sec: float
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        while self._process_group_exists(mp) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _watch_process_group_exit(self, mp: ManagedProcess) -> None:
        while self._process_group_exists(mp):
            await asyncio.sleep(0.1)
        self._mark_ended(mp)
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
            self._close_output_pipes(mp)

    async def _settle_completion(self, mp: ManagedProcess) -> None:
        """Give the completion tracker time to reap an exited subprocess."""
        task = mp.completion_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            # The process group is checked by the caller. Keep the tracker alive
            # so a delayed platform transport can still be reaped later.
            pass

    def _cancel_drains(self, mp: ManagedProcess) -> None:
        for task in (mp.drain_stdout, mp.drain_stderr):
            if task is not None and not task.done():
                task.cancel()
        self._close_output_pipes(mp)

    @staticmethod
    def _close_output_pipes(mp: ManagedProcess) -> None:
        """Close subprocess read transports after bounded capture is abandoned."""
        ProcessManager._close_process_pipes(mp.proc)

    @staticmethod
    def _close_process_pipes(proc: asyncio.subprocess.Process) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()

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
            self._mark_ended(mp)

        mp.completion_event.set()

    async def read_output(
        self,
        process_id: str,
        since_seq: int | None = None,
        max_output_bytes: int | None = None,
        streams: str = "both",
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        """Read output from a managed process."""
        mp = self._processes.get(process_id)
        if mp is None:
            return {"process_id": process_id, "error": f"Unknown process_id: {process_id}"}

        if streams not in ("both", "stdout", "stderr"):
            return {"process_id": process_id, "error": f"Invalid streams: {streams!r}"}

        selection_error = self._validate_selection(since_seq, tail_lines)
        if selection_error:
            return {"process_id": process_id, "error": selection_error}

        effective_max = self._max_output(max_output_bytes)

        selected: dict[str, RingBuffer] = {}
        if streams in ("both", "stdout"):
            selected["stdout"] = mp.stdout_buf
        if streams in ("both", "stderr"):
            selected["stderr"] = mp.stderr_buf
        data = self._read_for_response(
            mp,
            selected,
            effective_max,
            since_seq,
            tail_lines,
            # A narrowed selection must not advance the cursor shared with the
            # stream it left out.
            resumable=streams == "both",
        )

        result: dict[str, Any] = {
            "process_id": process_id,
            "status": self._reported_status(mp),
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
        }
        result.update(self._cursor_fields(process_id, data))
        for stream, text in data["texts"].items():
            result[stream] = self._redact(mp, text)
        return self._with_stdin_error(mp, result)

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
        if mp.stdin_task is not None and not mp.stdin_task.done():
            return {
                "process_id": process_id,
                "ok": False,
                "error": "Initial stdin write is still in progress",
            }

        try:
            data = input_data.encode("utf-8")
            if newline:
                data += b"\n"
            mp.proc.stdin.write(data)
            await asyncio.wait_for(
                mp.proc.stdin.drain(),
                timeout=MAX_EFFECTIVE_WAIT_MS / 1000.0,
            )
            return {"process_id": process_id, "ok": True}
        except asyncio.TimeoutError:
            return {
                "process_id": process_id,
                "ok": False,
                "error": (
                    "Timed out waiting for process stdin to accept input "
                    f"after {MAX_EFFECTIVE_WAIT_MS}ms"
                ),
            }
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
        since_seq: int | None = None,
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        """Wait for a process to exit without killing it.

        ``timeout_ms`` is a maximum wait and never terminates the process.
        Pass ``since_seq`` from a previous response to receive only output
        produced since that cursor.
        """
        mp = self._processes.get(process_id)
        if mp is None:
            return {"process_id": process_id, "error": f"Unknown process_id: {process_id}"}

        selection_error = self._validate_selection(since_seq, tail_lines)
        if selection_error:
            return {"process_id": process_id, "error": selection_error}

        # Cap effective wait below typical MCP request timeout thresholds
        effective_wait_ms = max(0, min(timeout_ms, MAX_EFFECTIVE_WAIT_MS))

        started = time.monotonic()
        exited = await self._await_live_work_end(mp, effective_wait_ms / 1000.0)
        waited_ms = (time.monotonic() - started) * 1000

        effective_max = self._max_output(max_output_bytes)
        snapshot = self._read_snapshot(mp, effective_max, since_seq, tail_lines)

        return self._with_stdin_error(mp, {
            "process_id": process_id,
            "status": self._reported_status(mp),
            "exit_code": mp.info.exit_code,
            "signal": mp.info.signal,
            "wait_result": "exited" if exited else "deadline_reached",
            "waited_ms": round(waited_ms, 1),
            "max_wait_ms": effective_wait_ms,
            **snapshot,
        })

    @staticmethod
    def _validate_selection(
        since_seq: int | None, tail_lines: int | None
    ) -> str | None:
        if tail_lines is not None and since_seq is not None:
            return (
                "tail_lines and since_seq are mutually exclusive; use "
                "tail_lines for a snapshot of the newest output or since_seq "
                "to continue an incremental read"
            )
        if tail_lines is not None and tail_lines <= 0:
            return f"tail_lines must be positive, got {tail_lines}"
        return None

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
        # Reserve time for force-kill observation, final drain, and subprocess
        # reaping so the complete stop request stays transport-safe.
        effective_force_ms = self._clamp_stop_grace_ms(force_after_ms)
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

        # A fast graceful exit can make the process group disappear before the
        # completion tracker has reaped the subprocess transport or finished
        # draining its pipes. Settle it before reporting a completed stop.
        await self._drain_with_timeout(mp, timeout_sec=FINAL_DRAIN_MS / 1000.0)
        await self._settle_completion(mp)
        self._mark_ended(mp)

        # If the process exited due to our signal, mark it as STOPPED.
        # _track_completion may have set COMPLETED, but since we initiated
        # termination, the correct terminal status is STOPPED.
        if mp.info.status in (ProcessStatus.RUNNING, ProcessStatus.COMPLETED):
            mp.info.status = ProcessStatus.STOPPED

        return {
            "process_id": process_id,
            "stopped": not self._has_live_work(mp),
            "signal": signal_name,
            "error": None,
        }

    def list_processes(
        self, include_completed: bool = True, limit: int = 50
    ) -> dict[str, Any]:
        """List managed processes."""
        now = time.time()
        processes = []
        for mp in list(self._processes.values()):
            has_live_work = self._has_live_work(mp)
            reported_status = self._reported_status(mp, has_live_work)
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
                    "ended_at": None if has_live_work else mp.info.ended_at,
                    "duration_ms": round(
                        mp.info.duration_ms
                        if not has_live_work and mp.info.ended_at is not None
                        else (time.monotonic() - mp.info.start_monotonic) * 1000,
                        1,
                    ),
                    "stdout_bytes": mp.stdout_buf.byte_count,
                    "stderr_bytes": mp.stderr_buf.byte_count,
                    "last_output_at": mp.last_output_at,
                    # Distinguishes a genuine hang from quiet work.
                    "idle_ms": (
                        None
                        if mp.last_output_at is None
                        else round((now - mp.last_output_at) * 1000, 1)
                    ),
                    "latest_seq": mp.latest_seq,
                    "stdin_error": mp.stdin_error,
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
        if completed_older_than_ms < 0 or stopped_older_than_ms < 0:
            return {
                "removed": 0,
                "error": "Cleanup age thresholds must be non-negative",
            }

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
        """Stop all live process groups through one shared teardown task."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            task = self._shutdown_task
            if task is None:
                self._shutdown_requested = True
                task = asyncio.create_task(
                    self._run_shutdown(),
                    name="yieldshell-shutdown",
                )
                self._shutdown_task = task
        await asyncio.shield(task)

    async def _run_shutdown(self) -> None:
        """Perform the shutdown sequence once, independently of caller cancellation."""
        await self._wait_for_no_pending_spawns()
        await self._cancel_pending_spawns()

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
            await asyncio.gather(
                *(self._settle_completion(mp) for mp in live),
                return_exceptions=True,
            )
            for mp in live:
                if not self._process_group_exists(mp):
                    self._mark_ended(mp)

            for mp in live:
                if mp.timeout_task is not None and not mp.timeout_task.done():
                    mp.timeout_task.cancel()
                if mp.completion_task is not None and not mp.completion_task.done():
                    mp.completion_task.cancel()
                if mp.group_watch_task is not None and not mp.group_watch_task.done():
                    mp.group_watch_task.cancel()
                if mp.stdin_task is not None and not mp.stdin_task.done():
                    mp.stdin_task.cancel()
                self._close_output_pipes(mp)
                if id(mp) in running_at_start and mp.info.status in (
                    ProcessStatus.RUNNING,
                    ProcessStatus.COMPLETED,
                ):
                    mp.info.status = ProcessStatus.STOPPED
                mp.completion_event.set()

            tasks = [
                task
                for mp in live
                for task in (
                    mp.timeout_task,
                    mp.completion_task,
                    mp.group_watch_task,
                    mp.stdin_task,
                )
                if task is not None
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            async with self._shutdown_lock:
                self._shutdown_complete = True
            return

    def get_process(self, process_id: str) -> ManagedProcess | None:
        return self._processes.get(process_id)
