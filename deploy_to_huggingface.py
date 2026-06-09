from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys


SPACE_ID_DEFAULT = os.getenv("HF_SPACE_ID", "your-hf-username/uoh-blackboard-mcp")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_TO_INCLUDE = [
    "README.md",
    "Dockerfile",
    "app.py",
    "server.py",
    "requirements.txt",
]


def run(args: list[str], env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=PROJECT_DIR, env=env, check=True)


def main() -> int:
    space_id = input(f"Hugging Face Space ID [{SPACE_ID_DEFAULT}]: ").strip() or SPACE_ID_DEFAULT
    token = getpass.getpass("Hugging Face token with write access: ").strip()
    if not token:
        print("A Hugging Face token is required.", file=sys.stderr)
        return 1

    env = {**os.environ, "HF_TOKEN": token}
    try:
        hf_bin = shutil.which("hf") or os.path.expanduser("~/.local/bin/hf")
        run(
            [
                hf_bin,
                "repos",
                "create",
                space_id,
                "--type",
                "space",
                "--space-sdk",
                "docker",
                "--exist-ok",
            ],
            env=env,
        )
        run(
            [
                hf_bin,
                "upload",
                space_id,
                ".",
                "--type",
                "space",
                "--include",
                ",".join(FILES_TO_INCLUDE),
                "--commit-message",
                "Deploy Blackboard MCP Docker Space",
            ],
            env=env,
        )
    except FileNotFoundError:
        print("The `hf` CLI is not installed. Install it with: curl -LsSf https://hf.co/cli/install.sh | bash -s", file=sys.stderr)
        return 1

    print(f"Deployed Space: https://huggingface.co/spaces/{space_id}")
    print(f"MCP endpoint: https://{space_id.replace('/', '-')}.hf.space/sse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
