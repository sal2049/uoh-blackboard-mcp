from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
PORT = int(os.getenv("PORT", "8000"))
LOCAL_URL = f"http://localhost:{PORT}"
SSE_URL = f"{LOCAL_URL}/sse"
TUNNEL_RE = re.compile(r"https://[-a-zA-Z0-9]+\.trycloudflare\.com")


def find_cloudflared() -> str:
    candidates = [
        shutil.which("cloudflared"),
        str(PROJECT_DIR / ".tools" / "cloudflared"),
        str(Path.home() / ".local" / "bin" / "cloudflared"),
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit(
        "cloudflared is not installed or not on PATH. Install it, then rerun:\n"
        "  brew install cloudflared\n"
        "or place the cloudflared binary at .tools/cloudflared inside this project."
    )


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"Server did not start listening on {host}:{port} within {timeout} seconds.")


def stream_output(name: str, process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        text = line.rstrip()
        print(f"[{name}] {text}", flush=True)
        lines.put(text)


def terminate(process: subprocess.Popen[str] | None) -> None:
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    load_dotenv()
    if not PYTHON.exists():
        raise SystemExit("Missing .venv. Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt")

    cloudflared = find_cloudflared()
    env = {
        **os.environ,
        "HOST": "0.0.0.0",
        "PORT": str(PORT),
        "PYTHONUNBUFFERED": "1",
    }

    server: subprocess.Popen[str] | None = None
    tunnel: subprocess.Popen[str] | None = None

    def stop_all(_signum: int | None = None, _frame: object | None = None) -> None:
        print("\nStopping local MCP server and Cloudflare tunnel...", flush=True)
        terminate(tunnel)
        terminate(server)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    server = subprocess.Popen(
        [str(PYTHON), "app.py"],
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    server_lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=stream_output, args=("mcp", server, server_lines), daemon=True).start()

    wait_for_port("127.0.0.1", PORT)
    print("\n============================================================", flush=True)
    print(f"LOCAL MCP SERVER LISTENING: {SSE_URL}", flush=True)
    print("============================================================\n", flush=True)

    tunnel = subprocess.Popen(
        [cloudflared, "tunnel", "--protocol", "http2", "--url", LOCAL_URL],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tunnel_lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=stream_output, args=("cloudflared", tunnel, tunnel_lines), daemon=True).start()

    public_url = None
    deadline = time.time() + 90
    while time.time() < deadline and public_url is None:
        if server.poll() is not None:
            raise RuntimeError("MCP server exited before the tunnel became ready.")
        if tunnel.poll() is not None:
            raise RuntimeError("cloudflared exited before printing a tunnel URL.")
        try:
            line = tunnel_lines.get(timeout=0.5)
        except queue.Empty:
            continue
        match = TUNNEL_RE.search(line)
        if match:
            public_url = match.group(0)

    if not public_url:
        raise RuntimeError("Timed out waiting for a trycloudflare.com tunnel URL.")

    print("\n" + "=" * 72, flush=True)
    print("CLOUDFLARE MCP URL READY", flush=True)
    print(f"{public_url}/sse", flush=True)
    print("=" * 72, flush=True)
    print("\nPaste that /sse URL into poke.com/integrations/new.", flush=True)
    print("Keep this terminal open while using the tunnel. Press Ctrl+C to stop.\n", flush=True)

    while True:
        if server.poll() is not None:
            raise RuntimeError("MCP server stopped unexpectedly.")
        if tunnel.poll() is not None:
            raise RuntimeError("cloudflared tunnel stopped unexpectedly.")
        time.sleep(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
