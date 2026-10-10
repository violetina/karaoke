# CPU contention: slow Web TUI and frozen screen casting

Verified on this host (AMD Ryzen AI 9 HX 370, 24 threads, Gentoo, COSMIC/Wayland)
while a 20k-file archive import was running.

## Symptom cluster — one cause

Users report these separately, but they are the same problem:

- "the webtui atm is very slow"
- "I try to cast my screen it is casting but image freeze on tv"
- audio glitches / laggy key response

Casting is the most misleading one. The connection succeeds, the receiver is
found, the link is fine — and the TV still shows a still frame, because the
capture/encode pipeline gets no CPU and the receiver simply holds the last
decoded frame. **Do not start with Wi-Fi or the receiver.** Check CPU first.

## The signature

Throttling, not idling. The giveaway is a low clock while the governor is
`performance`:

```
load average: 17.25
avg cpu freq:  609 MHz      # governor=performance
Tctl:          99.2 C
PPT:           21.65 W      # far below boost budget
fan1:          6301 RPM
```

Confirm with:

```
ps -eo pid,%cpu,%mem,cmd --sort=-%cpu | head -12
awk '/cpu MHz/{s+=$4;n++} END{printf "avg_cpu_mhz=%.0f cores=%d\n", s/n, n}' /proc/cpuinfo
cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor
sensors | grep -iE 'Tctl|PPT|fan1'
cat /proc/loadavg
```

What was actually consuming the machine:

```
2559529  vector_index --rebuild --lines   856% CPU, 59 threads
450415   overnight_collection_scan.py     237% CPU
2552703  karaoke.tui                       41%
2502018  karaoke.tui                       19%
2550672  karaoke.tui                        7%
2079384  karaoke.tui  (orphan since Sep 8, parent=1)
```

## Root cause found

A `no_agent` cron job firing `every 40m` ran a **full** vector rebuild with no
thread cap, on top of a live importer. The vector rebuild's own lock does not
guard against a plain folder scan, so the two ran concurrently.

## Recovery, in order

1. Kill the unthrottled rebuild — and its cron wrapper, or the parent dangles:
   ```
   pgrep -af 'karaoke.vector_index'
   kill <wrapper_pid> <rebuild_pid>
   ```
2. Reap orphaned TUIs. Distinguish orphans from live sessions by parent:
   ```
   for p in $(pgrep -f 'karaoke.tui'); do
     ppid=$(ps -o ppid= -p $p | tr -d ' ')
     echo "tui=$p parent=$ppid -> $(tr '\0' ' ' < /proc/$ppid/cmdline | cut -c1-120)"
   done
   ```
   Parent `1`/systemd = orphan from an old session (one had been up since Sep 8).
   Parent `scripts/web_serve.py` = live Web TUI child; keep the ones in use.
3. Leave the importer running unless the user asks otherwise.
4. Verify the service survived the cleanup:
   ```
   systemctl --user is-active karaoke-webtui.service
   curl -fsS -o /dev/null -w 'webtui_http=%{http_code}\n' http://127.0.0.1:8001/
   ```

## Measured result

```
                before      after
load            17.25       11.70
avg cpu freq    609 MHz     2265 MHz     (3.7x)
PPT             21.65 W     26.00 W
```

Ingestion throughput improved as a side effect, measured from the scan's own
position line:

```
during contention:  31 files / 284s  =  9.16 s/file
after cleanup:      60 files / 358s  =  5.97 s/file    (~1.5x)
```

Casting recovered. `Tctl` stayed near 100 C with the importer still at 237%,
which is expected on this chassis and not itself the fault.

## Permanent fix applied

`~/.hermes/scripts/karaoke-vector-refresh.py` now:

- passes `--threads 4 --chunk-size 500` instead of taking every core;
- checks `ps -eo args` for `scripts/overnight_collection_scan.py` and
  `scripts/retry_skipped_collection.py`, and exits 0 with
  `KARAOKE VECTOR REFRESH SKIPPED ... reason=ingestion-running`.

Verified by running the script manually during ingestion — it skipped cleanly.

## ETA pitfall worth repeating

`count(*) FROM tracks` is **not** a progress signal. It read `+1 in 60s` while
the scan was running at full speed, because that stretch of the archive was
resolving to already-existing tracks. Use `Folder scan N/TOTAL` from the app log
and derive `sec_per_file` from two timestamps. The app log rotates
(`karaoke.log.1`, `.2`, ...), so pick the active file by mtime and grep a
`tail -n` slice rather than scanning megabytes repeatedly.

Also: a long-running importer keeps the code it started with. A logging fix
patched mid-run does not reach the live process, and its traceback will point at
the *new* file's line numbers (potentially a comment line) because linecache
reads the updated file from disk. Judge liveness by observed progress, not by
traceback text.
