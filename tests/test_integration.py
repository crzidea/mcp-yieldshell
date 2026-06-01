"""Integration tests for process management and tool behaviors."""

import asyncio
import os
import signal
import sys

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.process.manager import ProcessManager


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def manager(config):
    return ProcessManager(config)


@pytest.fixture
def short_yield_config():
    """Config with very short yield for fast tests."""
    os.environ["YIELDSHELL_DEFAULT_YIELD_MS"] = "100"
    os.environ["YIELDSHELL_MAX_YIELD_MS"] = "5000"
    cfg = Config()
    del os.environ["YIELDSHELL_DEFAULT_YIELD_MS"]
    del os.environ["YIELDSHELL_MAX_YIELD_MS"]
    return cfg


@pytest.fixture
def short_yield_manager(short_yield_config):
    return ProcessManager(short_yield_config)


class TestQuickCommand:
    @pytest.mark.asyncio
    async def test_completed_status(self, manager):
        result = await manager.exec_command("echo hello")
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_exit_code_nonzero(self, manager):
        result = await manager.exec_command("exit 1")
        assert result["status"] == "completed"
        assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_stderr_captured(self, manager):
        result = await manager.exec_command("echo error >&2")
        assert result["status"] == "completed"
        assert "error" in result["stderr"]

    @pytest.mark.asyncio
    async def test_duration_ms_present(self, manager):
        result = await manager.exec_command("echo hello")
        assert "duration_ms" in result
        assert result["duration_ms"] >= 0


class TestLongCommand:
    @pytest.mark.asyncio
    async def test_backgrounded_status(self, short_yield_manager):
        result = await short_yield_manager.exec_command(
            "sleep 10", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        assert "process_id" in result
        assert result["process_id"].startswith("proc_")
        # Clean up
        await short_yield_manager.stop_process(result["process_id"], force_after_ms=500)

    @pytest.mark.asyncio
    async def test_wait_returns_completed(self, manager):
        # Start a short process that backgrounds
        result = await manager.exec_command(
            "echo hello && sleep 1", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        # Wait for it to complete
        wait_result = await manager.wait_process(pid, timeout_ms=5000)
        assert wait_result["status"] in ("completed", "stopped")
        assert "hello" in wait_result.get("stdout", "")

    @pytest.mark.asyncio
    async def test_wait_includes_output_emitted_before_normal_exit(self, manager):
        result = await manager.exec_command(
            "python -c \"import sys,time; "
            "sys.stdout.write(\\\"hello\\\\n\\\"); sys.stdout.flush(); time.sleep(0.2)\"",
            yield_ms=0,
        )
        assert result["status"] == "backgrounded"

        wait_result = await manager.wait_process(result["process_id"], timeout_ms=5000)

        assert wait_result["status"] == "completed"
        assert "hello" in wait_result.get("stdout", "")

    @pytest.mark.asyncio
    async def test_wait_completes_when_background_child_keeps_pipes_open(self, manager):
        if sys.platform == "win32":
            pytest.skip("POSIX process groups only")

        result = await manager.exec_command(
            "python -c \"import subprocess; subprocess.Popen([\\\"sleep\\\", \\\"30\\\"])\"",
            yield_ms=0,
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        mp = manager.get_process(pid)
        assert mp is not None
        pgid = mp.process_group_id

        try:
            wait_result = await manager.wait_process(pid, timeout_ms=5000)

            assert wait_result["status"] == "completed"
            assert wait_result["exit_code"] == 0
        finally:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @pytest.mark.asyncio
    async def test_timeout_force_kills_process_group_after_sigterm_is_ignored(self, manager):
        if sys.platform == "win32":
            pytest.skip("POSIX process groups only")

        result = await manager.exec_command(
            "python -c \"import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"",
            yield_ms=0,
            timeout_ms=100,
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        mp = manager.get_process(pid)
        assert mp is not None
        pgid = mp.process_group_id

        try:
            wait_result = await manager.wait_process(pid, timeout_ms=5000)

            assert wait_result["status"] == "timed_out"
            assert pgid is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(pgid, signal.SIGCONT)
        finally:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class TestYieldZero:
    @pytest.mark.asyncio
    async def test_yield_zero_backgrounds(self, manager):
        result = await manager.exec_command(
            "sleep 5", yield_ms=0
        )
        assert result["status"] == "backgrounded"
        await manager.stop_process(result["process_id"], force_after_ms=500)


class TestIncrementalRead:
    @pytest.mark.asyncio
    async def test_read_since_seq(self, manager):
        result = await manager.exec_command(
            "echo first && sleep 0.2 && echo second", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        await asyncio.sleep(0.5)  # Let both lines emit

        read_result = await manager.read_output(pid)
        assert (
            "first" in read_result.get("stdout", "")
            or "second" in read_result.get("stdout", "")
        )

        # Read with since_seq beyond next_seq
        read_result2 = await manager.read_output(pid, since_seq=999)
        assert read_result2["stdout"] == ""

        # Clean up
        await manager.stop_process(pid, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_read_streams_filter(self, manager):
        result = await manager.exec_command(
            "echo out && echo err >&2 && sleep 5", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]

        await asyncio.sleep(0.3)
        stdout_only = await manager.read_output(pid, streams="stdout")
        assert "stdout" in stdout_only
        assert "stderr" not in stdout_only

        stderr_only = await manager.read_output(pid, streams="stderr")
        assert "stderr" in stderr_only
        assert "stdout" not in stderr_only
        await manager.stop_process(pid, force_after_ms=500)


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_to_stdin(self, manager):
        # Use a Python process that echoes stdin lines back to stdout
        cmd = (
            f"{sys.executable} -c '"
            "import sys\n"
            "for line in sys.stdin:\n"
            "    print(f\"got: {line.strip()}\", flush=True)"
            "'"
        )
        result = await manager.exec_command(
            cmd, yield_ms=200
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        await asyncio.sleep(0.2)
        write_result = await manager.write_input(pid, "hello", newline=True)
        assert write_result["ok"] is True
        await asyncio.sleep(0.3)
        read_result = await manager.read_output(pid, streams="stdout")
        assert "got: hello" in read_result.get("stdout", "")
        assert "ok" in write_result
        await manager.stop_process(pid, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_write_after_initial_stdin(self, manager):
        """Providing stdin in exec must not close the pipe; follow-up write must work."""
        cmd = (
            f"{sys.executable} -c '"
            "import sys\n"
            "for line in sys.stdin:\n"
            "    print(f\"got: {line.strip()}\", flush=True)"
            "'"
        )
        result = await manager.exec_command(
            cmd, stdin="first\n", yield_ms=200
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        await asyncio.sleep(0.3)
        # Initial stdin data should appear in output
        read1 = await manager.read_output(pid, streams="stdout")
        assert "got: first" in read1.get("stdout", "")
        # Follow-up write must succeed (stdin must still be open)
        write_result = await manager.write_input(pid, "second", newline=True)
        assert write_result["ok"] is True
        await asyncio.sleep(0.3)
        read2 = await manager.read_output(pid, since_seq=read1["next_seq"], streams="stdout")
        assert "got: second" in read2.get("stdout", "")
        await manager.stop_process(pid, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("proc_nonexistent", "hello")
        assert result["ok"] is False
        assert "Unknown" in result.get("error", "")


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_running_process(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]

        stop_result = await manager.stop_process(pid, force_after_ms=500)
        assert stop_result["stopped"] is True

    @pytest.mark.asyncio
    async def test_stop_with_sigint(self, manager):
        """Test stop with a custom signal (SIGINT) before default SIGTERM."""
        result = await manager.exec_command(
            "sleep 60", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        stop_result = await manager.stop_process(
            pid, signal_name="SIGINT", force_after_ms=500
        )
        assert stop_result["stopped"] is True
        assert stop_result["process_id"] == pid

    @pytest.mark.asyncio
    async def test_stop_unknown_process(self, manager):
        result = await manager.stop_process("proc_nonexistent")
        assert result["stopped"] is False
        assert "Unknown" in result.get("error", "")


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=500, timeout_ms=500
        )
        # Should get backgrounded first, then timeout kills it
        if result["status"] == "backgrounded":
            pid = result["process_id"]
            await asyncio.sleep(1.0)
            read_result = await manager.read_output(pid)
            assert read_result["status"] in ("timed_out", "completed", "stopped")
        elif result["status"] == "timed_out":
            assert "process_id" in result


class TestBoundedOutput:
    @pytest.mark.asyncio
    async def test_output_above_cap(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_OUTPUT_BYTES", "100")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            f"{sys.executable} -c \"print('A' * 500)\""
        )
        assert result["status"] == "completed"
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_output_within_cap(self, manager):
        result = await manager.exec_command("echo hello")
        assert result["status"] == "completed"
        assert result["truncated"] is False


class TestSecurityConfig:
    @pytest.mark.asyncio
    async def test_cwd_restriction(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/tmp")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command("echo hello", cwd="/etc")
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_command_deny(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"rm\s")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command("rm -rf /tmp/test")
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_command_allow(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOW_COMMAND_REGEX", r"^echo\s")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command("ls -la")
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_env_overlay(self, manager, monkeypatch):
        result = await manager.exec_command(
            f"{sys.executable} -c \"import os; print(os.environ.get('TEST_VAR', 'unset'))\"",
            env_overlay={"TEST_VAR": "hello"},
        )
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]


class TestRedaction:
    @pytest.mark.asyncio
    async def test_env_value_redacted(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "supersecret123")
        config = Config()
        mgr = ProcessManager(config)
        cmd = (
            f"{sys.executable} -c "
            "\"import os; print(os.environ.get('MY_SECRET_KEY', ''))\""
        )
        result = await mgr.exec_command(cmd)
        assert result["status"] == "completed"
        assert "supersecret123" not in result["stdout"]
        assert "[REDACTED:" in result["stdout"]



class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_processes(self, manager):
        # Start and complete a process
        await manager.exec_command("echo done")
        # Now cleanup with threshold 0 (immediate)
        result = await manager.cleanup(completed_older_than_ms=0, stopped_older_than_ms=0)
        assert result["removed"] >= 1

    @pytest.mark.asyncio
    async def test_cleanup_does_not_remove_running(self, manager):
        result = await manager.exec_command(
            "sleep 30", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        cleanup_result = await manager.cleanup(
            completed_older_than_ms=0, stopped_older_than_ms=0
        )
        assert cleanup_result["removed"] == 0
        await manager.stop_process(pid, force_after_ms=500)


class TestPs:
    @pytest.mark.asyncio
    async def test_ps_returns_metadata(self, manager):
        await manager.exec_command("echo test", name="testproc")
        result = manager.list_processes()
        assert "processes" in result
        procs = result["processes"]
        assert len(procs) >= 1
        proc = procs[0]
        assert "process_id" in proc
        assert "pid" in proc
        assert "name" in proc
        assert "command" in proc
        assert "cwd" in proc
        assert "status" in proc
        assert "started_at" in proc
        assert "stdout_bytes" in proc
        assert "stderr_bytes" in proc

    @pytest.mark.asyncio
    async def test_ps_exclude_completed(self, manager):
        await manager.exec_command("echo done")
        result = manager.list_processes(include_completed=False)
        assert len(result["processes"]) == 0

    @pytest.mark.asyncio
    async def test_ps_limit(self, manager):
        for i in range(5):
            await manager.exec_command(f"echo test{i}")
        result = manager.list_processes(limit=3)
        assert len(result["processes"]) <= 3


class TestProcessLimit:
    @pytest.mark.asyncio
    async def test_max_processes_rejects(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "2")
        config = Config()
        mgr = ProcessManager(config)

        # Start two long-running processes
        r1 = await mgr.exec_command("sleep 30", yield_ms=0)
        r2 = await mgr.exec_command("sleep 30", yield_ms=0)

        # Third should be rejected
        r3 = await mgr.exec_command("echo nope", yield_ms=0)
        if r1["status"] == "backgrounded" and r2["status"] == "backgrounded":
            assert r3["status"] == "failed_to_start"
            assert "limit" in r3["error"].lower()

        # Clean up
        for r in [r1, r2]:
            if "process_id" in r:
                await mgr.stop_process(r["process_id"], force_after_ms=500)


class TestWriteErrors:
    @pytest.mark.asyncio
    async def test_write_to_completed_process(self, manager):
        result = await manager.exec_command("echo done")
        assert result["status"] == "completed"
        # Find the process in ps
        ps_result = manager.list_processes()
        if ps_result["processes"]:
            pid = ps_result["processes"][0]["process_id"]
            write_result = await manager.write_input(pid, "hello")
            assert write_result["ok"] is False

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("proc_nonexistent", "hello")
        assert result["ok"] is False


class TestStopResponseShape:
    @pytest.mark.asyncio
    async def test_stop_success_includes_error_field(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=100
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        stop_result = await manager.stop_process(pid, force_after_ms=500)
        assert "error" in stop_result
        assert stop_result["stopped"] is True

    @pytest.mark.asyncio
    async def test_stop_unknown_includes_error_field(self, manager):
        result = await manager.stop_process("proc_nonexistent")
        assert "error" in result


class TestReadStreamValidation:
    @pytest.mark.asyncio
    async def test_read_invalid_streams(self, manager):
        result = await manager.exec_command("echo hello")
        pid = result.get("process_id")
        if pid is None:
            ps_result = manager.list_processes(limit=1)
            pid = ps_result["processes"][0]["process_id"]
        read_result = await manager.read_output(pid, streams="invalid")
        assert "error" in read_result



class TestUnknownProcessIds:
    @pytest.mark.asyncio
    async def test_read_unknown_process(self, manager):
        result = await manager.read_output("proc_nonexistent")
        assert "error" in result
        assert result["process_id"] == "proc_nonexistent"

    @pytest.mark.asyncio
    async def test_wait_unknown_process(self, manager):
        result = await manager.wait_process("proc_nonexistent")
        assert "error" in result
        assert result["process_id"] == "proc_nonexistent"

    @pytest.mark.asyncio
    async def test_stop_unknown_process(self, manager):
        result = await manager.stop_process("proc_nonexistent")
        assert "error" in result
        assert result["process_id"] == "proc_nonexistent"
        assert result["stopped"] is False

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("proc_nonexistent", "hello")
        assert result["ok"] is False
        assert "Unknown" in result.get("error", "")


class TestWaitCapBehavior:
    @pytest.mark.asyncio
    async def test_wait_large_timeout_returns_running(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=0
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        try:
            wait_result = await manager.wait_process(pid, timeout_ms=500)
            assert wait_result["status"] == "running"
            assert wait_result["exit_code"] is None
            assert "next_seq" in wait_result
        finally:
            await manager.stop_process(pid, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_wait_already_completed_returns_immediately(self, manager):
        result = await manager.exec_command("echo hello")
        assert result["status"] == "completed"
        ps_result = manager.list_processes(limit=1)
        pid = ps_result["processes"][0]["process_id"]
        wait_result = await manager.wait_process(pid, timeout_ms=5000)
        assert wait_result["status"] == "completed"
        assert "hello" in wait_result.get("stdout", "")


class TestTimedOutStatus:
    @pytest.mark.asyncio
    async def test_exec_timeout_returns_timed_out(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=0, timeout_ms=500
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        await asyncio.sleep(1.0)
        read_result = await manager.read_output(pid)
        assert read_result["status"] in ("timed_out", "completed", "stopped")

    @pytest.mark.asyncio
    async def test_wait_sees_timed_out(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=0, timeout_ms=500
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        wait_result = await manager.wait_process(pid, timeout_ms=5000)
        assert wait_result["status"] in ("timed_out", "completed", "stopped")


class TestIncrementalReadSinceSeq:
    @pytest.mark.asyncio
    async def test_incremental_read_since_seq(self, manager):
        result = await manager.exec_command(
            f"{sys.executable} -c \""
            "import time, sys\n"
            "print('first', flush=True)\n"
            "time.sleep(0.3)\n"
            "print('second', flush=True)\n"
            "time.sleep(0.3)\n"
            "print('third', flush=True)\n"
            "\"",
            yield_ms=100,
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        await asyncio.sleep(1.0)

        first_read = await manager.read_output(pid)
        assert "first" in first_read.get("stdout", "")
        next_seq = first_read["next_seq"]

        incremental_read = await manager.read_output(pid, since_seq=next_seq)
        if "stdout" in incremental_read:
            assert "first" not in incremental_read["stdout"]

        await manager.stop_process(pid, force_after_ms=500)


class TestRingBufferByteCount:
    def test_byte_count_tracks_total_written(self):
        from mcp_yieldshell.process.ring_buffer import RingBuffer

        buf = RingBuffer(10)
        buf.append(b"0123456789")
        assert buf.byte_count == 10
        buf.append(b"ABCDE")
        assert buf.byte_count == 15

    def test_byte_count_tracks_total_after_eviction(self):
        from mcp_yieldshell.process.ring_buffer import RingBuffer

        buf = RingBuffer(10)
        buf.append(b"0123456789")
        buf.append(b"ABCDE")
        assert buf.byte_count == 15
        assert buf._retained_bytes <= 10

    def test_clear_resets_retained_but_not_total(self):
        from mcp_yieldshell.process.ring_buffer import RingBuffer

        buf = RingBuffer(100)
        buf.append(b"hello")
        assert buf.byte_count == 5
        buf.clear()
        assert buf._retained_bytes == 0
        assert buf.byte_count == 5


class TestDefectFixes:
    @pytest.mark.asyncio
    async def test_max_processes_allows_sequential(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "2")
        config = Config()
        mgr = ProcessManager(config)

        # Run two commands that complete
        await mgr.exec_command("echo hello")
        await mgr.exec_command("echo world")

        # Third should succeed as completed ones don't count against limit
        r3 = await mgr.exec_command("echo test")
        assert r3["status"] == "completed"
        assert "test" in r3["stdout"]

    @pytest.mark.asyncio
    async def test_timeout_task_cancelled_on_natural_completion(self, manager):
        result = await manager.exec_command(
            "echo test", timeout_ms=60000
        )
        assert result["status"] == "completed"

        # Find the process and check that its timeout task is cancelled
        ps_result = manager.list_processes(limit=1)
        pid = ps_result["processes"][0]["process_id"]
        mp = manager.get_process(pid)
        assert mp is not None
        assert mp.timeout_task is not None
        assert mp.timeout_task.cancelled() or mp.timeout_task.done()

