"""Command-line / shortcut control for a running worker.

    python worker_ctl.py pause        # freeze now, keep place
    python worker_ctl.py pause_after  # finish current file, then pause
    python worker_ctl.py resume
    python worker_ctl.py stop         # stop current file, return it to the queue, pause
    python worker_ctl.py quit         # hand back current file, shut the worker down
    python worker_ctl.py status
"""
import json
import sys
import urllib.request

from aiconverter.config import CONFIG

COMMANDS = {"pause": "pause_now", "pause_after": "pause_after", "resume": "resume", "stop": "stop_job", "quit": "quit"}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    base = f"http://127.0.0.1:{CONFIG['worker_control_port']}"
    if cmd == "status":
        req = urllib.request.Request(base + "/api/status")
    elif cmd in COMMANDS:
        req = urllib.request.Request(f"{base}/control/{COMMANDS[cmd]}?json=1", data=b"", method="POST")
    else:
        print(__doc__)
        return 2
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(json.dumps(json.loads(resp.read()), indent=2))
    except OSError as e:
        print(f"Worker is not running or not reachable at {base}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
