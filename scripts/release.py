#!/usr/bin/env python3
import sys
import os
import re
import subprocess

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
        new_content = content.replace(project_match.group(0), project_match.group(1) + new_version + project_match.group(3), 1)
    else:
        new_content = re.sub(r'(version\s*=\s*")[^"]+(")', rf'\g<1>{new_version}\g<2>', content, count=1)
        
    with open("pyproject.toml", "w") as f:
        f.write(new_content)

def main():
    # Ensure we are in git repository root
    if not os.path.exists(".git"):
        print("Error: Must run from the root of the git repository.")
        sys.exit(1)

    # 1. Check git status
    status = run_cmd("git status --porcelain")
    if status.stdout.strip():
        print("Warning: You have uncommitted changes in your git repository:")
        print(status.stdout)
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
    new_version = input("Enter new version (e.g. 0.1.1): ").strip()
    if not new_version:
        print("Aborted.")
        sys.exit(0)
        
    # Validate version format roughly (X.Y.Z)
    if not re.match(r'^\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?$', new_version):
        print("Warning: Version format does not look standard (e.g. 1.0.0 or 1.0.0-beta.1)")
        confirm = input("Proceed with this version name? (y/N): ").strip().lower()
        if confirm != 'y':
            sys.exit(1)

    # 3. Update version
    set_version(new_version)
    print(f"Updated pyproject.toml to version {new_version}")

    # 4. Git commit and tag
    run_cmd(f"git add pyproject.toml")
    run_cmd(f'git commit -m "chore: bump version to v{new_version}"')
    run_cmd(f'git tag v{new_version}')
    print(f"Committed and tagged with v{new_version}")

    # 5. Push
    push_confirm = input("Push commit and tag to origin? (y/N): ").strip().lower()
    if push_confirm == 'y':
        # Get current branch
        branch_res = run_cmd("git branch --show-current")
        branch = branch_res.stdout.strip()
        print(f"Pushing to origin {branch}...")
        run_cmd(f"git push origin {branch}")
        run_cmd(f"git push origin v{new_version}")
        print("Push complete. GitHub Action should trigger shortly!")
    else:
        print("\nPush skipped. You can manually push when ready using:")
        print(f"  git push origin HEAD && git push origin v{new_version}")

if __name__ == "__main__":
    main()
