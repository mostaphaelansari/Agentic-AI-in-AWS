"""Start Streamlit locally and verify its health endpoint."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "comparison" / "app.py"
PORT = 18501
HEALTH_URL = f"http://127.0.0.1:{PORT}/_stcore/health"


def main() -> int:
    env = os.environ.copy()
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP),
            "--server.headless=true",
            f"--server.port={PORT}",
            "--server.address=127.0.0.1",
            "--server.fileWatcherType=none",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                print(output, file=sys.stderr)
                return process.returncode or 1
            try:
                with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                    if response.status == 200 and response.read().strip() == b"ok":
                        print("Streamlit health check passed.")
                        return 0
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1)
        print("Streamlit did not become healthy within 45 seconds.", file=sys.stderr)
        return 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
