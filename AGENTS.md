# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11 package using a `src/` layout. Runtime code lives in `src/mcp_yield_shell/`, with the MCP server entry points in `server.py` and `__main__.py`. Process lifecycle logic is grouped under `src/mcp_yield_shell/process/`. Tests live in `tests/` and mirror the main behavior areas, for example `test_config.py`, `test_ring_buffer.py`, and `test_security.py`. Release automation is in `scripts/release.py`, package metadata is in `pyproject.toml`, and CI publishing configuration is in `.github/workflows/publish.yml`.

## Build, Test, and Development Commands

- `uv sync`: install runtime and development dependencies from `pyproject.toml` and `uv.lock`.
- `uv run mcp-yield-shell`: run the MCP server locally.
- `uv run pytest`: run the full test suite configured under `tests/`.
- `uv run ruff check .`: lint imports and Python style issues.
- `uv run pyright`: type-check the `src/` package with basic checking.
- `uv build`: build source and wheel distributions.

## Coding Style & Naming Conventions

Use 4-space indentation and standard Python naming: `snake_case` for functions, variables, and modules; `PascalCase` for classes; uppercase names for constants. Keep modules focused around their current responsibilities rather than adding broad utility files. Ruff is configured for Python 3.11, 99-character lines, import sorting, and `E`, `F`, `I`, and `W` lint families.

## Testing Guidelines

Tests use `pytest` with `pytest-asyncio`; async tests are supported automatically by the project configuration. Name new test files `test_<area>.py` and test functions `test_<expected_behavior>`. Add focused tests near the behavior being changed, especially for process state transitions, timeout behavior, output truncation, cwd policy, and command security rules.

## Commit & Pull Request Guidelines

The existing history uses concise Conventional Commit-style prefixes such as `feat:`, `fix:`, `build:`, and `chore:`. Keep commit subjects imperative and scoped to one change. Pull requests should include a short behavior summary, tests run, linked issues when applicable, and notes for any security, configuration, or release-impacting changes.

## Security & Configuration Tips

This project executes shell commands, so treat policy changes as security-sensitive. Review updates to `YIELD_SHELL_ALLOWED_CWDS`, command allow/deny regexes, environment redaction, process termination, and output retention carefully. Do not commit local secrets or machine-specific MCP client configuration.
