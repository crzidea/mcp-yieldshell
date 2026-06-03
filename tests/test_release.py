"""Isolated tests for ``scripts/release.py``.

These tests deliberately avoid real commits, tags, pushes, or lockfile churn
against the main repository. They exercise the release script inside a
throwaway temporary directory, a fake ``uv`` shim, and a mocked git
environment so that no destructive operations escape the test sandbox.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "release.py"


def _load_release_module():
    """Import ``scripts.release`` as a fresh module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release_module():
    return _load_release_module()


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create an isolated git repo with a ``pyproject.toml`` and ``uv.lock``."""

    repo = tmp_path / "repo"
    repo.mkdir()

    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        '[project]\n'
        'name = "mcp-yieldshell"\n'
        'version = "0.3.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["mcp>=1.9.0,<2"]\n'
        "\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
    )
    lock = repo / "uv.lock"
    lock.write_text(
        'version = 1\n'
        'requires-python = ">=3.11"\n'
        "\n"
        "[[package]]\n"
        'name = "mcp-yieldshell"\n'
        'version = "0.3.0"\n'
        'source = { editable = "." }\n'
    )

    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "pyproject.toml", "uv.lock"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Replace ``uv`` with a fake binary in PATH that records invocations.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log_path = shim_dir / "uv.log"
    uv_shim = shim_dir / "uv"
    uv_shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"log = {str(log_path)!r}\n"
        "with open(log, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "# Touch the lockfile so git diffs match a real refresh.\n"
        "with open('uv.lock', 'a') as f:\n"
        "    f.write('# touched by shim\\n')\n"
        "sys.exit(0)\n"
    )
    uv_shim.chmod(0o755)

    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    return {
        "repo": repo,
        "pyproject": pyproject,
        "lock": lock,
        "shim_dir": shim_dir,
        "uv_log": log_path,
    }


class TestReleaseLockfileRefresh:
    def test_refresh_lockfile_invokes_uv_lock(self, release_module, fake_repo, monkeypatch):
        monkeypatch.chdir(fake_repo["repo"])
        release_module.run_cmd = lambda cmd, check=True: subprocess.run(
            cmd, shell=True, check=check, capture_output=True, text=True
        )
        result = release_module.refresh_lockfile()
        assert result.returncode == 0
        log = fake_repo["uv_log"].read_text()
        assert log.strip() == "lock"

    def test_refresh_lockfile_aborts_when_uv_lock_missing(
        self, release_module, fake_repo, monkeypatch
    ):
        """If ``uv.lock`` is missing, the script must abort before any work."""
        # Remove the lockfile to simulate a project that does not use uv.
        fake_repo["lock"].unlink()
        monkeypatch.chdir(fake_repo["repo"])
        # The script should exit(1) before running uv.
        with pytest.raises(SystemExit) as excinfo:
            release_module.refresh_lockfile()
        assert excinfo.value.code != 0
        # The shim must not have been invoked — the log file should not exist.
        assert not fake_repo["uv_log"].exists()

    def test_uv_lock_failure_aborts_release(self, release_module, fake_repo, monkeypatch):
        """If ``uv lock`` fails, the release script exits non-zero before any commit/tag."""
        # Make the uv shim fail.
        shim = fake_repo["shim_dir"] / "uv"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stderr.write('simulated uv lock failure\\n')\n"
            "sys.exit(1)\n"
        )
        shim.chmod(0o755)

        # Track all git commands invoked.
        git_log: list[str] = []

        def fake_run_cmd(cmd, check=True):
            git_log.append(cmd)
            if cmd.startswith("uv "):
                # Mirror the real run_cmd behavior on CalledProcessError.
                print(f"Error running command: {cmd}")
                sys.exit(1)
            return subprocess.run(
                cmd, shell=True, check=check, capture_output=True, text=True
            )

        monkeypatch.chdir(fake_repo["repo"])
        release_module.run_cmd = fake_run_cmd
        # Patch input() to mimic -y/--yes so we get a fully non-interactive run.
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")

        with pytest.raises(SystemExit) as excinfo:
            release_module.main()
        assert excinfo.value.code != 0

        # No commit, no tag, and no push should have been issued.
        joined = " | ".join(git_log)
        assert "git commit" not in joined
        assert "git tag" not in joined
        assert "git push" not in joined
        # And the version-bump commit should not exist in the repo.
        head = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=fake_repo["repo"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "bump version" not in head.stdout

    def test_release_stages_both_pyproject_and_lock(self, release_module, fake_repo, monkeypatch):
        """The release script must stage both ``pyproject.toml`` and ``uv.lock``."""
        git_log: list[str] = []

        def fake_run_cmd(cmd, check=True):
            git_log.append(cmd)
            try:
                return subprocess.run(
                    cmd, shell=True, check=check, capture_output=True, text=True
                )
            except subprocess.CalledProcessError:
                # Mirror the real run_cmd: abort on failure.
                sys.exit(1)

        monkeypatch.chdir(fake_repo["repo"])
        release_module.run_cmd = fake_run_cmd
        release_module.run_interactive = lambda cmd: subprocess.run(
            cmd, shell=True, check=False, capture_output=True, text=True
        )
        # Force non-interactive so no input() is required.
        monkeypatch.setattr(sys, "argv", ["release.py", "0.4.0", "-y"])

        with pytest.raises(SystemExit):
            release_module.main()

        # Find the ``git add`` call and verify it stages both files.
        add_calls = [cmd for cmd in git_log if cmd.startswith("git add")]
        assert add_calls, "release script did not run git add"
        # The last ``git add`` call before commit must include both files.
        last_add = add_calls[-1]
        assert "pyproject.toml" in last_add
        assert "uv.lock" in last_add

    def test_release_uv_lock_runs_after_set_version(self, release_module, fake_repo, monkeypatch):
        """``uv lock`` must run after the version bump and before any git commit."""
        order: list[str] = []

        def fake_set_version(new_version):
            order.append(f"set_version:{new_version}")

        def fake_refresh_lockfile():
            order.append("refresh_lockfile")
            with open("uv.lock", "a") as f:
                f.write("# refreshed\n")

        def fake_run_cmd(cmd, check=True):
            order.append(f"run_cmd:{cmd}")
            try:
                return subprocess.run(
                    cmd, shell=True, check=check, capture_output=True, text=True
                )
            except subprocess.CalledProcessError:
                sys.exit(1)

        monkeypatch.chdir(fake_repo["repo"])
        release_module.set_version = fake_set_version  # type: ignore[assignment]
        release_module.refresh_lockfile = fake_refresh_lockfile  # type: ignore[assignment]
        release_module.run_cmd = fake_run_cmd
        release_module.run_interactive = lambda cmd: subprocess.run(
            cmd, shell=True, check=False, capture_output=True, text=True
        )
        monkeypatch.setattr(sys, "argv", ["release.py", "0.4.0", "-y"])

        with pytest.raises(SystemExit):
            release_module.main()

        set_version_idx = order.index("set_version:0.4.0")
        refresh_idx = order.index("refresh_lockfile")
        first_add_idx = next(
            (i for i, entry in enumerate(order) if entry.startswith("run_cmd:git add")),
            None,
        )
        assert first_add_idx is not None
        # set_version -> refresh_lockfile -> first git add
        assert set_version_idx < refresh_idx < first_add_idx
        # And no git commit/tag/push before refresh_lockfile.
        for entry in order[:refresh_idx]:
            assert "git commit" not in entry
            assert "git tag" not in entry
            assert "git push" not in entry

    def test_release_uses_uv_lock_from_current_directory(
        self, release_module, fake_repo, monkeypatch
    ):
        """The ``uv lock`` command must be invoked from the repo root."""
        invocations: list[str] = []

        def fake_run_cmd(cmd, check=True):
            if cmd.startswith("uv "):
                invocations.append(cmd)
            return subprocess.run(
                cmd, shell=True, check=check, capture_output=True, text=True
            )

        monkeypatch.chdir(fake_repo["repo"])
        release_module.run_cmd = fake_run_cmd
        release_module.refresh_lockfile()
        assert invocations
        assert invocations[0] == "uv lock"
