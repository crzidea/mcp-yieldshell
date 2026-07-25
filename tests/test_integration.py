"""Integration tests for process management and tool behaviors."""

import asyncio
import os
import shutil
import signal
import sys

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.policy import MAX_EFFECTIVE_WAIT_MS
from mcp_yieldshell.process.manager import ProcessManager
from mcp_yieldshell.types import ProcessStatus, SideEffect

NONE = [SideEffect.NONE]


@pytest.fixture
def config():
    return Config()


@pytest.fixture
async def manager(config):
    instance = ProcessManager(config)
    try:
        yield instance
    finally:
        await instance.shutdown()


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
async def short_yield_manager(short_yield_config):
    instance = ProcessManager(short_yield_config)
    try:
        yield instance
    finally:
        await instance.shutdown()


class TestQuickCommand:
    @pytest.mark.asyncio
    async def test_completed_status(self, manager):
        result = await manager.exec_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_exit_code_nonzero(self, manager):
        result = await manager.exec_command("exit 1", side_effects=NONE)
        assert result["status"] == "completed"
        assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_stderr_captured(self, manager):
        result = await manager.exec_command("echo error >&2", side_effects=NONE)
        assert result["status"] == "completed"
        assert "error" in result["stderr"]

    @pytest.mark.asyncio
    async def test_duration_ms_present(self, manager):
        result = await manager.exec_command("echo hello", side_effects=NONE)
        assert "duration_ms" in result
        assert result["duration_ms"] >= 0


class TestLongCommand:
    @pytest.mark.asyncio
    async def test_six_second_command_completes_inline_with_default_yield(self, manager):
        result = await manager.exec_command("sleep 6 && echo inline", side_effects=NONE)
        assert result["status"] == "completed"
        assert "inline" in result["stdout"]

    @pytest.mark.asyncio
    async def test_backgrounded_status(self, short_yield_manager):
        result = await short_yield_manager.exec_command(
            "sleep 10", yield_ms=100, side_effects=NONE
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
            "echo hello && sleep 1", yield_ms=100, side_effects=NONE
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
            yield_ms=0, side_effects=NONE
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
            yield_ms=0, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        mp = manager.get_process(pid)
        assert mp is not None
        pgid = mp.process_group_id

        try:
            wait_result = await manager.wait_process(pid, timeout_ms=5000)

            assert mp.info.status == ProcessStatus.COMPLETED
            assert wait_result["status"] == "running"
            assert wait_result["exit_code"] == 0
        finally:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @pytest.mark.asyncio
    async def test_timeout_force_kills_process_group_after_sigterm_is_ignored(
        self, manager, monkeypatch
    ):
        if sys.platform == "win32":
            pytest.skip("POSIX process groups only")
        monkeypatch.setattr("mcp_yieldshell.process.manager.GRACEFUL_STOP_MS", 100)
        monkeypatch.setattr("mcp_yieldshell.process.manager.PROCESS_GROUP_EXIT_MS", 500)

        result = await manager.exec_command(
            "python -c \"import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"",
            yield_ms=0,
            timeout_ms=100, side_effects=NONE
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
            "sleep 5", yield_ms=0, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        await manager.stop_process(result["process_id"], force_after_ms=500)


class TestIncrementalRead:
    @pytest.mark.asyncio
    async def test_read_since_seq(self, manager):
        result = await manager.exec_command(
            "echo first && sleep 0.2 && echo second", yield_ms=100, side_effects=NONE
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
            "echo out && echo err >&2 && sleep 5", yield_ms=100, side_effects=NONE
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
            cmd, yield_ms=200, close_stdin=False, side_effects=NONE
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
        """An explicitly interactive exec keeps stdin open for follow-up writes."""
        cmd = (
            f"{sys.executable} -c '"
            "import sys\n"
            "for line in sys.stdin:\n"
            "    print(f\"got: {line.strip()}\", flush=True)"
            "'"
        )
        result = await manager.exec_command(
            cmd,
            stdin="first\n",
            close_stdin=False,
            yield_ms=200,
            side_effects=NONE,
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
    async def test_initial_stdin_is_closed_by_default(self, manager):
        result = await manager.exec_command(
            "wc -c",
            stdin="hello",
            yield_ms=2_000,
            side_effects=NONE,
        )

        assert result["status"] == "completed"
        assert result["stdout"].strip() == "5"

    @pytest.mark.asyncio
    async def test_write_can_close_stdin(self, manager):
        result = await manager.exec_command(
            "wc -c",
            close_stdin=False,
            yield_ms=100,
            side_effects=NONE,
        )
        assert result["status"] == "backgrounded"

        write_result = await manager.write_input(
            result["process_id"], "hello", close_stdin=True
        )
        assert write_result["ok"] is True
        completed = await manager.wait_process(result["process_id"], timeout_ms=2_000)
        assert completed["status"] == "completed"
        assert completed["stdout"].strip() == "5"

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("proc_nonexistent", "hello")
        assert result["ok"] is False
        assert "Unknown" in result.get("error", "")


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_running_process(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]

        stop_result = await manager.stop_process(pid, force_after_ms=500)
        assert stop_result["stopped"] is True

    @pytest.mark.asyncio
    async def test_stop_with_sigint(self, manager):
        """Test stop with a custom signal (SIGINT) before default SIGTERM."""
        result = await manager.exec_command(
            "sleep 60", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        stop_result = await manager.stop_process(
            pid, signal_name="SIGINT", force_after_ms=500
        )
        assert stop_result["stopped"] is True
        assert stop_result["process_id"] == pid

    @pytest.mark.asyncio
    async def test_invalid_signal_is_rejected_without_stopping_process(self, manager):
        result = await manager.exec_command(
            "sleep 30", yield_ms=0, side_effects=NONE
        )
        process_id = result["process_id"]

        stop_result = await manager.stop_process(
            process_id, signal_name="NOT_A_SIGNAL", force_after_ms=0
        )

        assert stop_result["stopped"] is False
        assert "Invalid signal" in stop_result["error"]
        assert (await manager.read_output(process_id))["status"] == "running"
        await manager.stop_process(process_id, force_after_ms=100)

    @pytest.mark.asyncio
    async def test_stop_unknown_process(self, manager):
        result = await manager.stop_process("proc_nonexistent")
        assert result["stopped"] is False
        assert "Unknown" in result.get("error", "")


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_response_is_never_reported_as_backgrounded(self, manager):
        result = await manager.exec_command(
            "sleep 30",
            yield_ms=2_000,
            timeout_ms=50,
            side_effects=NONE,
        )

        assert result["status"] == "timed_out"

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=500, timeout_ms=500, side_effects=NONE
        )
        # Should get backgrounded first, then timeout kills it
        if result["status"] == "backgrounded":
            pid = result["process_id"]
            await asyncio.sleep(1.0)
            read_result = await manager.read_output(pid)
            assert read_result["status"] in ("timed_out", "completed", "stopped")
        elif result["status"] == "timed_out":
            assert "process_id" in result

    @pytest.mark.asyncio
    async def test_default_timeout_task_and_explicit_unlimited_override(self, manager):
        default = await manager.exec_command("sleep 30", yield_ms=0, side_effects=NONE)
        unlimited = await manager.exec_command(
            "sleep 30", yield_ms=0, timeout_ms=0, side_effects=NONE
        )
        default_mp = manager.get_process(default["process_id"])
        unlimited_mp = manager.get_process(unlimited["process_id"])
        assert default_mp is not None and default_mp.timeout_task is not None
        assert unlimited_mp is not None and unlimited_mp.timeout_task is None
        await manager.stop_process(default["process_id"], force_after_ms=100)
        await manager.stop_process(unlimited["process_id"], force_after_ms=100)
        assert default_mp.completion_task is not None
        assert default_mp.completion_task.done()
        assert unlimited_mp.completion_task is not None
        assert unlimited_mp.completion_task.done()
        assert default_mp.proc.returncode is not None
        assert unlimited_mp.proc.returncode is not None


class TestBoundedOutput:
    @pytest.mark.asyncio
    async def test_output_above_cap(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_OUTPUT_BYTES", "100")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command(
            f"{sys.executable} -c \"print('A' * 500)\"", side_effects=NONE
        )
        assert result["status"] == "completed"
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_output_within_cap(self, manager):
        result = await manager.exec_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_large_final_output_burst_preserves_tail(self, manager):
        marker = "FINAL-TAIL-MARKER"
        result = await manager.exec_command(
            f"{sys.executable} -c \"import sys; "
            f"sys.stdout.write('A' * 15000 + '{marker}'); sys.stdout.flush()\"",
            side_effects=NONE,
        )
        assert result["status"] == "completed"
        assert result["truncated"] is False
        assert result["stdout"].endswith(marker)

    @pytest.mark.asyncio
    async def test_large_final_burst_after_primary_exit_via_wait(self, manager):
        marker = "FINAL-WAIT-TAIL-MARKER"
        started = await manager.exec_command(
            f"{sys.executable} -c \"import sys,time; "
            f"time.sleep(0.05); sys.stdout.write('B' * 15000 + '{marker}'); "
            "sys.stdout.flush()\"",
            yield_ms=0,
            side_effects=NONE,
        )
        wait_result = await manager.wait_process(
            started["process_id"], timeout_ms=10_000
        )
        assert wait_result["status"] == "completed"
        assert wait_result["truncated"] is False
        assert wait_result["stdout"].endswith(marker)

class TestSecurityConfig:
    @pytest.mark.asyncio
    async def test_cwd_restriction(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/tmp")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command("echo hello", cwd="/etc", side_effects=NONE)
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_command_deny(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"rm\s")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command("rm -rf /tmp/test", side_effects=NONE)
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_command_allow(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOW_COMMAND_REGEX", r"^echo\s")
        config = Config()
        mgr = ProcessManager(config)
        result = await mgr.exec_command("ls -la", side_effects=NONE)
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_env_overlay(self, manager, monkeypatch):
        result = await manager.exec_command(
            f"{sys.executable} -c \"import os; print(os.environ.get('TEST_VAR', 'unset'))\"",
            env_overlay={"TEST_VAR": "hello"}, side_effects=NONE
        )
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]


class TestRedaction:
    @pytest.fixture(autouse=True)
    def _enable_redaction(self, monkeypatch):
        monkeypatch.setenv(
            "YIELDSHELL_REDACT_ENV_REGEX", r"TOKEN|KEY|SECRET|PASSWORD"
        )

    @pytest.mark.asyncio
    async def test_env_value_redacted(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "supersecret123")
        config = Config()
        mgr = ProcessManager(config)
        cmd = (
            f"{sys.executable} -c "
            "\"import os; print(os.environ.get('MY_SECRET_KEY', ''))\""
        )
        result = await mgr.exec_command(cmd, side_effects=NONE)
        assert result["status"] == "completed"
        assert "supersecret123" not in result["stdout"]
        assert "[REDACTED:" in result["stdout"]

    @pytest.mark.asyncio
    async def test_background_read_and_wait_use_config_snapshot(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "snapshot-secret")
        config = Config()
        manager = ProcessManager(config)
        monkeypatch.setenv("MY_SECRET_KEY", "changed-secret")
        command = (
            f"{sys.executable} -c \"import os,time; "
            "print(os.environ['MY_SECRET_KEY'], flush=True); time.sleep(0.2)\""
        )
        result = await manager.exec_command(command, yield_ms=0, side_effects=NONE)
        process_id = result["process_id"]
        await asyncio.sleep(0.1)

        read_result = await manager.read_output(process_id)
        wait_result = await manager.wait_process(process_id, timeout_ms=1000)

        assert "changed-secret" in read_result["stdout"]
        assert "changed-secret" in wait_result["stdout"]
        assert "snapshot-secret" not in read_result["stdout"]

    @pytest.mark.asyncio
    async def test_sensitive_env_overlay_is_redacted_across_tools(self):
        manager = ProcessManager(Config())
        secret = "overlay-secret-value"
        command = (
            f"{sys.executable} -c \"import os,time; "
            "print(os.environ['API_TOKEN'], flush=True); time.sleep(0.2)\""
        )
        started = await manager.exec_command(
            command,
            env_overlay={"API_TOKEN": secret},
            yield_ms=0,
            name=secret,
            side_effects=NONE,
        )
        process_id = started["process_id"]
        await asyncio.sleep(0.1)

        read_result = await manager.read_output(process_id)
        wait_result = await manager.wait_process(process_id, timeout_ms=1_000)
        listed = manager.list_processes()["processes"][0]

        for value in (
            started["stdout"],
            read_result["stdout"],
            wait_result["stdout"],
            listed["name"],
        ):
            assert secret not in value
        assert "[REDACTED:API_TOKEN]" in read_result["stdout"]
        assert "[REDACTED:API_TOKEN]" in wait_result["stdout"]
        assert listed["name"] == "[REDACTED:API_TOKEN]"


@pytest.mark.asyncio
async def test_environment_output_is_not_redacted_by_default(monkeypatch):
    monkeypatch.setenv("MY_SECRET_KEY", "supersecret123")
    manager = ProcessManager(Config())
    command = (
        f"{sys.executable} -c "
        "\"import os; print(os.environ.get('MY_SECRET_KEY', ''))\""
    )

    result = await manager.exec_command(command, side_effects=NONE)

    assert result["status"] == "completed"
    assert "supersecret123" in result["stdout"]



class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_processes(self, manager):
        # Start and complete a process
        await manager.exec_command("echo done", side_effects=NONE)
        # Now cleanup with threshold 0 (immediate)
        result = await manager.cleanup(completed_older_than_ms=0, stopped_older_than_ms=0)
        assert result["removed"] >= 1

    @pytest.mark.asyncio
    async def test_cleanup_does_not_remove_running(self, manager):
        result = await manager.exec_command(
            "sleep 30", yield_ms=100, side_effects=NONE
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
        await manager.exec_command("echo test", name="testproc", side_effects=NONE)
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
        await manager.exec_command("echo done", side_effects=NONE)
        result = manager.list_processes(include_completed=False)
        assert len(result["processes"]) == 0

    @pytest.mark.asyncio
    async def test_ps_limit(self, manager):
        for i in range(5):
            await manager.exec_command(f"echo test{i}", side_effects=NONE)
        result = manager.list_processes(limit=3)
        assert len(result["processes"]) <= 3

    @pytest.mark.asyncio
    async def test_ps_negative_limit_returns_no_processes(self, manager):
        await manager.exec_command("echo test", side_effects=NONE)
        assert manager.list_processes(limit=-1)["processes"] == []


class TestProcessLimit:
    @pytest.mark.asyncio
    async def test_max_processes_rejects(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "2")
        config = Config()
        mgr = ProcessManager(config)

        # Start two long-running processes
        r1 = await mgr.exec_command("sleep 30", yield_ms=0, side_effects=NONE)
        r2 = await mgr.exec_command("sleep 30", yield_ms=0, side_effects=NONE)

        # Third should be rejected
        r3 = await mgr.exec_command("echo nope", yield_ms=0, side_effects=NONE)
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
        result = await manager.exec_command("echo done", side_effects=NONE)
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
            "sleep 60", yield_ms=100, side_effects=NONE
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
        result = await manager.exec_command("echo hello", side_effects=NONE)
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
            "sleep 60", yield_ms=0, side_effects=NONE
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
        result = await manager.exec_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        ps_result = manager.list_processes(limit=1)
        pid = ps_result["processes"][0]["process_id"]
        wait_result = await manager.wait_process(pid, timeout_ms=5000)
        assert wait_result["status"] == "completed"
        assert "hello" in wait_result.get("stdout", "")

    @pytest.mark.asyncio
    async def test_completed_truncated_exec_returns_process_id(self, manager):
        result = await manager.exec_command(
            "seq 1 20000",
            max_output_bytes=100,
            side_effects=NONE,
        )

        assert result["status"] == "completed"
        assert result["truncated"] is True
        assert result["process_id"].startswith("proc_")
        retained = await manager.read_output(result["process_id"])
        assert retained["status"] == "completed"

    def test_yield_clamp_boundaries(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DEFAULT_YIELD_MS", "120000")
        monkeypatch.setenv("YIELDSHELL_MAX_YIELD_MS", "120000")
        manager = ProcessManager(Config())
        assert manager._clamp_yield_ms(None) == MAX_EFFECTIVE_WAIT_MS
        assert manager._clamp_yield_ms(120000) == MAX_EFFECTIVE_WAIT_MS
        assert manager._clamp_yield_ms(-1) == 0

        monkeypatch.setenv("YIELDSHELL_MAX_YIELD_MS", "1234")
        manager = ProcessManager(Config())
        assert manager._clamp_yield_ms(None) == 1234
        assert manager._clamp_yield_ms(5000) == 1234

    def test_stop_grace_reserves_time_for_cleanup(self, manager):
        reserved = (
            MAX_EFFECTIVE_WAIT_MS - manager._clamp_stop_grace_ms(MAX_EFFECTIVE_WAIT_MS)
        )

        assert reserved > 0
        assert manager._clamp_stop_grace_ms(-1) == 0

    def test_runtime_default_and_explicit_zero_selection(self, manager):
        assert manager._clamp_timeout_ms(None) == 3600000
        assert manager._clamp_timeout_ms(0) == 0
        assert manager._clamp_timeout_ms(250) == 250


class TestTimedOutStatus:
    @pytest.mark.asyncio
    async def test_exec_timeout_returns_timed_out(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=0, timeout_ms=500, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        pid = result["process_id"]
        await asyncio.sleep(1.0)
        read_result = await manager.read_output(pid)
        assert read_result["status"] in ("timed_out", "completed", "stopped")

    @pytest.mark.asyncio
    async def test_wait_sees_timed_out(self, manager):
        result = await manager.exec_command(
            "sleep 60", yield_ms=0, timeout_ms=500, side_effects=NONE
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
            yield_ms=100, side_effects=NONE
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
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX custom shell")
    async def test_exec_uses_requested_shell(self, manager):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is not installed")

        result = await manager.exec_command(
            'printf "%s" "$BASH_VERSION"',
            shell=bash,
            side_effects=NONE,
        )

        assert result["status"] == "completed"
        assert result["stdout"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX custom shell")
    async def test_requested_shell_cannot_bypass_command_policy(
        self, monkeypatch
    ):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is not installed")
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", "bash")
        manager = ProcessManager(Config())

        result = await manager.exec_command(
            "echo allowed-command",
            shell=bash,
            side_effects=NONE,
        )

        assert result["status"] == "failed_to_start"
        assert "Shell rejected by policy" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_requested_shell_is_rejected(self, manager):
        result = await manager.exec_command(
            "echo should-not-run",
            shell="   ",
            side_effects=NONE,
        )

        assert result["status"] == "failed_to_start"
        assert "must not be empty" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_cursor_resumes_after_capped_snapshot(self, manager):
        result = await manager.exec_command(
            "printf 0123456789; sleep 0.1",
            yield_ms=0,
            side_effects=NONE,
        )
        process_id = result["process_id"]

        waited = await manager.wait_process(
            process_id, timeout_ms=2_000, max_output_bytes=4
        )
        remainder = await manager.read_output(
            process_id,
            since_seq=waited["next_seq"],
            max_output_bytes=100,
            streams="stdout",
        )

        assert waited["stdout"] + remainder["stdout"] == "0123456789"

    @pytest.mark.asyncio
    async def test_capped_incremental_read_returns_all_output(self, manager):
        result = await manager.exec_command(
            "printf 0123456789abcdef",
            yield_ms=0,
            side_effects=NONE,
        )
        process_id = result["process_id"]
        await manager.wait_process(process_id, timeout_ms=2_000)

        cursor = 1
        output = ""
        for _ in range(4):
            page = await manager.read_output(
                process_id,
                since_seq=cursor,
                max_output_bytes=5,
                streams="stdout",
            )
            output += page["stdout"]
            cursor = page["next_seq"]

        assert output == "0123456789abcdef"

    @pytest.mark.asyncio
    async def test_descendant_status_and_write_use_same_running_state(
        self, manager, monkeypatch
    ):
        monkeypatch.setattr(
            "mcp_yieldshell.process.manager.PROCESS_GROUP_EXIT_MS", 10
        )
        monkeypatch.setattr("mcp_yieldshell.process.manager.FINAL_DRAIN_MS", 10)
        command = (
            f"{sys.executable} -c 'import os,time; "
            "pid=os.fork(); time.sleep(30) if pid == 0 else None; os._exit(0)'"
        )
        result = await manager.exec_command(
            command,
            close_stdin=False,
            yield_ms=100,
            side_effects=NONE,
        )
        process_id = result["process_id"]
        await asyncio.sleep(0.1)

        read_result = await manager.read_output(process_id)
        write_result = await manager.write_input(process_id, "x")
        running_only = manager.list_processes(include_completed=False)["processes"]

        assert read_result["status"] == "running"
        assert write_result["ok"] is True
        assert any(item["process_id"] == process_id for item in running_only)
        await manager.stop_process(process_id, force_after_ms=100)

    @pytest.mark.asyncio
    async def test_max_processes_allows_sequential(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "2")
        config = Config()
        mgr = ProcessManager(config)

        # Run two commands that complete
        await mgr.exec_command("echo hello", side_effects=NONE)
        await mgr.exec_command("echo world", side_effects=NONE)

        # Third should succeed as completed ones don't count against limit
        r3 = await mgr.exec_command("echo test", side_effects=NONE)
        assert r3["status"] == "completed"
        assert "test" in r3["stdout"]

    @pytest.mark.asyncio
    async def test_timeout_task_cancelled_on_natural_completion(self, manager):
        result = await manager.exec_command(
            "echo test", timeout_ms=60000, side_effects=NONE
        )
        assert result["status"] == "completed"

        # Find the process and check that its timeout task is cancelled
        ps_result = manager.list_processes(limit=1)
        pid = ps_result["processes"][0]["process_id"]
        mp = manager.get_process(pid)
        assert mp is not None
        assert mp.timeout_task is not None
        assert mp.timeout_task.cancelled() or mp.timeout_task.done()
