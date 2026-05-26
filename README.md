# Yield Shell MCP

A drop-in shell MCP server that auto-yields long-running commands into managed background processes.

## Why Auto-Yielding?

Most shell tools present a frustrating choice: either block the LLM agent until the command finishes, or force the agent to decide upfront that a command should run in the background.

**Yield Shell MCP** solves this by keeping normal foreground semantics for fast commands, then automatically promoting long-running commands into managed background processes after a brief delay (`yield_ms`, default: 1 second).

```mermaid
graph TD
    A[exec_command] --> B["Wait for yield_ms (default: 1s)"]
    B --> C{Is process still running?}
    C -->|Yes| D["backgrounded<br>Returns process_id"]
    C -->|No| E["completed<br>Returns full output"]
```

- **Fast Commands** (e.g., `echo hello`, `ls`): Complete instantly, returning the output immediately.
- **Long-Running Commands** (e.g., `npm run dev`, `docker build`, `sleep 60`): Automatically yield control back to the agent with a `process_id` and a snapshot of initial output, letting the agent decide when to `read`, `wait`, or `stop` the process.

---

## Installation

### From Registry (Recommended)

To run the published package via `uv`:

```bash
uv tool install mcp-yield-shell
```

### Local Development

To clone and run locally:

```bash
git clone <repo-url> && cd mcp-yield-shell
uv sync
uv run mcp-yield-shell
```

---

## MCP Client Configuration

### Claude Desktop

To configure the server in Claude Desktop, add the configuration below to your Claude Desktop config file:

*   **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
*   **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

#### Production (via uvx)

```json
{
  "mcpServers": {
    "yield-shell": {
      "command": "uvx",
      "args": ["mcp-yield-shell"]
    }
  }
}
```

#### Production with Security Restrictions

```json
{
  "mcpServers": {
    "yield-shell": {
      "command": "uvx",
      "args": ["mcp-yield-shell"],
      "env": {
        "YIELD_SHELL_ALLOWED_CWDS": "/home/user/projects:/tmp/build",
        "YIELD_SHELL_DEFAULT_TIMEOUT_MS": "300000"
      }
    }
  }
}
```

#### Local Development Setup

Replace `/path/to/mcp-yield-shell` with the absolute path to your cloned repository:

```json
{
  "mcpServers": {
    "yield-shell": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/mcp-yield-shell",
        "run",
        "mcp-yield-shell"
      ]
    }
  }
}
```

### Cursor

To configure the server in Cursor:
1. Open **Cursor Settings** -> **Features** -> **MCP**.
2. Click **+ Add New MCP Server**.
3. Fill out the form:
   - **Name**: `yield-shell`
   - **Type**: `stdio`
   - **Command**: `uvx mcp-yield-shell` (or `uv --directory /path/to/mcp-yield-shell run mcp-yield-shell` for local development)

### OpenCode

Add to your OpenCode MCP settings:

```json
{
  "mcpServers": {
    "yield-shell": {
      "command": "uvx",
      "args": ["mcp-yield-shell"]
    }
  }
}
```

---

## Tool Reference

### `exec`
Execute a shell command. If the command runs longer than `yield_ms`, it yields a `process_id` and runs in the background.

*   **Parameters**:
    *   `command` (string, **required**): The command string to execute in the shell.
    *   `cwd` (string, optional): Working directory for the command. Must be under allowed roots if `YIELD_SHELL_ALLOWED_CWDS` is set. Defaults to `YIELD_SHELL_DEFAULT_CWD`.
    *   `env` (object of string to string, optional): Additive environment variable overlay. Merged into the parent environment.
    *   `shell` (string, optional): A custom shell path to execute commands (e.g. `/bin/zsh`). Internally executed using the platform's default shell handler if omitted.
    *   `stdin` (string, optional): Initial text input written to standard input immediately after spawning.
    *   `name` (string, optional): A human-readable label/name to identify this process.
    *   `yield_ms` (integer, optional): Milliseconds to wait before yielding execution to background. Clamped by `YIELD_SHELL_MAX_YIELD_MS`. Defaults to `YIELD_SHELL_DEFAULT_YIELD_MS` (1000ms).
    *   `timeout_ms` (integer, optional): Total execution runtime limit in milliseconds. Process is killed if it runs longer than this. Defaults to `YIELD_SHELL_DEFAULT_TIMEOUT_MS` (0 = no limit).
    *   `max_output_bytes` (integer, optional): Maximum output bytes to capture in stdout/stderr ring buffers. Subject to `YIELD_SHELL_MAX_OUTPUT_BYTES` cap.
    *   `auto_background` (boolean, default: `true`): If `true`, yields after `yield_ms`. If `false`, blocks until the command completes or times out.

*   **Output Statuses**:
    *   `completed`: Process finished within `yield_ms`. Returns exit code, stdout, and stderr.
    *   `backgrounded`: Process auto-yielded. Returns `process_id` and `pid` for tracking.
    *   `timed_out`: Process exceeded `timeout_ms` and was terminated.
    *   `stopped`: Process was explicitly terminated.
    *   `failed_to_start`: Command could not be spawned (e.g., bad directory or policy violation).
    *   `failed`: An internal execution error occurred.

### `read`
Read stdout and/or stderr output from a running or completed background process.

*   **Parameters**:
    *   `process_id` (string, **required**): Unique identifier of the process.
    *   `since_seq` (integer, optional): Return only output appended after this sequence number. Enables efficient incremental log polling.
    *   `max_output_bytes` (integer, optional): Clamps the response size. Defaults to the server cap.
    *   `streams` (string, default: `"both"`): The streams to read. Options: `"both"`, `"stdout"`, or `"stderr"`.

*   **Returns**:
    *   `process_id`, `status`, `exit_code`, `signal`, `next_seq` (sequence index to use in subsequent `since_seq` reads), `stdout`/`stderr` text, and a `truncated` flag.

### `write`
Write text input to the standard input (`stdin`) of a running process.

*   **Parameters**:
    *   `process_id` (string, **required**): Unique identifier of the process.
    *   `input` (string, **required**): Text input to write.
    *   `newline` (boolean, default: `false`): If `true`, appends `\n` to the input.

### `wait`
Block execution until the process exits or the wait timeout expires. This allows the LLM to pause and await completion without spawning a new execution loop.

*   **Parameters**:
    *   `process_id` (string, **required**): Unique identifier of the process.
    *   `timeout_ms` (integer, default: `30000`): Maximum time to wait.
    *   `max_output_bytes` (integer, optional): Maximum output bytes to return in the response.

*   **Important**: If the wait timeout expires, `wait` returns the current status but **does not kill** the process. It continues running in the background.

### `stop`
Gracefully terminate or force kill a running process.

*   **Parameters**:
    *   `process_id` (string, **required**): Unique identifier of the process.
    *   `signal` (string, default: `"SIGTERM"`): OS signal to send (e.g. `SIGTERM`, `SIGKILL`, `SIGINT`). Ignored on Windows.
    *   `force_after_ms` (integer, default: `3000`): Grace period before escalating to force kill (`SIGKILL`).

### `ps`
List all managed processes.

*   **Parameters**:
    *   `include_completed` (boolean, default: `true`): If `false`, finished/stopped processes are excluded from the output.
    *   `limit` (integer, default: `50`): Maximum number of entries.

### `cleanup`
Prune completed or stopped process records to free memory.

*   **Parameters**:
    *   `completed_older_than_ms` (integer, default: `3600000`): Prunes completed processes older than this threshold (1 hour default).
    *   `stopped_older_than_ms` (integer, default: `3600000`): Prunes stopped, timed-out, or failed processes older than this threshold (1 hour default).

---

## Sequence Number & Incremental Reads

To avoid sending duplicate data over the MCP protocol (which can consume context window space), the server implements a sequence-based polling protocol:

1. Every output chunk appended to a process's ring buffer receives a unique, incremental sequence number (`seq`).
2. When calling `exec`, `read`, or `wait`, the response includes a `next_seq` value representing the index of the next chunk to be written.
3. To retrieve only *new* output, call `read` with `since_seq` set to the previously received `next_seq`.
4. Omitting `since_seq` returns the entire contents currently stored in the buffer (clamped by `max_output_bytes`).

---

## Configuration Variables

Configure the server by setting these environment variables prior to launch:

| Environment Variable | Default Value | Description |
|---|---|---|
| `YIELD_SHELL_DEFAULT_CWD` | Current directory | The fallback working directory for commands. |
| `YIELD_SHELL_ALLOWED_CWDS` | *(none)* | A list of allowed directory paths separated by `os.pathsep` (e.g., `:` on UNIX, `;` on Windows). If set, all command execution paths must resolve inside one of these roots. |
| `YIELD_SHELL_MAX_OUTPUT_BYTES` | `20000` | The default and maximum capacity of the ring buffers for stdout/stderr. |
| `YIELD_SHELL_MAX_PROCESSES` | `50` | Maximum concurrent managed processes. Spawning a new command when this limit is reached will return `failed_to_start`. |
| `YIELD_SHELL_DEFAULT_YIELD_MS` | `1000` | Fallback delay before auto-yielding. |
| `YIELD_SHELL_MAX_YIELD_MS` | `30000` | The maximum allowed value for the `yield_ms` parameter. |
| `YIELD_SHELL_DEFAULT_TIMEOUT_MS` | `0` | Default hard runtime limit (0 means no limit). |
| `YIELD_SHELL_DENY_COMMAND_REGEX` | *(none)* | A regular expression pattern. Commands matching this pattern are blocked before starting. |
| `YIELD_SHELL_ALLOW_COMMAND_REGEX` | *(none)* | A regular expression pattern. If set, only commands matching this pattern are permitted. |
| `YIELD_SHELL_REDACT_ENV_REGEX` | `TOKEN\|KEY\|SECRET\|PASSWORD` | Regex to identify sensitive environment variable keys. Their values are redacted in stdout/stderr outputs. |

---

## Security Notes

*   **Arbitrary Code Execution**: This server executes shell commands on the host system. Always run the server inside a container, sandbox, or isolated development VM.
*   **Path Validation**: CWD path verification uses absolute paths (`resolve()`), preventing path-traversal attacks (`../`) outside the allowed roots.
*   **Additive Environments**: The `env` argument overlays existing env parameters. It merges with the parent process environment instead of completely replacing it, protecting critical OS vars.
*   **Best-effort Redaction**: While values of variables matching `YIELD_SHELL_REDACT_ENV_REGEX` are scrubbed from outputs, this is a best-effort system. Sensitive data printed through complex formats or argument lists might not be caught.

---

## Platform Support

*   **POSIX (Linux & macOS)**: Fully supported. Spawns processes in distinct sessions (`start_new_session=True`), allowing signals (`SIGTERM`/`SIGKILL`) to target the entire process group. This ensures child processes started by commands (such as npm dev tasks) are completely cleaned up.
*   **Windows**: Supported with best-effort process group controls. Windows lacks native POSIX signals, meaning `stop` and `timeout_ms` act on the primary process, and child subprocesses might persist if they do not exit cleanly.

---

## License

MIT