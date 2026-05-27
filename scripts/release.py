#!/usr/bin/env python3
import os
import re
import subprocess
import sys


def run_cmd(cmd, check=True):
    try:
        return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {cmd}")
        print(e.stderr)
        sys.exit(1)

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

    version_arg = sys.argv[1].lower() if len(sys.argv) > 1 else None

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
        if version_arg == "patch":
            new_version = next_patch
        elif version_arg == "minor":
            new_version = next_minor
        elif version_arg == "major":
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

    # 3. Update version
    set_version(new_version)
    print(f"Updated pyproject.toml to version {new_version}")

    # 4. Git commit and tag
    run_cmd("git add pyproject.toml")
    run_cmd(f'git commit -m "chore: bump version to v{new_version}"')
    run_cmd(f'git tag v{new_version}')
    print(f"Committed and tagged with v{new_version}")

    # 5. Push
    # Get current branch
    branch_res = run_cmd("git branch --show-current")
    branch = branch_res.stdout.strip()
    print(f"Pushing to origin {branch}...")
    run_cmd(f"git push origin {branch}")
    run_cmd(f"git push origin v{new_version}")
    print("Push complete. GitHub Action should trigger shortly!")

if __name__ == "__main__":
    main()
