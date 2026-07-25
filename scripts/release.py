#!/usr/bin/env python3
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd, check=True):
    try:
        return subprocess.run(
            shlex.split(cmd),
            check=check,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {cmd}")
        print(e.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error running command: {cmd}")
        print(e)
        sys.exit(1)


def run_interactive(cmd):
    """Run a command, inheriting stdio (so the user sees prompts/output)."""
    return subprocess.run(shlex.split(cmd), check=True)


def get_current_version():
    if not os.path.exists("pyproject.toml"):
        print("Error: pyproject.toml not found in the current directory.")
        sys.exit(1)

    with open("pyproject.toml", "r") as f:
        content = f.read()

    # Simple regex to find version in [project] block
    match = re.search(r'\[project\]\n(?:.*\n)*?version\s*=\s*"([^"]+)"', content)
    if not match:
        # Fallback to first version line
        match = re.search(r'version\s*=\s*"([^"]+)"', content)

    if match:
        return match.group(1)
    return None


def set_version(new_version):
    with open("pyproject.toml", "r") as f:
        content = f.read()

    # Replace version in [project] block
    project_match = re.search(r'(\[project\]\n(?:.*\n)*?version\s*=\s*")([^"]+)(")', content)
    if project_match:
        old_val = project_match.group(0)
        new_val = project_match.group(1) + new_version + project_match.group(3)
        new_content = content.replace(old_val, new_val, 1)
    else:
        new_content = re.sub(
            r'(version\s*=\s*")[^"]+(")',
            rf'\g<1>{new_version}\g<2>',
            content,
            count=1,
        )

    with open("pyproject.toml", "w") as f:
        f.write(new_content)


def refresh_lockfile():
    """Regenerate ``uv.lock`` so it tracks the new project version.

    Returns the CompletedProcess from ``uv lock`` for callers/tests that
    want to inspect the result. Exits the process with a clear error if
    ``uv`` is not available or the lock command fails, so the version bump
    is never committed with a stale lock.
    """
    if not os.path.exists("uv.lock"):
        print("Error: uv.lock not found in the current directory.")
        sys.exit(1)
    print("Refreshing uv.lock to match the new project version...")
    return run_cmd("uv lock")


def get_next_version(current_version, bump_type):
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)(.*)$', current_version)
    if not match:
        return None
    major, minor, patch, suffix = match.groups()
    if bump_type == "patch":
        return f"{major}.{minor}.{int(patch) + 1}"
    elif bump_type == "minor":
        return f"{major}.{int(minor) + 1}.0"
    elif bump_type == "major":
        return f"{int(major) + 1}.0.0"
    return None


def push_release(branch, tag):
    """Atomically push the release branch and tag."""
    print(f"Pushing to origin {branch}...")
    run_cmd(f"git push --atomic origin {shlex.quote(branch)} {tag}")
    print("Push complete. GitHub Action should trigger shortly!")


def main():
    # Ensure we are in git repository root
    if not os.path.exists(".git"):
        print("Error: Must run from the root of the git repository.")
        sys.exit(1)

    # Parse arguments
    non_interactive = False
    if "--yes" in sys.argv or "-y" in sys.argv:
        non_interactive = True
        sys.argv = [arg for arg in sys.argv if arg not in ("--yes", "-y")]

    version_arg = sys.argv[1] if len(sys.argv) > 1 else None
    bump_arg = version_arg.lower() if version_arg else None

    # 1. Check git status
    status = run_cmd("git status --porcelain")
    if status.stdout.strip():
        print("Warning: You have uncommitted changes in your git repository:")
        print(status.stdout)
        if non_interactive:
            print("Aborting: Git workspace is not clean in non-interactive mode.")
            sys.exit(1)
        confirm = input("Do you want to proceed anyway? (y/N): ").strip().lower()
        if confirm != 'y':
            print("Aborted.")
            sys.exit(1)

    # 2. Get current version
    current_version = get_current_version()
    if not current_version:
        print("Error: Could not find version in pyproject.toml")
        sys.exit(1)

    print(f"Current version: {current_version}")

    # Determine new version
    next_patch = get_next_version(current_version, "patch")
    next_minor = get_next_version(current_version, "minor")
    next_major = get_next_version(current_version, "major")

    new_version = None
    if version_arg:
        if bump_arg == "patch":
            new_version = next_patch
        elif bump_arg == "minor":
            new_version = next_minor
        elif bump_arg == "major":
            new_version = next_major
        elif re.match(r'^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$', version_arg):
            new_version = version_arg
        else:
            print(f"Error: Invalid version or bump type '{version_arg}'")
            print("Usage: ./scripts/release.py [patch|minor|major|<version>] [-y|--yes]")
            sys.exit(1)

    if not new_version:
        if non_interactive:
            new_version = next_patch
            if not new_version:
                print("Error: Could not calculate next patch version automatically.")
                sys.exit(1)
            print(f"Automatically selected next patch version: {new_version}")
        else:
            default_str = f" [default: {next_patch}]" if next_patch else ""
            user_input = input(f"Enter new version{default_str}: ").strip()
            if not user_input:
                if next_patch:
                    new_version = next_patch
                else:
                    print("Error: No version provided and cannot calculate patch default.")
                    sys.exit(1)
            else:
                new_version = user_input

    # Validate version format roughly
    if not re.match(r'^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$', new_version):
        print(f"Warning: Version format '{new_version}' does not look standard (e.g. 1.0.0)")
        if non_interactive:
            print("Aborting: Invalid version format in non-interactive mode.")
            sys.exit(1)
        confirm = input("Proceed with this version name? (y/N): ").strip().lower()
        if confirm != 'y':
            sys.exit(1)
    branch_res = run_cmd("git branch --show-current")
    branch = branch_res.stdout.strip()
    if not branch:
        print("Error: Cannot release from a detached HEAD.")
        sys.exit(1)

    tag = f"v{new_version}"
    tag_exists = (
        run_cmd(f"git rev-parse -q --verify refs/tags/{tag}", check=False).returncode
        == 0
    )
    if new_version == current_version:
        expected_subject = f"chore: bump version to v{new_version}"
        tag_commit = run_cmd(
            f"git rev-parse -q --verify refs/tags/{tag}^{{commit}}",
            check=False,
        )
        head_commit = run_cmd("git rev-parse HEAD")
        head_subject = run_cmd("git log -1 --format=%s")
        if (
            tag_exists
            and tag_commit.returncode == 0
            and tag_commit.stdout.strip() == head_commit.stdout.strip()
            and head_subject.stdout.strip() == expected_subject
        ):
            print(f"Resuming push for existing local release {tag}.")
            push_release(branch, tag)
            return
        print(f"Error: Version is already {current_version}.")
        sys.exit(1)

    if tag_exists:
        print(f"Error: Tag {tag} already exists.")
        sys.exit(1)

    # 3-4. Update version artifacts transactionally through lock refresh.
    if not Path("uv.lock").exists():
        print("Error: uv.lock not found in the current directory.")
        sys.exit(1)
    pyproject_before = Path("pyproject.toml").read_bytes()
    lock_before = Path("uv.lock").read_bytes()
    try:
        set_version(new_version)
        print(f"Updated pyproject.toml to version {new_version}")
        refresh_lockfile()
    except BaseException:
        Path("pyproject.toml").write_bytes(pyproject_before)
        Path("uv.lock").write_bytes(lock_before)
        raise

    # 5. Git commit and tag
    run_cmd("git add pyproject.toml uv.lock")
    run_cmd(
        f'git commit --only pyproject.toml uv.lock '
        f'-m "chore: bump version to v{new_version}"'
    )
    run_cmd(f"git tag {tag}")
    print(f"Committed and tagged with v{new_version}")

    # 6. Push branch and tag atomically.
    push_release(branch, tag)

if __name__ == "__main__":
    main()
