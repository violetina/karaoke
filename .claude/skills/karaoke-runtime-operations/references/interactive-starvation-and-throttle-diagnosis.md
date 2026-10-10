# Interactive starvation and CPU throttle diagnosis

Background batch jobs on this workstation can starve *anything* latency-sensitive —
Web TUI responsiveness, audio, and notably **screen casting to a TV**. The failure
presents as a network/device problem and is not one.

## Signature: "casting works but the TV image freezes"

Reported symptom was a frozen picture on the TV while casting appeared connected.
Root cause was zero CPU headroom, not Wi-Fi or the receiver.

Evidence collected at the time:

```
2559529  karaoke.vector_index --rebuild --lines   856% CPU, 59 threads
450415   overnight_collection_scan.py            237% CPU
load average: 17.25
Tctl:            99.2 °C
PPT:             21.65 W        <- power budget floor
avg cpu freq:    609 MHz        <- with governor = performance
```

`governor = performance` at **609 MHz** is the tell: the SoC is not idling, it is
thermally/power capped. With every core saturated there is nothing left for
screen capture plus H.264/VP9 encode, so the encoder drops frames and the TV
holds its last decoded frame.

After killing the offending job: load `17.25 -> 11.70`, avg freq
`609 MHz -> 2265 MHz` (3.7x), PPT `21.65 W -> 26.00 W`. Casting recovered.

## Diagnose CPU starvation before blaming the network

Check in this order — the cheap local checks first:

```bash
cat /proc/loadavg
awk '/cpu MHz/{s+=$4;n++} END{printf "avg_cpu_mhz=%.0f cores=%d\n", s/n, n}' /proc/cpuinfo
cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor
sensors | grep -iE 'Tctl|PPT|fan1'
ps -eo pid,%cpu,%mem,comm --sort=-%cpu | head -10
```

Interpretation: a low average clock **combined with** `performance` governor and a
high `Tctl` / floored `PPT` means throttling. Then identify the top consumers and
resolve their full command line — `%CPU` over ~700 on a 24-thread part is one job
taking essentially the whole machine:

```bash
tr '\0' ' ' < /proc/<pid>/cmdline
ls /proc/<pid>/task | wc -l      # thread count
ps -o ppid= -p <pid>             # find the launcher (often a cron wrapper)
```

Always trace the parent. In this session the 856%-CPU rebuild's parent was a
Hermes cron script, which is the actual thing that needed fixing — killing the
child alone would have let it return on the next tick.

Only after CPU is ruled out, look at the radio. Useful but secondary:

```bash
nmcli -f IN-USE,SSID,CHAN,RATE,SIGNAL,BARS dev wifi
systemctl is-active avahi-daemon      # mDNS discovery for cast receivers
```

A weak 5 GHz signal makes a CPU-starved encoder look completely frozen, so both
can contribute — but fix the starvation first, it is the dominant term.

## Harden recurring background jobs against this

Two independent guards, both needed:

1. **Cap the parallelism.** Pass the job's own throttle flags rather than letting
   it size itself to the machine. For the vector refresh that is
   `--threads 4 --chunk-size 500`. An unthrottled rebuild grabs all 24 threads.
2. **Skip while a higher-priority writer is running.** Ingestion is a long
   sequential job that also holds SQLite; rebuilding vectors underneath it
   competes for CPU *and* the DB, and the result is stale the moment ingestion
   continues. Detect and exit 0 rather than compete:

```python
BLOCKING_PROCESSES = (
    "scripts/overnight_collection_scan.py",
    "scripts/retry_skipped_collection.py",
)

def blocking_process() -> str:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                         text=True, timeout=30, check=False).stdout
    for line in out.splitlines():
        if line.strip().startswith("ps "):   # skip our own probe
            continue
        for needle in BLOCKING_PROCESSES:
            if needle in line and "python" in line:
                return line.strip()
    return ""
```

Verify the guard fires before trusting it — a skip must be observable:

```
KARAOKE VECTOR REFRESH SKIPPED started=... reason=ingestion-running
```

Note a `no_agent` cron job whose script is edited on disk picks up the change on
its next tick; no reinstall step is needed. Cron jobs configured
`deliver: local` save output but do not message anyone, so a silently skipping
job will look like nothing happened — keep the skip line in stdout.

## Pitfall: tracebacks from a long-running process point at the wrong line

After patching a script on disk while its process was still running, the reported
traceback was:

```
File ".../overnight_collection_scan.py", line 18, in write
    # Persist first: unattended runs may outlive the launching terminal/process,
BrokenPipeError: [Errno 32] Broken pipe
```

Line 18 is a *comment* — it cannot raise. The running interpreter still held the
pre-edit code, while `linecache` rendered the traceback against the **new** file
contents, so line numbers no longer aligned with the executing bytecode.

Consequences to remember:

- A traceback pointing at a comment or a blank line means the source on disk has
  drifted from the loaded code. Do not "fix" the innocent line.
- Editing a file does **not** fix a running process. Confirm whether the fix is
  live by checking process start time against the file mtime; a patched importer
  keeps failing exactly as before until restarted.
- When the process is nearly finished and the failure is cosmetic (a broken
  progress log while ingestion continues correctly), prefer letting it finish
  over restarting to pick up the fix.
