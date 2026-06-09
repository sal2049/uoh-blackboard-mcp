from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TUNNEL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def stream_process(name: str, process: subprocess.Popen[str], url_event: threading.Event) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[{name}] {line}", end="", flush=True)
        match = TUNNEL_RE.search(line)
        if match:
            print("\nMCP URL:", match.group(0).rstrip("/") + "/sse", flush=True)
            url_event.set()


def main() -> int:
    cloudflared = shutil.which("cloudflared") or "/tmp/cloudflared"
    if not os.path.exists(cloudflared):
        print(
            "cloudflared was not found. Install it or place the binary on PATH, then rerun this script.",
            file=sys.stderr,
        )
        return 1

    uvicorn_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    tunnel_cmd = [cloudflared, "tunnel", "--url", "http://localhost:8000"]

    url_event = threading.Event()
    processes: list[subprocess.Popen[str]] = []

    try:
        server = subprocess.Popen(
            uvicorn_cmd,
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        processes.append(server)
        threading.Thread(target=stream_process, args=("server", server, url_event), daemon=True).start()
        time.sleep(2)

        tunnel = subprocess.Popen(
            tunnel_cmd,
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        processes.append(tunnel)
        threading.Thread(target=stream_process, args=("cloudflared", tunnel, url_event), daemon=True).start()

        print("Waiting for Cloudflare Quick Tunnel URL...", flush=True)
        url_event.wait(timeout=90)
        if not url_event.is_set():
            print("Tunnel started, but no trycloudflare.com URL was detected within 90 seconds.", file=sys.stderr)

        while all(process.poll() is None for process in processes):
            time.sleep(1)

        return next((process.returncode or 0 for process in processes if process.poll() is not None), 0)
    except KeyboardInterrupt:
        return 130
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
