from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from urllib.parse import quote

import requests


REPO_NAME = "uoh-blackboard-mcp"
DEFAULT_OWNER = os.getenv("GITHUB_OWNER", "your-github-username")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_TO_STAGE = [
    "server.py",
    "requirements.txt",
    "run_tunnel.py",
    "push_to_github.py",
    "Dockerfile",
    ".gitignore",
    ".dockerignore",
]


def run(args: list[str], check: bool = True, hide_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=PROJECT_DIR,
        check=check,
        text=True,
        stdout=subprocess.PIPE if hide_output else None,
        stderr=subprocess.PIPE if hide_output else None,
    )


def infer_owner() -> str:
    result = run(["git", "remote", "get-url", "origin"], check=False, hide_output=True)
    remote = (result.stdout or "").strip()
    marker = "github.com/"
    if marker in remote:
        tail = remote.split(marker, 1)[1].removesuffix(".git")
        if "/" in tail:
            return tail.split("/", 1)[0].split("@")[-1]
    return DEFAULT_OWNER


def ensure_repo(owner: str, token: str) -> None:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repo_url = f"https://api.github.com/repos/{owner}/{REPO_NAME}"
    response = requests.get(repo_url, headers=headers, timeout=30)
    if response.status_code == 200:
        return
    if response.status_code != 404:
        raise RuntimeError(f"Could not inspect repo: HTTP {response.status_code} {response.text[:500]}")

    create = requests.post(
        "https://api.github.com/user/repos",
        headers=headers,
        data=json.dumps({"name": REPO_NAME, "private": True, "auto_init": False}),
        timeout=30,
    )
    if create.status_code not in {200, 201}:
        raise RuntimeError(f"Could not create repo: HTTP {create.status_code} {create.text[:500]}")


def ensure_git_repo(owner: str) -> None:
    if not os.path.isdir(os.path.join(PROJECT_DIR, ".git")):
        run(["git", "init"])
    run(["git", "branch", "-M", "main"], check=False)
    remote_url = f"https://github.com/{owner}/{REPO_NAME}.git"
    existing = run(["git", "remote", "get-url", "origin"], check=False, hide_output=True)
    if existing.returncode == 0:
        run(["git", "remote", "set-url", "origin", remote_url])
    else:
        run(["git", "remote", "add", "origin", remote_url])


def commit_changes() -> None:
    existing_files = [path for path in FILES_TO_STAGE if os.path.exists(os.path.join(PROJECT_DIR, path))]
    run(["git", "add", *existing_files])
    status = run(["git", "status", "--porcelain"], hide_output=True)
    if not (status.stdout or "").strip():
        print("No local changes to commit.")
        return
    run(["git", "commit", "-m", "Rebuild Blackboard MCP PRD server"])


def push(owner: str, token: str) -> None:
    safe_token = quote(token, safe="")
    authed_url = f"https://{owner}:{safe_token}@github.com/{owner}/{REPO_NAME}.git"
    run(["git", "push", "-u", authed_url, "main"])


def main() -> int:
    owner = infer_owner()
    typed_owner = input(f"GitHub username [{owner}]: ").strip()
    owner = typed_owner or owner
    token = getpass.getpass("GitHub Personal Access Token: ").strip()
    if not token:
        print("A GitHub PAT is required.", file=sys.stderr)
        return 1

    ensure_repo(owner, token)
    ensure_git_repo(owner)
    commit_changes()
    push(owner, token)
    print(f"Pushed to https://github.com/{owner}/{REPO_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
