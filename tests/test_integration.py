"""Integration tests for execution management and tool behaviors."""

import asyncio
import os
import re
import shutil
import signal
import sys
from unittest.mock import AsyncMock

import pytest

from mcp_yieldshell.config import Config
from mcp_yieldshell.execution.manager import ExecutionManager
from mcp_yieldshell.policy import MAX_EFFECTIVE_WAIT_MS
from mcp_yieldshell.types import ExecutionStatus, SideEffect

NONE = [SideEffect.NONE]


def _assert_addressable_response(
    result: dict, manager: ExecutionManager
) -> str:
    execution_id = result["execution_id"]
    assert isinstance(execution_id, str)
    assert re.fullmatch(r"[0-9a-f]{12}", execution_id)
    assert not execution_id.startswith("proc_")
    assert isinstance(result["process_id"], int)
    assert "pid" not in result
    managed = manager.get_execution(execution_id)
    assert managed is not None
    assert result["process_id"] == managed.proc.pid
    return execution_id


@pytest.fixture
def config():
    return Config()


@pytest.fixture
async def manager(config):
    instance = ExecutionManager(config)
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
    instance = ExecutionManager(short_yield_config)
    try:
        yield instance
    finally:
        await instance.shutdown()


class TestQuickCommand:
    @pytest.mark.asyncio
    async def test_completed_status(self, manager):
        result = await manager.execute_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_exit_code_nonzero(self, manager):
        result = await manager.execute_command("exit 1", side_effects=NONE)
        assert result["status"] == "completed"
        assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_stderr_captured(self, manager):
        result = await manager.execute_command("echo error >&2", side_effects=NONE)
        assert result["status"] == "completed"
        assert "error" in result["stderr"]

    @pytest.mark.asyncio
    async def test_duration_ms_present(self, manager):
        result = await manager.execute_command("echo hello", side_effects=NONE)
        assert "duration_ms" in result
        assert result["duration_ms"] >= 0


class TestExecutionResponseContract:
    @pytest.mark.asyncio
    async def test_completed_response_exposes_both_identifiers(self, manager):
        result = await manager.execute_command("printf done", side_effects=NONE)

        assert result["status"] == "completed"
        _assert_addressable_response(result, manager)

    @pytest.mark.asyncio
    async def test_backgrounded_response_exposes_both_identifiers(self, manager):
        result = await manager.execute_command(
            "sleep 30", yield_ms=0, timeout_ms=0, side_effects=NONE
        )

        assert result["status"] == "backgrounded"
        execution_id = _assert_addressable_response(result, manager)
        assert result["message"] == (
            "Execution is running in the background. Use read, wait, or stop "
            "with execution_id."
        )
        await manager.stop_execution(execution_id, force_after_ms=100)

    @pytest.mark.asyncio
    async def test_timed_out_response_exposes_both_identifiers(self, manager):
        result = await manager.execute_command(
            "sleep 30",
            yield_ms=2_000,
            timeout_ms=20,
            side_effects=NONE,
        )

        assert result["status"] == "timed_out"
        _assert_addressable_response(result, manager)

    @pytest.mark.parametrize(
        ("terminal_status", "expected_status"),
        [
            (ExecutionStatus.STOPPED, "stopped"),
            (ExecutionStatus.FAILED, "failed"),
        ],
    )
    @pytest.mark.asyncio
    async def test_terminal_response_branches_expose_both_identifiers(
        self, manager, monkeypatch, terminal_status, expected_status
    ):
        class FakeProcess:
            pid = 654_321
            stdout = None
            stderr = None
            stdin = None
            returncode = 1

        proc = FakeProcess()

        async def finish_with_status(_proc, managed):
            managed.info.status = terminal_status
            managed.info.exit_code = proc.returncode
            managed.process_group_exited = True
            manager._mark_ended(managed)
            managed.completion_event.set()

        monkeypatch.setattr(
            "mcp_yieldshell.execution.manager.spawn_process",
            AsyncMock(return_value=proc),
        )
        monkeypatch.setattr(manager, "_track_completion", finish_with_status)

        result = await manager.execute_command(
            "synthetic terminal execution",
            timeout_ms=0,
            side_effects=NONE,
        )

        assert result["status"] == expected_status
        _assert_addressable_response(result, manager)

    @pytest.mark.asyncio
    async def test_generated_execution_ids_are_bare_hex_and_unique(self, manager):
        results = [
            await manager.execute_command(f"printf {index}", side_effects=NONE)
            for index in range(8)
        ]

        execution_ids = {
            _assert_addressable_response(result, manager) for result in results
        }
        assert len(execution_ids) == len(results)

    @pytest.mark.asyncio
    async def test_failed_to_start_is_not_addressable(self, manager, monkeypatch):
        monkeypatch.setattr(
            "mcp_yieldshell.execution.manager.spawn_process",
            AsyncMock(side_effect=OSError("spawn unavailable")),
        )

        result = await manager.execute_command("never starts", side_effects=NONE)

        assert result == {
            "status": "failed_to_start",
            "error": "spawn unavailable",
        }
        assert "execution_id" not in result
        assert "process_id" not in result
        assert "pid" not in result


class TestLongCommand:
    @pytest.mark.asyncio
    async def test_six_second_command_completes_inline_with_default_yield(self, manager):
        result = await manager.execute_command("sleep 6 && echo inline", side_effects=NONE)
        assert result["status"] == "completed"
        assert "inline" in result["stdout"]

    @pytest.mark.asyncio
    async def test_backgrounded_status(self, short_yield_manager):
        result = await short_yield_manager.execute_command(
            "sleep 10", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        assert "execution_id" in result
        assert re.fullmatch(r"[0-9a-f]{12}", result["execution_id"])
        # Clean up
        await short_yield_manager.stop_execution(result["execution_id"], force_after_ms=500)

    @pytest.mark.asyncio
    async def test_wait_returns_completed(self, manager):
        # Start a short process that backgrounds
        result = await manager.execute_command(
            "echo hello && sleep 1", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        # Wait for it to complete. Cursorless reads resume, so execute's snapshot
        # and this response together carry the output exactly once.
        wait_result = await manager.wait_execution(execution_id, timeout_ms=5000)
        assert wait_result["status"] in ("completed", "stopped")
        combined = result.get("stdout", "") + wait_result.get("stdout", "")
        assert "hello" in combined
        assert combined.count("hello") == 1

    @pytest.mark.asyncio
    async def test_wait_includes_output_emitted_before_normal_exit(self, manager):
        result = await manager.execute_command(
            "python -c \"import sys,time; "
            "sys.stdout.write(\\\"hello\\\\n\\\"); sys.stdout.flush(); time.sleep(0.2)\"",
            yield_ms=0, side_effects=NONE
        )
        assert result["status"] == "backgrounded"

        wait_result = await manager.wait_execution(result["execution_id"], timeout_ms=5000)

        assert wait_result["status"] == "completed"
        assert "hello" in wait_result.get("stdout", "")

    @pytest.mark.asyncio
    async def test_wait_completes_when_background_child_keeps_pipes_open(self, manager):
        if sys.platform == "win32":
            pytest.skip("POSIX process groups only")

        result = await manager.execute_command(
            "python -c \"import subprocess; subprocess.Popen([\\\"sleep\\\", \\\"30\\\"])\"",
            yield_ms=0, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        mp = manager.get_execution(execution_id)
        assert mp is not None
        pgid = mp.process_group_id

        try:
            wait_result = await manager.wait_execution(execution_id, timeout_ms=5000)

            assert mp.info.status == ExecutionStatus.COMPLETED
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
        monkeypatch.setattr("mcp_yieldshell.execution.manager.GRACEFUL_STOP_MS", 100)
        monkeypatch.setattr("mcp_yieldshell.execution.manager.PROCESS_GROUP_EXIT_MS", 500)

        result = await manager.execute_command(
            "python -c \"import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"",
            yield_ms=0,
            timeout_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        mp = manager.get_execution(execution_id)
        assert mp is not None
        pgid = mp.process_group_id

        try:
            wait_result = await manager.wait_execution(execution_id, timeout_ms=5000)

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
        result = await manager.execute_command(
            "sleep 5", yield_ms=0, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        await manager.stop_execution(result["execution_id"], force_after_ms=500)


class TestIncrementalRead:
    @pytest.mark.asyncio
    async def test_read_since_seq(self, manager):
        result = await manager.execute_command(
            "echo first && sleep 0.2 && echo second", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        await asyncio.sleep(0.5)  # Let both lines emit

        read_result = await manager.read_execution_output(execution_id)
        assert (
            "first" in read_result.get("stdout", "")
            or "second" in read_result.get("stdout", "")
        )

        # Read with since_seq beyond next_seq
        read_result2 = await manager.read_execution_output(execution_id, since_seq=999)
        assert read_result2["stdout"] == ""

        # Clean up
        await manager.stop_execution(execution_id, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_read_streams_filter(self, manager):
        result = await manager.execute_command(
            "echo out && echo err >&2 && sleep 5", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]

        await asyncio.sleep(0.3)
        stdout_only = await manager.read_execution_output(execution_id, streams="stdout")
        assert "stdout" in stdout_only
        assert "stderr" not in stdout_only

        stderr_only = await manager.read_execution_output(execution_id, streams="stderr")
        assert "stderr" in stderr_only
        assert "stdout" not in stderr_only
        await manager.stop_execution(execution_id, force_after_ms=500)


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
        result = await manager.execute_command(
            cmd, yield_ms=200, close_stdin=False, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        await asyncio.sleep(0.2)
        write_result = await manager.write_input(execution_id, "hello", newline=True)
        assert write_result["ok"] is True
        await asyncio.sleep(0.3)
        read_result = await manager.read_execution_output(execution_id, streams="stdout")
        assert "got: hello" in read_result.get("stdout", "")
        assert "ok" in write_result
        await manager.stop_execution(execution_id, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_write_after_initial_stdin(self, manager):
        """An explicitly interactive execute keeps stdin open for follow-up writes."""
        cmd = (
            f"{sys.executable} -c '"
            "import sys\n"
            "for line in sys.stdin:\n"
            "    print(f\"got: {line.strip()}\", flush=True)"
            "'"
        )
        result = await manager.execute_command(
            cmd,
            stdin="first\n",
            close_stdin=False,
            yield_ms=200,
            side_effects=NONE,
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        await asyncio.sleep(0.3)
        # Initial stdin data should appear in output
        read1 = await manager.read_execution_output(execution_id, streams="stdout")
        assert "got: first" in read1.get("stdout", "")
        # Follow-up write must succeed (stdin must still be open)
        write_result = await manager.write_input(execution_id, "second", newline=True)
        assert write_result["ok"] is True
        await asyncio.sleep(0.3)
        read2 = await manager.read_execution_output(
            execution_id,
            since_seq=read1["next_seq"],
            streams="stdout",
        )
        assert "got: second" in read2.get("stdout", "")
        await manager.stop_execution(execution_id, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_initial_stdin_is_closed_by_default(self, manager):
        result = await manager.execute_command(
            "wc -c",
            stdin="hello",
            yield_ms=2_000,
            side_effects=NONE,
        )

        assert result["status"] == "completed"
        assert result["stdout"].strip() == "5"

    @pytest.mark.asyncio
    async def test_write_can_close_stdin(self, manager):
        result = await manager.execute_command(
            "wc -c",
            close_stdin=False,
            yield_ms=100,
            side_effects=NONE,
        )
        assert result["status"] == "backgrounded"

        write_result = await manager.write_input(
            result["execution_id"], "hello", close_stdin=True
        )
        assert write_result["ok"] is True
        completed = await manager.wait_execution(result["execution_id"], timeout_ms=2_000)
        assert completed["status"] == "completed"
        assert completed["stdout"].strip() == "5"

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("nonexistent", "hello")
        assert result["ok"] is False
        assert "Unknown" in result.get("error", "")


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_running_process(self, manager):
        result = await manager.execute_command(
            "sleep 60", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]

        stop_result = await manager.stop_execution(execution_id, force_after_ms=500)
        assert stop_result["stopped"] is True

    @pytest.mark.asyncio
    async def test_stop_with_sigint(self, manager):
        """Test stop with a custom signal (SIGINT) before default SIGTERM."""
        result = await manager.execute_command(
            "sleep 60", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        stop_result = await manager.stop_execution(
            execution_id, signal_name="SIGINT", force_after_ms=500
        )
        assert stop_result["stopped"] is True
        assert stop_result["execution_id"] == execution_id

    @pytest.mark.asyncio
    async def test_invalid_signal_is_rejected_without_stopping_process(self, manager):
        result = await manager.execute_command(
            "sleep 30", yield_ms=0, side_effects=NONE
        )
        execution_id = result["execution_id"]

        stop_result = await manager.stop_execution(
            execution_id, signal_name="NOT_A_SIGNAL", force_after_ms=0
        )

        assert stop_result["stopped"] is False
        assert "Invalid signal" in stop_result["error"]
        assert (await manager.read_execution_output(execution_id))["status"] == "running"
        await manager.stop_execution(execution_id, force_after_ms=100)

    @pytest.mark.asyncio
    async def test_stop_unknown_process(self, manager):
        result = await manager.stop_execution("nonexistent")
        assert result["stopped"] is False
        assert "Unknown" in result.get("error", "")


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_response_is_never_reported_as_backgrounded(self, manager):
        result = await manager.execute_command(
            "sleep 30",
            yield_ms=2_000,
            timeout_ms=50,
            side_effects=NONE,
        )

        assert result["status"] == "timed_out"

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, manager):
        result = await manager.execute_command(
            "sleep 60", yield_ms=500, timeout_ms=500, side_effects=NONE
        )
        # Should get backgrounded first, then timeout kills it
        if result["status"] == "backgrounded":
            execution_id = result["execution_id"]
            await asyncio.sleep(1.0)
            read_result = await manager.read_execution_output(execution_id)
            assert read_result["status"] in ("timed_out", "completed", "stopped")
        elif result["status"] == "timed_out":
            assert "execution_id" in result

    @pytest.mark.asyncio
    async def test_default_timeout_task_and_explicit_unlimited_override(self, manager):
        default = await manager.execute_command("sleep 30", yield_ms=0, side_effects=NONE)
        unlimited = await manager.execute_command(
            "sleep 30", yield_ms=0, timeout_ms=0, side_effects=NONE
        )
        default_mp = manager.get_execution(default["execution_id"])
        unlimited_mp = manager.get_execution(unlimited["execution_id"])
        assert default_mp is not None and default_mp.timeout_task is not None
        assert unlimited_mp is not None and unlimited_mp.timeout_task is None
        await manager.stop_execution(default["execution_id"], force_after_ms=100)
        await manager.stop_execution(unlimited["execution_id"], force_after_ms=100)
        assert default_mp.completion_task is not None
        assert default_mp.completion_task.done()
        assert unlimited_mp.completion_task is not None
        assert unlimited_mp.completion_task.done()
        assert default_mp.proc.returncode is not None
        assert unlimited_mp.proc.returncode is not None


class TestBoundedOutput:
    @pytest.mark.asyncio
    async def test_configured_buffer_cap_is_independent_of_response_cap(
        self, monkeypatch
    ):
        monkeypatch.setenv("YIELDSHELL_MAX_OUTPUT_BYTES", "100")
        monkeypatch.setenv("YIELDSHELL_MAX_BUFFER_BYTES", "10")
        mgr = ExecutionManager(Config())
        try:
            result = await mgr.execute_command(
                "printf 12345678901234567890",
                side_effects=NONE,
            )
            execution_id = mgr.list_executions(limit=1)["executions"][0]["execution_id"]
            mp = mgr.get_execution(execution_id)

            assert result["status"] == "completed"
            assert result["stdout"] == "1234567890"
            assert result["evicted"] is True
            assert mp is not None
            assert mp.stdout_buf.max_bytes == 10
            assert mp.stdout_buf._retained_bytes == 10
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_output_above_cap(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_OUTPUT_BYTES", "100")
        config = Config()
        mgr = ExecutionManager(config)
        result = await mgr.execute_command(
            f"{sys.executable} -c \"print('A' * 500)\"", side_effects=NONE
        )
        assert result["status"] == "completed"
        # Retention is independent of the response cap, so the withheld bytes
        # are still readable rather than lost.
        assert result["capped"] is True
        assert result["evicted"] is False
        assert result["latest_seq"] > result["next_seq"]

    @pytest.mark.asyncio
    async def test_output_within_cap(self, manager):
        result = await manager.execute_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        assert result["capped"] is False
        assert result["evicted"] is False
        assert "hint" not in result

    @pytest.mark.asyncio
    async def test_large_final_output_burst_preserves_tail(self, manager):
        marker = "FINAL-TAIL-MARKER"
        result = await manager.execute_command(
            f"{sys.executable} -c \"import sys; "
            f"sys.stdout.write('A' * 15000 + '{marker}'); sys.stdout.flush()\"",
            side_effects=NONE,
        )
        assert result["status"] == "completed"
        assert result["capped"] is False
        assert result["stdout"].endswith(marker)

    @pytest.mark.asyncio
    async def test_large_final_burst_after_primary_exit_via_wait(self, manager):
        marker = "FINAL-WAIT-TAIL-MARKER"
        started = await manager.execute_command(
            f"{sys.executable} -c \"import sys,time; "
            f"time.sleep(0.05); sys.stdout.write('B' * 15000 + '{marker}'); "
            "sys.stdout.flush()\"",
            yield_ms=0,
            side_effects=NONE,
        )
        wait_result = await manager.wait_execution(
            started["execution_id"], timeout_ms=10_000
        )
        assert wait_result["status"] == "completed"
        assert wait_result["capped"] is False
        assert wait_result["stdout"].endswith(marker)

class TestIncrementalPolling:
    @pytest.mark.asyncio
    async def test_repeated_wait_with_cursor_returns_only_new_output(self, manager):
        started = await manager.execute_command(
            f"{sys.executable} -c \"import sys,time; "
            "[ (sys.stdout.write('line-%d\\n' % i), sys.stdout.flush(), "
            'time.sleep(0.1)) for i in range(10) ]"',
            yield_ms=0,
            side_effects=NONE,
        )
        execution_id = started["execution_id"]
        assert "next_seq" in started

        collected = started["stdout"]
        cursor = started["next_seq"]
        for _ in range(20):
            page = await manager.wait_execution(
                execution_id, timeout_ms=300, since_seq=cursor
            )
            assert page["start_seq"] >= cursor
            collected += page["stdout"]
            cursor = page["next_seq"]
            if page["wait_result"] == "exited":
                break

        assert collected == "".join(f"line-{index}\n" for index in range(10))

    @pytest.mark.asyncio
    async def test_exec_cursor_is_usable_by_read(self, manager):
        # The yield window is shorter than the command, so execute returns the
        # first write and a cursor positioned after it.
        started = await manager.execute_command(
            "printf 'first\\n'; sleep 2; printf 'second\\n'",
            yield_ms=300,
            side_effects=NONE,
        )
        execution_id = started["execution_id"]
        assert started["status"] == "backgrounded"
        assert started["stdout"] == "first\n"

        await manager.wait_execution(execution_id, timeout_ms=5_000)
        remainder = await manager.read_execution_output(
            execution_id, since_seq=started["next_seq"]
        )

        assert remainder["stdout"] == "second\n"
        assert started["stdout"] + remainder["stdout"] == "first\nsecond\n"

    @pytest.mark.asyncio
    async def test_wait_reports_latest_seq_beyond_a_capped_response(self, manager):
        started = await manager.execute_command(
            "seq 1 5000", yield_ms=0, side_effects=NONE
        )
        execution_id = started["execution_id"]

        waited = await manager.wait_execution(
            execution_id, timeout_ms=5_000, max_output_bytes=50
        )

        assert waited["capped"] is True
        assert waited["latest_seq"] > waited["next_seq"]
        assert f"since_seq={waited['next_seq']}" in waited["hint"]


class TestTailMode:
    @pytest.mark.asyncio
    async def test_read_tail_lines_returns_newest_lines(self, manager):
        await manager.execute_command("seq 1 5000", side_effects=NONE)
        execution_id = manager.list_executions(limit=1)["executions"][0]["execution_id"]

        tail = await manager.read_execution_output(execution_id, tail_lines=3)

        assert tail["stdout"] == "4998\n4999\n5000\n"

    @pytest.mark.asyncio
    async def test_wait_tail_lines_returns_newest_lines(self, manager):
        started = await manager.execute_command(
            "seq 1 5000", yield_ms=0, side_effects=NONE
        )

        waited = await manager.wait_execution(
            started["execution_id"], timeout_ms=5_000, tail_lines=2
        )

        assert waited["stdout"] == "4999\n5000\n"

    @pytest.mark.asyncio
    async def test_tail_lines_and_since_seq_are_mutually_exclusive(self, manager):
        started = await manager.execute_command(
            "echo hi", yield_ms=0, side_effects=NONE
        )
        execution_id = manager.list_executions(limit=1)["executions"][0]["execution_id"]
        assert started is not None

        result = await manager.read_execution_output(
            execution_id, since_seq=1, tail_lines=5
        )
        waited = await manager.wait_execution(
            execution_id, timeout_ms=100, since_seq=1, tail_lines=5
        )

        assert "mutually exclusive" in result["error"]
        assert "mutually exclusive" in waited["error"]

    @pytest.mark.asyncio
    async def test_non_positive_tail_lines_is_rejected(self, manager):
        await manager.execute_command("echo hi", side_effects=NONE)
        execution_id = manager.list_executions(limit=1)["executions"][0]["execution_id"]

        result = await manager.read_execution_output(execution_id, tail_lines=0)

        assert "must be positive" in result["error"]


class TestProcessActivity:
    @pytest.mark.asyncio
    async def test_ps_reports_idle_time_and_cursor(self, manager):
        started = await manager.execute_command(
            "printf 'ready\\n'; sleep 5", yield_ms=0, side_effects=NONE
        )
        execution_id = started["execution_id"]

        try:
            for _ in range(50):
                entry = manager.list_executions()["executions"][0]
                if entry["last_output_at"] is not None:
                    break
                await asyncio.sleep(0.05)

            assert entry["status"] == "running"
            assert entry["last_output_at"] is not None
            assert entry["idle_ms"] >= 0
            assert entry["latest_seq"] == len("ready\n") + 1
        finally:
            await manager.stop_execution(execution_id, force_after_ms=200)

    @pytest.mark.asyncio
    async def test_ps_idle_is_none_before_any_output(self, manager):
        started = await manager.execute_command(
            "sleep 5", yield_ms=0, side_effects=NONE
        )
        try:
            entry = manager.list_executions()["executions"][0]
            assert entry["last_output_at"] is None
            assert entry["idle_ms"] is None
            assert entry["latest_seq"] == 1
        finally:
            await manager.stop_execution(started["execution_id"], force_after_ms=200)


class TestSecurityConfig:
    @pytest.mark.asyncio
    async def test_cwd_restriction(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOWED_CWDS", "/tmp")
        config = Config()
        mgr = ExecutionManager(config)
        result = await mgr.execute_command("echo hello", cwd="/etc", side_effects=NONE)
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_command_deny(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DENY_COMMAND_REGEX", r"rm\s")
        config = Config()
        mgr = ExecutionManager(config)
        result = await mgr.execute_command("rm -rf /tmp/test", side_effects=NONE)
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_command_allow(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_ALLOW_COMMAND_REGEX", r"^echo\s")
        config = Config()
        mgr = ExecutionManager(config)
        result = await mgr.execute_command("ls -la", side_effects=NONE)
        assert result["status"] == "failed_to_start"

    @pytest.mark.asyncio
    async def test_env_overlay(self, manager, monkeypatch):
        result = await manager.execute_command(
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
        mgr = ExecutionManager(config)
        cmd = (
            f"{sys.executable} -c "
            "\"import os; print(os.environ.get('MY_SECRET_KEY', ''))\""
        )
        result = await mgr.execute_command(cmd, side_effects=NONE)
        assert result["status"] == "completed"
        assert "supersecret123" not in result["stdout"]
        assert "[REDACTED:" in result["stdout"]

    @pytest.mark.asyncio
    async def test_background_read_and_wait_use_config_snapshot(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_KEY", "snapshot-secret")
        config = Config()
        manager = ExecutionManager(config)
        monkeypatch.setenv("MY_SECRET_KEY", "changed-secret")
        command = (
            f"{sys.executable} -c \"import os,time; "
            "print(os.environ['MY_SECRET_KEY'], flush=True); time.sleep(0.2)\""
        )
        result = await manager.execute_command(command, yield_ms=0, side_effects=NONE)
        execution_id = result["execution_id"]
        await asyncio.sleep(0.1)

        read_result = await manager.read_execution_output(execution_id)
        # since_seq=1 re-reads the same range rather than resuming past it.
        wait_result = await manager.wait_execution(
            execution_id, timeout_ms=1000, since_seq=1
        )

        assert "changed-secret" in read_result["stdout"]
        assert "changed-secret" in wait_result["stdout"]
        assert "snapshot-secret" not in read_result["stdout"]

    @pytest.mark.asyncio
    async def test_sensitive_env_overlay_is_redacted_across_tools(self):
        manager = ExecutionManager(Config())
        secret = "overlay-secret-value"
        command = (
            f"{sys.executable} -c \"import os,time; "
            "print(os.environ['API_TOKEN'], flush=True); time.sleep(0.2)\""
        )
        started = await manager.execute_command(
            command,
            env_overlay={"API_TOKEN": secret},
            yield_ms=0,
            name=secret,
            side_effects=NONE,
        )
        execution_id = started["execution_id"]
        await asyncio.sleep(0.1)

        read_result = await manager.read_execution_output(execution_id)
        # since_seq=1 re-reads the same range; a cursorless wait would resume
        # past what the read above already consumed.
        wait_result = await manager.wait_execution(
            execution_id, timeout_ms=1_000, since_seq=1
        )
        listed = manager.list_executions()["executions"][0]

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
    manager = ExecutionManager(Config())
    command = (
        f"{sys.executable} -c "
        "\"import os; print(os.environ.get('MY_SECRET_KEY', ''))\""
    )

    result = await manager.execute_command(command, side_effects=NONE)

    assert result["status"] == "completed"
    assert "supersecret123" in result["stdout"]



class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_executions(self, manager):
        # Start and complete a process
        await manager.execute_command("echo done", side_effects=NONE)
        # Now cleanup with threshold 0 (immediate)
        result = await manager.cleanup(completed_older_than_ms=0, stopped_older_than_ms=0)
        assert result["removed"] >= 1

    @pytest.mark.asyncio
    async def test_cleanup_does_not_remove_running(self, manager):
        result = await manager.execute_command(
            "sleep 30", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        cleanup_result = await manager.cleanup(
            completed_older_than_ms=0, stopped_older_than_ms=0
        )
        assert cleanup_result["removed"] == 0
        await manager.stop_execution(execution_id, force_after_ms=500)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("completed_older_than_ms", "stopped_older_than_ms"),
        [(-1, 0), (0, -1)],
    )
    async def test_cleanup_rejects_negative_thresholds_without_removing_records(
        self,
        manager,
        completed_older_than_ms,
        stopped_older_than_ms,
    ):
        await manager.execute_command("echo retained", side_effects=NONE)

        result = await manager.cleanup(
            completed_older_than_ms=completed_older_than_ms,
            stopped_older_than_ms=stopped_older_than_ms,
        )

        assert result["removed"] == 0
        assert "non-negative" in result["error"]
        assert len(manager.list_executions()["executions"]) == 1


class TestPs:
    @pytest.mark.asyncio
    async def test_ps_returns_metadata(self, manager):
        started = await manager.execute_command(
            "echo test", name="testproc", side_effects=NONE
        )
        result = manager.list_executions()
        assert "executions" in result
        assert "processes" not in result
        procs = result["executions"]
        assert len(procs) >= 1
        proc = procs[0]
        assert proc["execution_id"] == started["execution_id"]
        assert isinstance(proc["execution_id"], str)
        assert proc["process_id"] == started["process_id"]
        assert isinstance(proc["process_id"], int)
        assert "pid" not in proc
        assert "name" in proc
        assert "command" in proc
        assert "cwd" in proc
        assert "status" in proc
        assert "started_at" in proc
        assert "stdout_bytes" in proc
        assert "stderr_bytes" in proc

    @pytest.mark.asyncio
    async def test_ps_exclude_completed(self, manager):
        await manager.execute_command("echo done", side_effects=NONE)
        result = manager.list_executions(include_completed=False)
        assert len(result["executions"]) == 0

    @pytest.mark.asyncio
    async def test_ps_limit(self, manager):
        for i in range(5):
            await manager.execute_command(f"echo test{i}", side_effects=NONE)
        result = manager.list_executions(limit=3)
        assert len(result["executions"]) <= 3

    @pytest.mark.asyncio
    async def test_ps_negative_limit_returns_no_executions(self, manager):
        await manager.execute_command("echo test", side_effects=NONE)
        assert manager.list_executions(limit=-1)["executions"] == []


class TestProcessLimit:
    @pytest.mark.asyncio
    async def test_max_processes_rejects(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "2")
        config = Config()
        mgr = ExecutionManager(config)

        # Start two long-running processes
        r1 = await mgr.execute_command("sleep 30", yield_ms=0, side_effects=NONE)
        r2 = await mgr.execute_command("sleep 30", yield_ms=0, side_effects=NONE)

        # Third should be rejected
        r3 = await mgr.execute_command("echo nope", yield_ms=0, side_effects=NONE)
        if r1["status"] == "backgrounded" and r2["status"] == "backgrounded":
            assert r3["status"] == "failed_to_start"
            assert "limit" in r3["error"].lower()

        # Clean up
        for r in [r1, r2]:
            if "execution_id" in r:
                await mgr.stop_execution(r["execution_id"], force_after_ms=500)


class TestWriteErrors:
    @pytest.mark.asyncio
    async def test_write_to_completed_process(self, manager):
        result = await manager.execute_command("echo done", side_effects=NONE)
        assert result["status"] == "completed"
        # Find the process in ps
        ps_result = manager.list_executions()
        if ps_result["executions"]:
            execution_id = ps_result["executions"][0]["execution_id"]
            write_result = await manager.write_input(execution_id, "hello")
            assert write_result["ok"] is False

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("nonexistent", "hello")
        assert result["ok"] is False


class TestStopResponseShape:
    @pytest.mark.asyncio
    async def test_stop_success_includes_error_field(self, manager):
        result = await manager.execute_command(
            "sleep 60", yield_ms=100, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        stop_result = await manager.stop_execution(execution_id, force_after_ms=500)
        assert "error" in stop_result
        assert stop_result["stopped"] is True

    @pytest.mark.asyncio
    async def test_stop_unknown_includes_error_field(self, manager):
        result = await manager.stop_execution("nonexistent")
        assert "error" in result


class TestReadStreamValidation:
    @pytest.mark.asyncio
    async def test_read_invalid_streams(self, manager):
        result = await manager.execute_command("echo hello", side_effects=NONE)
        execution_id = result.get("execution_id")
        if execution_id is None:
            ps_result = manager.list_executions(limit=1)
            execution_id = ps_result["executions"][0]["execution_id"]
        read_result = await manager.read_execution_output(execution_id, streams="invalid")
        assert "error" in read_result



class TestUnknownProcessIds:
    @pytest.mark.asyncio
    async def test_read_unknown_process(self, manager):
        result = await manager.read_execution_output("nonexistent")
        assert result["execution_id"] == "nonexistent"
        assert result["error"] == "Unknown execution_id: nonexistent"

    @pytest.mark.asyncio
    async def test_wait_unknown_process(self, manager):
        result = await manager.wait_execution("nonexistent")
        assert result["execution_id"] == "nonexistent"
        assert result["error"] == "Unknown execution_id: nonexistent"

    @pytest.mark.asyncio
    async def test_stop_unknown_process(self, manager):
        result = await manager.stop_execution("nonexistent")
        assert result["execution_id"] == "nonexistent"
        assert result["error"] == "Unknown execution_id: nonexistent"
        assert result["stopped"] is False

    @pytest.mark.asyncio
    async def test_write_unknown_process(self, manager):
        result = await manager.write_input("nonexistent", "hello")
        assert result["execution_id"] == "nonexistent"
        assert result["ok"] is False
        assert result["error"] == "Unknown execution_id: nonexistent"


class TestWaitCapBehavior:
    @pytest.mark.asyncio
    async def test_wait_large_timeout_returns_running(self, manager):
        result = await manager.execute_command(
            "sleep 60", yield_ms=0, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        try:
            wait_result = await manager.wait_execution(execution_id, timeout_ms=500)
            assert wait_result["status"] == "running"
            assert wait_result["exit_code"] is None
            assert "next_seq" in wait_result
        finally:
            await manager.stop_execution(execution_id, force_after_ms=500)

    @pytest.mark.asyncio
    async def test_wait_already_completed_returns_immediately(self, manager):
        result = await manager.execute_command("echo hello", side_effects=NONE)
        assert result["status"] == "completed"
        ps_result = manager.list_executions(limit=1)
        execution_id = ps_result["executions"][0]["execution_id"]
        # execute already consumed the output; since_seq=1 inspects it again.
        wait_result = await manager.wait_execution(execution_id, timeout_ms=5000, since_seq=1)
        assert wait_result["status"] == "completed"
        assert "hello" in wait_result.get("stdout", "")

    @pytest.mark.asyncio
    async def test_completed_truncated_execute_returns_execution_id(self, manager):
        result = await manager.execute_command(
            "seq 1 20000",
            max_output_bytes=100,
            side_effects=NONE,
        )

        assert result["status"] == "completed"
        assert result["capped"] is True
        assert re.fullmatch(r"[0-9a-f]{12}", result["execution_id"])
        assert f"since_seq={result['next_seq']}" in result["hint"]
        assert "execution_id" in result["hint"]
        assert "process_id" not in result["hint"]
        retained = await manager.read_execution_output(result["execution_id"])
        assert retained["status"] == "completed"

    def test_yield_clamp_boundaries(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_DEFAULT_YIELD_MS", "120000")
        monkeypatch.setenv("YIELDSHELL_MAX_YIELD_MS", "120000")
        manager = ExecutionManager(Config())
        assert manager._clamp_yield_ms(None) == MAX_EFFECTIVE_WAIT_MS
        assert manager._clamp_yield_ms(120000) == MAX_EFFECTIVE_WAIT_MS
        assert manager._clamp_yield_ms(-1) == 0

        monkeypatch.setenv("YIELDSHELL_MAX_YIELD_MS", "1234")
        manager = ExecutionManager(Config())
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
        result = await manager.execute_command(
            "sleep 60", yield_ms=0, timeout_ms=500, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        await asyncio.sleep(1.0)
        read_result = await manager.read_execution_output(execution_id)
        assert read_result["status"] in ("timed_out", "completed", "stopped")

    @pytest.mark.asyncio
    async def test_wait_sees_timed_out(self, manager):
        result = await manager.execute_command(
            "sleep 60", yield_ms=0, timeout_ms=500, side_effects=NONE
        )
        assert result["status"] == "backgrounded"
        execution_id = result["execution_id"]
        wait_result = await manager.wait_execution(execution_id, timeout_ms=5000)
        assert wait_result["status"] in ("timed_out", "completed", "stopped")


class TestIncrementalReadSinceSeq:
    @pytest.mark.asyncio
    async def test_incremental_read_since_seq(self, manager):
        result = await manager.execute_command(
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
        execution_id = result["execution_id"]
        await asyncio.sleep(1.0)

        # execute's inline snapshot already consumed the first line, so read from
        # the head explicitly to see it again.
        first_read = await manager.read_execution_output(execution_id, since_seq=1)
        assert "first" in first_read.get("stdout", "")
        next_seq = first_read["next_seq"]

        incremental_read = await manager.read_execution_output(execution_id, since_seq=next_seq)
        if "stdout" in incremental_read:
            assert "first" not in incremental_read["stdout"]

        await manager.stop_execution(execution_id, force_after_ms=500)


class TestRingBufferByteCount:
    def test_byte_count_tracks_total_written(self):
        from mcp_yieldshell.execution.ring_buffer import RingBuffer

        buf = RingBuffer(10)
        buf.append(b"0123456789")
        assert buf.byte_count == 10
        buf.append(b"ABCDE")
        assert buf.byte_count == 15

    def test_byte_count_tracks_total_after_eviction(self):
        from mcp_yieldshell.execution.ring_buffer import RingBuffer

        buf = RingBuffer(10)
        buf.append(b"0123456789")
        buf.append(b"ABCDE")
        assert buf.byte_count == 15
        assert buf._retained_bytes <= 10

    def test_clear_resets_retained_but_not_total(self):
        from mcp_yieldshell.execution.ring_buffer import RingBuffer

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

        result = await manager.execute_command(
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
        manager = ExecutionManager(Config())

        result = await manager.execute_command(
            "echo allowed-command",
            shell=bash,
            side_effects=NONE,
        )

        assert result["status"] == "failed_to_start"
        assert "Shell rejected by policy" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_requested_shell_is_rejected(self, manager):
        result = await manager.execute_command(
            "echo should-not-run",
            shell="   ",
            side_effects=NONE,
        )

        assert result["status"] == "failed_to_start"
        assert "must not be empty" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_cursor_resumes_after_capped_snapshot(self, manager):
        result = await manager.execute_command(
            "printf 0123456789; sleep 0.1",
            yield_ms=0,
            side_effects=NONE,
        )
        execution_id = result["execution_id"]

        # Anchored at the head so the assertion does not depend on whether
        # execute's inline snapshot already consumed the output.
        waited = await manager.wait_execution(
            execution_id, timeout_ms=2_000, max_output_bytes=4, since_seq=1
        )
        remainder = await manager.read_execution_output(
            execution_id,
            since_seq=waited["next_seq"],
            max_output_bytes=100,
            streams="stdout",
        )

        assert waited["stdout"] == "0123"
        assert waited["stdout"] + remainder["stdout"] == "0123456789"

    @pytest.mark.asyncio
    async def test_cursorless_reads_resume_where_the_last_one_stopped(self, manager):
        result = await manager.execute_command(
            "printf 0123456789abcdefghij",
            yield_ms=0,
            side_effects=NONE,
        )
        execution_id = result["execution_id"]
        mp = manager.get_execution(execution_id)
        assert mp is not None
        # since_seq=1 is out-of-band, so waiting for exit leaves the resume
        # cursor alone. Pin it so the assertions do not depend on whether
        # execute's inline snapshot already consumed part of the output.
        await manager.wait_execution(execution_id, timeout_ms=2_000, since_seq=1)
        mp.read_cursor = 1

        pages = []
        for _ in range(6):
            page = await manager.read_execution_output(execution_id, max_output_bytes=4)
            if not page["stdout"]:
                break
            pages.append(page["stdout"])

        assert pages == ["0123", "4567", "89ab", "cdef", "ghij"]

    @pytest.mark.asyncio
    async def test_out_of_band_reads_do_not_move_the_resume_cursor(self, manager):
        result = await manager.execute_command(
            "printf 0123456789",
            yield_ms=0,
            side_effects=NONE,
        )
        execution_id = result["execution_id"]
        mp = manager.get_execution(execution_id)
        assert mp is not None
        # since_seq=1 is out-of-band, so waiting for exit leaves the resume
        # cursor alone. Pin it so the polling below starts from a known point.
        await manager.wait_execution(execution_id, timeout_ms=2_000, since_seq=1)
        mp.read_cursor = 1

        first = await manager.read_execution_output(execution_id, max_output_bytes=4)
        cursor_after_poll = mp.read_cursor

        # None of these are part of the polling stream.
        await manager.read_execution_output(execution_id, since_seq=1)
        await manager.read_execution_output(execution_id, tail_lines=1)
        await manager.read_execution_output(execution_id, streams="stdout")
        assert mp.read_cursor == cursor_after_poll

        resumed = await manager.read_execution_output(execution_id, max_output_bytes=4)

        assert first["stdout"] == "0123"
        assert resumed["stdout"] == "4567"

    @pytest.mark.asyncio
    async def test_capped_incremental_read_returns_all_output(self, manager):
        result = await manager.execute_command(
            "printf 0123456789abcdef",
            yield_ms=0,
            side_effects=NONE,
        )
        execution_id = result["execution_id"]
        await manager.wait_execution(execution_id, timeout_ms=2_000)

        cursor = 1
        output = ""
        for _ in range(4):
            page = await manager.read_execution_output(
                execution_id,
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
            "mcp_yieldshell.execution.manager.PROCESS_GROUP_EXIT_MS", 10
        )
        monkeypatch.setattr("mcp_yieldshell.execution.manager.FINAL_DRAIN_MS", 10)
        command = (
            f"{sys.executable} -c 'import os,time; "
            "pid=os.fork(); time.sleep(30) if pid == 0 else None; os._exit(0)'"
        )
        result = await manager.execute_command(
            command,
            close_stdin=False,
            yield_ms=100,
            side_effects=NONE,
        )
        execution_id = result["execution_id"]
        await asyncio.sleep(0.1)

        read_result = await manager.read_execution_output(execution_id)
        write_result = await manager.write_input(execution_id, "x")
        running_only = manager.list_executions(include_completed=False)["executions"]

        assert read_result["status"] == "running"
        assert write_result["ok"] is True
        assert any(item["execution_id"] == execution_id for item in running_only)
        await manager.stop_execution(execution_id, force_after_ms=100)

    @pytest.mark.asyncio
    async def test_max_processes_allows_sequential(self, monkeypatch):
        monkeypatch.setenv("YIELDSHELL_MAX_PROCESSES", "2")
        config = Config()
        mgr = ExecutionManager(config)

        # Run two commands that complete
        await mgr.execute_command("echo hello", side_effects=NONE)
        await mgr.execute_command("echo world", side_effects=NONE)

        # Third should succeed as completed ones don't count against limit
        r3 = await mgr.execute_command("echo test", side_effects=NONE)
        assert r3["status"] == "completed"
        assert "test" in r3["stdout"]

    @pytest.mark.asyncio
    async def test_timeout_task_cancelled_on_natural_completion(self, manager):
        result = await manager.execute_command(
            "echo test", timeout_ms=60000, side_effects=NONE
        )
        assert result["status"] == "completed"

        # Find the process and check that its timeout task is cancelled
        ps_result = manager.list_executions(limit=1)
        execution_id = ps_result["executions"][0]["execution_id"]
        mp = manager.get_execution(execution_id)
        assert mp is not None
        assert mp.timeout_task is not None
        assert mp.timeout_task.cancelled() or mp.timeout_task.done()
