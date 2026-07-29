"""Retention, shared timing, and server shutdown regression tests."""

import asyncio
import os
import signal
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.policy import (
    FINAL_DRAIN_MS,
    GRACEFUL_STOP_MS,
    MAX_EFFECTIVE_WAIT_MS,
    PROCESS_GROUP_EXIT_MS,
)
from mcp_yieldshell.process.managed import ManagedProcess
from mcp_yieldshell.process.manager import ProcessManager
from mcp_yieldshell.process.spawn import kill_process, terminate_process
from mcp_yieldshell.server import _server_lifespan, create_server, mcp
from mcp_yieldshell.types import ProcessInfo, ProcessStatus, SideEffect

NONE = [SideEffect.NONE]


class TestSharedPolicy:
    def test_defaults_are_shared_policy_values(self):
        config = Config()
        assert config.default_yield_ms == 30_000
        assert config.default_timeout_ms == 3_600_000
        assert MAX_EFFECTIVE_WAIT_MS == 55_000
        assert GRACEFUL_STOP_MS == 10_000
        assert PROCESS_GROUP_EXIT_MS == 5_000
        assert FINAL_DRAIN_MS == 3_000

    def test_public_tool_defaults_use_shared_policy(self):
        from inspect import signature

        from mcp_yieldshell.server import stop, wait

        assert signature(wait).parameters["timeout_ms"].default == MAX_EFFECTIVE_WAIT_MS
        assert signature(stop).parameters["force_after_ms"].default == GRACEFUL_STOP_MS


class TestTerminalStateAccuracy:
    @pytest.mark.asyncio
    async def test_failed_force_kill_does_not_report_stopped(self, monkeypatch):
        manager = ProcessManager(Config())
        process = MagicMock()
        process.pid = None
        process.returncode = None
        process.stdin = None
        info = ProcessInfo(
            process_id="survivor",
            pid=None,
            command="survivor",
            cwd=os.getcwd(),
            name=None,
            status=ProcessStatus.RUNNING,
            started_at=time.time(),
            start_monotonic=time.monotonic(),
        )
        managed = ManagedProcess(info, process, 100)
        manager._processes[info.process_id] = managed
        monkeypatch.setattr(manager, "_process_group_exists", lambda _mp: True)
        monkeypatch.setattr(manager, "_wait_for_process_group_exit", AsyncMock())
        monkeypatch.setattr(manager, "_drain_with_timeout", AsyncMock())

        result = await manager.stop_process(
            info.process_id,
            force_after_ms=0,
        )

        assert result["stopped"] is False
        assert "did not stop" in result["error"]
        assert managed.info.status == ProcessStatus.RUNNING


class TestDrainLifecycle:
    @pytest.mark.asyncio
    async def test_timed_out_drains_close_abandoned_pipe_transports(self):
        manager = ProcessManager(Config())
        process = MagicMock()
        process.pid = None
        process.returncode = 0
        process.stdin = None
        process.stdout = MagicMock()
        process.stderr = MagicMock()
        info = ProcessInfo(
            process_id="pipes",
            pid=None,
            command="child",
            cwd=os.getcwd(),
            name=None,
            status=ProcessStatus.COMPLETED,
            started_at=time.time(),
            start_monotonic=time.monotonic(),
        )
        managed = ManagedProcess(info, process, 100)

        async def blocked_drain():
            await asyncio.Event().wait()

        managed.drain_stdout = asyncio.create_task(blocked_drain())
        managed.drain_stderr = asyncio.create_task(blocked_drain())

        await manager._drain_with_timeout(managed, timeout_sec=0.01)

        assert managed.drain_stdout.cancelled()
        assert managed.drain_stderr.cancelled()
        process.stdout._transport.close.assert_called_once_with()
        process.stderr._transport.close.assert_called_once_with()


class TestConcurrentShutdown:
    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_termination_sequence(
        self, monkeypatch
    ):
        manager = ProcessManager(Config())
        process = MagicMock()
        process.pid = None
        process.returncode = None
        process.stdin = None
        process.stdout = None
        process.stderr = None
        info = ProcessInfo(
            process_id="shutdown",
            pid=None,
            command="long-running",
            cwd=os.getcwd(),
            name=None,
            status=ProcessStatus.RUNNING,
            started_at=time.time(),
            start_monotonic=time.monotonic(),
        )
        managed = ManagedProcess(info, process, 100)
        manager._processes[info.process_id] = managed
        terminate_calls = 0

        def process_group_exists(mp):
            return not mp.process_group_exited

        async def terminate_once(_proc, _process_group_id):
            nonlocal terminate_calls
            terminate_calls += 1
            await asyncio.sleep(0.02)
            managed.process_group_exited = True

        monkeypatch.setattr(manager, "_process_group_exists", process_group_exists)
        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.terminate_process",
            terminate_once,
        )

        await asyncio.gather(manager.shutdown(), manager.shutdown())

        assert terminate_calls == 1
        assert manager._shutdown_complete is True
        assert managed.info.status == ProcessStatus.STOPPED


class TestAutomaticRetention:
    @pytest.mark.asyncio
    async def test_zero_cap_is_enforced_after_completion(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_RETAINED_PROCESSES", "0")
        manager = ProcessManager(Config())

        result = await manager.exec_command("echo returned", side_effects=NONE)

        assert result["status"] == "completed"
        assert "returned" in result["stdout"]
        assert manager.list_processes()["processes"] == []

    @pytest.mark.asyncio
    async def test_zero_cap_does_not_return_dangling_truncated_process_id(
        self, monkeypatch
    ):
        monkeypatch.setenv("YIELDSHELL_MAX_RETAINED_PROCESSES", "0")
        manager = ProcessManager(Config())

        result = await manager.exec_command(
            "seq 1 20000",
            max_output_bytes=100,
            side_effects=NONE,
        )

        assert result["status"] == "completed"
        assert result["capped"] is True
        assert "process_id" not in result

    @pytest.mark.asyncio
    async def test_reaps_every_terminal_state_and_ids_become_unknown(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_PROCESS_RETENTION_MS", "1000")
        manager = ProcessManager(Config())
        process_ids = []
        for index, status in enumerate(
            (
                ProcessStatus.COMPLETED,
                ProcessStatus.STOPPED,
                ProcessStatus.TIMED_OUT,
                ProcessStatus.FAILED,
            )
        ):
            await manager.exec_command(f"echo {index}", side_effects=NONE)
            process_id = manager.list_processes(limit=1)["processes"][0]["process_id"]
            mp = manager.get_process(process_id)
            assert mp is not None
            mp.info.status = status
            mp.info.ended_at = time.time() - 2
            process_ids.append(process_id)

        await manager.exec_command("echo trigger", side_effects=NONE)
        listed = {item["process_id"] for item in manager.list_processes()["processes"]}
        assert not listed.intersection(process_ids)
        for process_id in process_ids:
            assert "error" in await manager.read_output(process_id)
            assert "error" in await manager.wait_process(process_id)
            assert "error" in await manager.write_input(process_id, "x")
            assert "error" in await manager.stop_process(process_id)

    @pytest.mark.asyncio
    async def test_age_boundary_is_strict_and_reaping_is_repeatable(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_PROCESS_RETENTION_MS", "1000")
        manager = ProcessManager(Config())
        await manager.exec_command("echo retained", side_effects=NONE)
        process_id = manager.list_processes(limit=1)["processes"][0]["process_id"]
        mp = manager.get_process(process_id)
        assert mp is not None
        now = time.time()
        mp.info.ended_at = now - 1

        monkeypatch.setattr(time, "time", lambda: now)
        assert manager._reap_terminal_processes() == 0
        assert manager.get_process(process_id) is not None
        monkeypatch.setattr(time, "time", lambda: now + 0.001)
        assert manager._reap_terminal_processes() == 1
        assert manager._reap_terminal_processes() == 0

    @pytest.mark.asyncio
    async def test_cap_removes_oldest_and_never_running(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_RETAINED_PROCESSES", "2")
        manager = ProcessManager(Config())
        completed = []
        for index in range(4):
            await manager.exec_command(f"echo {index}", side_effects=NONE)
            completed.append(manager.list_processes(limit=1)["processes"][0]["process_id"])

        running = await manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE)
        running_id = running["process_id"]
        listed = {item["process_id"] for item in manager.list_processes()["processes"]}
        assert running_id in listed
        assert completed[-2:] == [item for item in completed if item in listed]
        await manager.exec_command("echo trigger", side_effects=NONE)
        assert manager.get_process(running_id) is not None
        await manager.stop_process(running_id, force_after_ms=100)

    @pytest.mark.asyncio
    async def test_manual_cleanup_contract_is_preserved(self):
        manager = ProcessManager(Config())
        await manager.exec_command("echo done", side_effects=NONE)
        result = await manager.cleanup(completed_older_than_ms=0)
        assert result["removed"] == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
    async def test_live_descendant_counts_toward_limit_and_survives_cleanup(
        self, monkeypatch
    ):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "1")
        monkeypatch.setattr("mcp_yieldshell.process.manager.PROCESS_GROUP_EXIT_MS", 100)
        monkeypatch.setattr("mcp_yieldshell.process.manager.FINAL_DRAIN_MS", 100)
        manager = ProcessManager(Config())
        first = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; subprocess.Popen(['sleep','30'])\"",
            yield_ms=0,
            side_effects=NONE,
        )
        await manager.wait_process(first["process_id"], timeout_ms=1000)

        cleanup = await manager.cleanup(completed_older_than_ms=0)
        second = await manager.exec_command("echo rejected", side_effects=NONE)

        assert cleanup["removed"] == 0
        assert second["status"] == "failed_to_start"
        assert "limit" in second["error"].lower()
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_completed_primary_with_live_descendant_reports_running(
        self, monkeypatch
    ):
        monkeypatch.setattr("mcp_yieldshell.process.manager.PROCESS_GROUP_EXIT_MS", 100)
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; subprocess.Popen(['sleep','30'])\"",
            yield_ms=0,
            side_effects=NONE,
        )
        process_id = result["process_id"]
        await manager.wait_process(process_id, timeout_ms=2000)

        listed = manager.list_processes()["processes"][0]
        read_result = await manager.read_output(process_id)

        assert listed["status"] == "running"
        assert read_result["status"] == "running"
        managed = manager.get_process(process_id)
        assert managed.info.status == ProcessStatus.COMPLETED
        primary_ended_at = managed.info.ended_at
        await manager.stop_process(process_id, force_after_ms=100)
        assert managed.info.ended_at is not None
        assert primary_ended_at is not None
        assert managed.info.ended_at > primary_ended_at

    @pytest.mark.asyncio
    async def test_observed_group_exit_is_monotonic(self, monkeypatch):
        manager = ProcessManager(Config())
        await manager.exec_command("echo done", side_effects=NONE)
        process_id = manager.list_processes(limit=1)["processes"][0]["process_id"]
        mp = manager.get_process(process_id)
        assert mp is not None
        assert manager._process_group_exists(mp) is False
        monkeypatch.setattr(os, "killpg", lambda *_: None)
        assert manager._process_group_exists(mp) is False

    @pytest.mark.asyncio
    async def test_concurrent_spawn_reservation_enforces_process_limit(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "1")
        manager = ProcessManager(Config())

        first, second = await asyncio.gather(
            manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE),
            manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE),
        )

        results = (first, second)
        assert sum(result["status"] == "backgrounded" for result in results) == 1
        assert sum(result["status"] == "failed_to_start" for result in results) == 1
        running = next(result for result in results if result["status"] == "backgrounded")
        await manager.stop_process(running["process_id"], force_after_ms=100)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
class TestManagerShutdown:
    @pytest.fixture(autouse=True)
    def short_lifecycle_policy(self, monkeypatch):
        monkeypatch.setattr("mcp_yieldshell.process.manager.GRACEFUL_STOP_MS", 100)
        monkeypatch.setattr("mcp_yieldshell.process.manager.PROCESS_GROUP_EXIT_MS", 500)
        monkeypatch.setattr("mcp_yieldshell.process.manager.FINAL_DRAIN_MS", 500)

    def test_process_group_id_does_not_depend_on_live_parent_lookup(
        self, monkeypatch
    ):
        manager = ProcessManager(Config())
        proc = MagicMock()
        proc.pid = 12345
        getpgid = MagicMock(side_effect=ProcessLookupError)
        monkeypatch.setattr("mcp_yieldshell.process.manager.os.getpgid", getpgid)

        assert manager._get_process_group_id(proc) == 12345
        getpgid.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_with_no_live_processes_is_idempotent(self):
        manager = ProcessManager(Config())
        await manager.shutdown()
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_stops_multiple_live_groups_and_descendant(self):
        manager = ProcessManager(Config())
        first = await manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE)
        second = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess,time; "
            "subprocess.Popen(['sleep','30']); time.sleep(30)\"",
            yield_ms=0,
            side_effects=NONE,
        )
        groups = []
        for result in (first, second):
            mp = manager.get_process(result["process_id"])
            assert mp is not None and mp.process_group_id is not None
            groups.append(mp.process_group_id)

        await manager.shutdown()
        await manager.shutdown()

        for result, group in zip((first, second), groups, strict=True):
            assert manager.get_process(result["process_id"]).info.status == ProcessStatus.STOPPED
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)

    @pytest.mark.asyncio
    async def test_shutdown_force_kills_group_ignoring_termination(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"exec {sys.executable} -c \"import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"",
            yield_ms=0,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None and mp.process_group_id is not None
        group = mp.process_group_id
        await asyncio.sleep(0.05)

        started = time.monotonic()
        await manager.shutdown()
        assert time.monotonic() - started < 2
        with pytest.raises(ProcessLookupError):
            os.killpg(group, signal.SIGCONT)

    @pytest.mark.asyncio
    async def test_shutdown_cleans_descendant_after_primary_is_terminal(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; "
            "subprocess.Popen(['sleep','30'])\"",
            yield_ms=0,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None and mp.process_group_id is not None
        group = mp.process_group_id
        wait_result = await manager.wait_process(result["process_id"], timeout_ms=2000)
        assert mp.info.status == ProcessStatus.COMPLETED
        assert wait_result["status"] == "running"

        await manager.shutdown()

        assert mp.info.status == ProcessStatus.COMPLETED
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)

    @pytest.mark.asyncio
    async def test_stop_force_kills_descendant_after_primary_exits(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"exec {sys.executable} -c \"import subprocess,time; "
            f"subprocess.Popen(['{sys.executable}','-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)']); time.sleep(30)\"",
            yield_ms=0,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None and mp.process_group_id is not None
        group = mp.process_group_id
        await asyncio.sleep(0.1)

        stopped = await manager.stop_process(result["process_id"])

        assert stopped["stopped"] is True
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)

    @pytest.mark.asyncio
    async def test_terminal_parent_with_live_descendant_can_be_stopped(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; subprocess.Popen(['sleep','30'])\"",
            yield_ms=0,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None and mp.process_group_id is not None
        group = mp.process_group_id
        waited = await manager.wait_process(result["process_id"], timeout_ms=2000)
        assert mp.info.status == ProcessStatus.COMPLETED
        assert waited["status"] == "running"
        first_listing = manager.list_processes()["processes"][0]
        assert first_listing["ended_at"] is None
        first_duration = first_listing["duration_ms"]
        await asyncio.sleep(0.05)
        second_listing = manager.list_processes()["processes"][0]
        assert second_listing["duration_ms"] > first_duration

        stopped = await manager.stop_process(result["process_id"])

        assert stopped["stopped"] is True
        final_listing = manager.list_processes()["processes"][0]
        assert final_listing["ended_at"] is not None
        assert final_listing["duration_ms"] >= second_listing["duration_ms"]
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)

    @pytest.mark.asyncio
    async def test_stop_reports_success_during_timeout_terminal_state(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            "sleep 30",
            yield_ms=0,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None
        mp.info.status = ProcessStatus.TIMED_OUT

        stopped = await manager.stop_process(
            result["process_id"],
            force_after_ms=100,
        )

        assert stopped["stopped"] is True
        assert stopped["error"] is None
        assert mp.info.status == ProcessStatus.TIMED_OUT

    @pytest.mark.asyncio
    async def test_runtime_timeout_remains_active_for_live_descendant(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; subprocess.Popen(['sleep','30'])\"",
            yield_ms=0,
            timeout_ms=300,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None and mp.process_group_id is not None
        group = mp.process_group_id

        await asyncio.sleep(1)

        assert mp.info.status == ProcessStatus.TIMED_OUT
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)

    @pytest.mark.asyncio
    async def test_terminal_descendant_receives_full_grace(self, monkeypatch, tmp_path):
        monkeypatch.setattr("mcp_yieldshell.process.manager.GRACEFUL_STOP_MS", 500)
        marker = tmp_path / "graceful"
        child = tmp_path / "child.py"
        child.write_text(
            "import signal, time\n"
            "from pathlib import Path\n"
            f"marker = Path({str(marker)!r})\n"
            "def stop(*_):\n"
            "    time.sleep(0.2)\n"
            "    marker.write_text('done')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; "
            f"subprocess.Popen(['{sys.executable}', {str(child)!r}])\"",
            yield_ms=0,
            side_effects=NONE,
        )
        await manager.wait_process(result["process_id"], timeout_ms=2000)

        stopped = await manager.stop_process(result["process_id"])

        assert stopped["stopped"] is True
        assert marker.read_text(encoding="utf-8") == "done"

    @pytest.mark.asyncio
    async def test_natural_descendant_exit_cancels_timeout_retention(self):
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"{sys.executable} -c \"import subprocess; subprocess.Popen("
            f"['{sys.executable}','-c','import time; time.sleep(2)'])\"",
            yield_ms=0,
            side_effects=NONE,
        )
        mp = manager.get_process(result["process_id"])
        assert mp is not None and mp.timeout_task is not None
        await manager.wait_process(result["process_id"], timeout_ms=2000)

        await asyncio.sleep(2)

        assert mp.timeout_task.cancelled() or mp.timeout_task.done()
        assert mp.group_watch_task is not None and mp.group_watch_task.done()

    @pytest.mark.asyncio
    async def test_stop_allows_natural_graceful_exit(self, monkeypatch):
        monkeypatch.setattr("mcp_yieldshell.process.manager.GRACEFUL_STOP_MS", 1000)
        manager = ProcessManager(Config())
        result = await manager.exec_command(
            f"exec {sys.executable} -c \"import signal,time,sys; "
            "signal.signal(signal.SIGTERM, lambda *_: (time.sleep(0.1), sys.exit(0))); "
            "time.sleep(30)\"",
            yield_ms=0,
            side_effects=NONE,
        )
        await asyncio.sleep(0.05)

        stopped = await manager.stop_process(result["process_id"])

        assert stopped["stopped"] is True
        assert manager.get_process(result["process_id"]).info.status == ProcessStatus.STOPPED


class TestWindowsFallback:
    @pytest.mark.asyncio
    async def test_termination_targets_primary_process(self, monkeypatch):
        process = MagicMock()
        process.pid = 123
        monkeypatch.setattr("mcp_yieldshell.process.spawn.sys.platform", "win32")

        await terminate_process(process, process_group_id=456)
        await kill_process(process, process_group_id=456)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()


class TestServerLifespan:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [None, KeyboardInterrupt(), RuntimeError("server failed")])
    async def test_shutdown_runs_for_all_server_exit_paths(self, monkeypatch, error):
        manager = AsyncMock()
        monkeypatch.setattr("mcp_yieldshell.server._manager", manager)

        if error is None:
            async with _server_lifespan(mcp):
                pass
        else:
            with pytest.raises(type(error)):
                async with _server_lifespan(mcp):
                    raise error

        manager.shutdown.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_shutdown_targets_manager_active_at_lifespan_entry(self, monkeypatch):
        active_manager = AsyncMock()
        replacement_manager = AsyncMock()
        monkeypatch.setattr("mcp_yieldshell.server._manager", active_manager)

        async with _server_lifespan(mcp):
            monkeypatch.setattr(
                "mcp_yieldshell.server._manager", replacement_manager
            )

        active_manager.shutdown.assert_awaited_once_with()
        replacement_manager.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_server_rejects_live_manager_replacement(self, monkeypatch):
        monkeypatch.setattr("mcp_yieldshell.server._manager", None)
        create_server(Config())
        from mcp_yieldshell import server as server_module

        active_manager = server_module._manager
        assert active_manager is not None

        with pytest.raises(RuntimeError, match="already initialized"):
            create_server(Config())

        assert server_module._manager is active_manager
        await active_manager.shutdown()


class TestSpawnRegistration:
    @pytest.mark.asyncio
    async def test_process_is_registered_before_initial_stdin_await(self, monkeypatch):
        entered_drain = asyncio.Event()
        release_drain = asyncio.Event()

        class BlockingStdin:
            def write(self, _: bytes) -> None:
                pass

            async def drain(self) -> None:
                entered_drain.set()
                await release_drain.wait()

        class FakeProcess:
            pid = 987_654_321
            stdout = None
            stderr = None
            stdin = BlockingStdin()
            returncode = None

            async def wait(self):
                await asyncio.Event().wait()

            def terminate(self):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.spawn_process", AsyncMock(return_value=FakeProcess())
        )
        manager = ProcessManager(Config())
        task = asyncio.create_task(
            manager.exec_command("blocked stdin", stdin="data", side_effects=NONE)
        )
        await entered_drain.wait()

        assert len(manager.list_processes()["processes"]) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await manager.shutdown()


class TestInitialStdinLifecycle:
    class FakeProcess:
        pid = 987_654_322
        stdout = None
        stderr = None
        returncode = None

        def __init__(self, stdin):
            self.stdin = stdin
            self._exited = asyncio.Event()

        async def wait(self):
            await self._exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -signal.SIGTERM
            self._exited.set()

        def kill(self):
            self.returncode = -signal.SIGKILL
            self._exited.set()

    @staticmethod
    def configure_fake_process_checks(manager, monkeypatch):
        monkeypatch.setattr(manager, "_get_process_group_id", lambda _proc: None)
        monkeypatch.setattr(
            manager,
            "_process_group_exists",
            lambda mp: mp.proc.returncode is None,
        )

    @pytest.mark.asyncio
    async def test_initial_stdin_backpressure_does_not_delay_auto_yield(
        self, monkeypatch
    ):
        entered_drain = asyncio.Event()
        release_drain = asyncio.Event()

        class BlockingStdin:
            def __init__(self):
                self.closed = False

            def write(self, _: bytes) -> None:
                pass

            async def drain(self) -> None:
                entered_drain.set()
                await release_drain.wait()

            def is_closing(self) -> bool:
                return self.closed

            def close(self) -> None:
                self.closed = True

        proc = self.FakeProcess(BlockingStdin())
        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.spawn_process",
            AsyncMock(return_value=proc),
        )
        manager = ProcessManager(Config())
        self.configure_fake_process_checks(manager, monkeypatch)

        started = time.monotonic()
        result = await manager.exec_command(
            "blocked stdin",
            stdin="data",
            yield_ms=20,
            timeout_ms=0,
            side_effects=NONE,
        )
        elapsed = time.monotonic() - started

        assert result["status"] == "backgrounded"
        assert elapsed < 0.5
        assert entered_drain.is_set()
        mp = manager.get_process(result["process_id"])
        assert mp is not None
        assert mp.stdin_task is not None and not mp.stdin_task.done()

        write_result = await manager.write_input(result["process_id"], "later")
        assert write_result["ok"] is False
        assert "still in progress" in write_result["error"]

        release_drain.set()
        await mp.stdin_task
        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_initial_stdin_failure_is_reported(self, monkeypatch):
        class BrokenStdin:
            def write(self, _: bytes) -> None:
                pass

            async def drain(self) -> None:
                raise BrokenPipeError("stdin delivery failed")

            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                pass

        proc = self.FakeProcess(BrokenStdin())
        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.spawn_process",
            AsyncMock(return_value=proc),
        )
        manager = ProcessManager(Config())
        self.configure_fake_process_checks(manager, monkeypatch)

        result = await manager.exec_command(
            "broken stdin",
            stdin="data",
            yield_ms=20,
            timeout_ms=0,
            side_effects=NONE,
        )

        assert result["status"] == "backgrounded"
        assert result["stdin_error"] == "stdin delivery failed"
        read_result = await manager.read_output(result["process_id"])
        assert read_result["stdin_error"] == "stdin delivery failed"
        await manager.shutdown()


class TestShutdownSpawnRace:
    @pytest.mark.asyncio
    async def test_concurrent_shutdown_and_exec_leave_no_live_managed_work(self):
        for _ in range(25):
            manager = ProcessManager(Config())
            exec_task = asyncio.create_task(
                manager.exec_command("sleep 0.2", yield_ms=0, side_effects=NONE)
            )
            await asyncio.sleep(0)
            await manager.shutdown()
            exec_task.cancel()
            try:
                await exec_task
            except asyncio.CancelledError:
                pass
            assert not any(
                manager._has_live_work(mp) for mp in manager._processes.values()
            )

    @pytest.mark.asyncio
    async def test_exec_rejected_after_shutdown_starts(self):
        manager = ProcessManager(Config())
        await manager.shutdown()
        result = await manager.exec_command("echo late", side_effects=NONE)
        assert result["status"] == "failed_to_start"
        assert "shutting down" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_pending_spawn_and_rejects_late_registration(
        self, monkeypatch
    ):
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()

        class FakeProcess:
            pid = 42
            stdout = None
            stderr = None
            stdin = None
            returncode = None

            async def wait(self):
                await asyncio.Event().wait()

            def terminate(self):
                pass

            def kill(self):
                pass

        async def slow_spawn(*_args, **_kwargs):
            spawn_started.set()
            await release_spawn.wait()
            return FakeProcess()

        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.spawn_process", slow_spawn
        )
        manager = ProcessManager(Config())
        exec_task = asyncio.create_task(
            manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE)
        )
        await spawn_started.wait()
        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0.05)
        release_spawn.set()

        await shutdown_task
        result = await exec_task
        assert result["status"] == "failed_to_start"
        assert "shutting down" in result["error"].lower()
        assert manager.list_processes()["processes"] == []

    @pytest.mark.asyncio
    async def test_shutdown_does_not_wait_indefinitely_for_pending_spawn(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.PENDING_SPAWN_SHUTDOWN_MS", 100
        )
        spawn_started = asyncio.Event()

        async def stuck_spawn(*_args, **_kwargs):
            spawn_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.spawn_process", stuck_spawn
        )
        manager = ProcessManager(Config())
        exec_task = asyncio.create_task(
            manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE)
        )
        await spawn_started.wait()
        started = time.monotonic()
        await manager.shutdown()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        with pytest.raises(asyncio.CancelledError):
            await exec_task
        assert manager._pending_spawns == 0
        assert manager._pending_spawn_tasks == set()
        assert manager._shutdown_complete is True


class TestWaitDeadline:
    @pytest.mark.asyncio
    async def test_wait_uses_its_budget_when_descendants_hold_the_group(
        self, monkeypatch
    ):
        """A latched completion event must not make wait return instantly.

        When the tracked shell exits but descendants keep the process group
        alive, the record still reports ``running``. wait must spend its
        deadline instead of returning immediately, or a polling agent spins.
        """
        if sys.platform == "win32":
            pytest.skip("POSIX process groups only")

        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.PROCESS_GROUP_EXIT_MS", 200
        )
        monkeypatch.setattr("mcp_yieldshell.process.manager.FINAL_DRAIN_MS", 200)
        manager = ProcessManager(Config())
        # The child redirects its own output, so every pipe disconnects and
        # proc.wait() completes while the process group stays alive. This is
        # the ordinary "launch a dev server" shape.
        result = await manager.exec_command(
            "sleep 30 > /dev/null 2>&1 &",
            yield_ms=0,
            side_effects=NONE,
        )
        process_id = result["process_id"]
        mp = manager.get_process(process_id)
        assert mp is not None
        pgid = mp.process_group_id

        try:
            # Let the shell exit and latch completion_event while the
            # detached child keeps the process group alive.
            for _ in range(100):
                if mp.completion_event.is_set():
                    break
                await asyncio.sleep(0.05)
            assert mp.completion_event.is_set()
            assert manager._has_live_work(mp) is True

            started = time.monotonic()
            wait_result = await manager.wait_process(process_id, timeout_ms=1_000)
            elapsed_ms = (time.monotonic() - started) * 1000

            assert wait_result["status"] == "running"
            assert wait_result["wait_result"] == "deadline_reached"
            assert elapsed_ms >= 900
        finally:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_wait_reports_exit_and_effective_cap(self):
        manager = ProcessManager(Config())
        try:
            result = await manager.exec_command(
                "sleep 0.2", yield_ms=0, side_effects=NONE
            )
            process_id = result["process_id"]

            wait_result = await manager.wait_process(
                process_id, timeout_ms=600_000
            )

            assert wait_result["status"] == "completed"
            assert wait_result["wait_result"] == "exited"
            assert wait_result["max_wait_ms"] == MAX_EFFECTIVE_WAIT_MS
            assert wait_result["waited_ms"] < 5_000
        finally:
            await manager.shutdown()


class TestRedactionAcrossTools:
    @pytest.fixture(autouse=True)
    def _enable_redaction(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_REDACT_ENV_REGEX", "SECRET")

    @pytest.mark.asyncio
    async def test_read_and_wait_do_not_redact_unrelated_truncated_suffixes(
        self, monkeypatch
    ):
        monkeypatch.setenv("MY_SECRET", "abcdefghijklmnop")
        manager = ProcessManager(Config())
        secret = "abcdefghijklmnop"
        command = (
            f"{sys.executable} -c \"import sys; sys.stdout.write('{'x' * 40}{secret}'); "
            "sys.stdout.flush()\""
        )
        started = await manager.exec_command(command, yield_ms=0, side_effects=NONE)
        process_id = started["process_id"]
        await manager.wait_process(process_id, timeout_ms=5000)
        read_result = await manager.read_output(
            process_id, max_output_bytes=48, streams="stdout"
        )
        assert "[REDACTED:MY_SECRET]" not in read_result["stdout"]
        assert "abcdefghijklmnop" not in read_result["stdout"]

    @pytest.mark.asyncio
    async def test_incremental_pages_cannot_reassemble_secret(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "abcdefghijklmnop")
        manager = ProcessManager(Config())
        started = await manager.exec_command(
            "printf abcdefghijklmnop",
            yield_ms=0,
            side_effects=NONE,
        )
        process_id = started["process_id"]
        await manager.wait_process(process_id, timeout_ms=2_000)

        cursor = 1
        pages = []
        for _ in range(10):
            page = await manager.read_output(
                process_id,
                since_seq=cursor,
                max_output_bytes=5,
                streams="stdout",
            )
            pages.append(page["stdout"])
            if page["next_seq"] == cursor:
                break
            cursor = page["next_seq"]
            if not page["capped"]:
                break

        combined = "".join(pages)
        assert "abcdefghijklmnop" not in combined
        assert "[REDACTED:MY_SECRET]" in combined
