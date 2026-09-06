#!/usr/bin/env python3
"""Runner script for serving the Karaoke TUI over Web/Browser.

Checks and reports the status of kiosk Chrome CDP (:9222) before starting:
- [✓] kiosk-chrome   (opt)  CDP :9222 active
- [!] kiosk-chrome   (opt)  kiosk Chrome CDP :9222 down (unified player off)
"""
import os
import sys
import subprocess
import urllib.request
import time
from pathlib import Path

CDP_URL = "http://localhost:9222/json"
KIOSK_PROFILE = os.path.expanduser("~/.local/share/karaoke/kiosk-chrome")


def check_cdp(timeout: float = 1.5) -> bool:
    """Return True if Chrome CDP debugging port :9222 is responding."""
    try:
        req = urllib.request.Request(CDP_URL)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def launch_kiosk_chrome() -> bool:
    """Launch Google Chrome in kiosk debugging mode on port 9222."""
    chrome_bin = os.environ.get("CHROME", "google-chrome-stable")
    if not shutil_which(chrome_bin):
        chrome_bin = "google-chrome"
        if not shutil_which(chrome_bin):
            return False

    cmd = [
        chrome_bin,
        '--app=https://music.youtube.com',
        '--remote-debugging-port=9222',
        f'--user-data-dir={KIOSK_PROFILE}',
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        return check_cdp()
    except Exception:
        return False


def shutil_which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


def main() -> int:
    start_kiosk_if_down = "--start-kiosk" in sys.argv or os.environ.get("KARAOKE_AUTO_KIOSK") == "1"

    print("Checking Karaoke platform components...")
    is_up = check_cdp()

    if is_up:
        print("  [✓] kiosk-chrome   (opt)  CDP :9222 active")
    else:
        print("  [!] kiosk-chrome   (opt)  kiosk Chrome CDP :9222 down (unified player off)")
        if start_kiosk_if_down:
            print("  [➜] Attempting to launch kiosk Chrome CDP on port 9222...")
            if launch_kiosk_chrome():
                print("  [✓] kiosk-chrome   (opt)  CDP :9222 active")
            else:
                print("  [!] Could not launch kiosk Chrome automatically.")

    print("\nStarting Web TUI server (textual-serve)...")
    python_bin = sys.executable
    cmd = [python_bin, "-m", "textual", "serve", f"{python_bin} -m karaoke.tui"]
    try:
        os.execvp(cmd[0], cmd)
    except Exception as exc:
        print(f"Error starting textual-serve: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
