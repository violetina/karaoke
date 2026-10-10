# Overnight scan progress logging and liveness checks

Session learnings for unattended karaoke collection scans.

## Broken stdout pipe is not necessarily a dead scan

If `scripts/overnight_collection_scan.py` logs tracebacks like:

```text
BrokenPipeError: [Errno 32] Broken pipe
folder-scan progress callback failed
```

while folder scan messages continue in `~/.local/share/karaoke/logs/karaoke.log*`, the scan may still be alive. The progress callback can fail because stdout/stderr was attached to a terminal or pipe that disappeared, while the scanner catches callback exceptions and continues processing.

Liveness checks:

- Check the process: `ps -eo pid,etime,%cpu,%mem,cmd | grep overnight_collection_scan`
- Check DB movement over 20 seconds: counts in `tracks`, `sources`, and `track_analysis` should increase if the import is still active.
- Check app logs: `grep 'Folder scan ' ~/.local/share/karaoke/logs/karaoke.log* | tail`
- Do not rely only on `overnight_collection_scan.log` if stdout broke before the script was patched.

## Robust progress writer pattern

For unattended scripts, write durable progress before printing to stdout and swallow broken pipe on stdout:

```python
def write(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    try:
        print(text, flush=True)
    except BrokenPipeError:
        pass
```

If a running process shows a traceback line pointing at a comment after a source patch, remember Python's `linecache` can display the new on-disk source against old bytecode line numbers from the already-running process. Verify liveness with process/DB/log evidence before restarting a long scan.
