# How-To: systemd services & health checks

The karaoke platform has several host-side pieces that must all be running for
"unified playing" to work. Instead of juggling terminals, this ships a set of
**systemd `--user` units** so everything starts together, restarts on failure,
and logs to **journald**. A health-check service + timer verifies the whole
stack every 5 minutes.

## Services

| Unit | What it does | Port |
|---|---|---|
| `karaoke-api.service` | FastAPI library backend (read-only tracks/lyrics/stats) | 8000 |
| `karaoke-ctrl-api.service` | Host-side playback control API (opens browser/Spotify) | 8765 |
| `karaoke-mq-forward.service` | `kubectl port-forward` for in-cluster RabbitMQ | 5672 / 15672 |
| `karaoke-postprocess@1..6.service` | Six competing post-processing workers (key/BPM + word-timing) | — |
| `karaoke-postprocess.slice` | Shared CPU/memory cap for all worker instances | — |
| `karaoke-healthcheck.service` | One-shot health probe (run by the timer) | — |
| `karaoke-healthcheck.timer` | Fires the health check every 5 min | — |
| `karaoke.target` | Umbrella — start/stop all of the above at once | — |

All units live in `deploy/systemd/` and are symlinked into
`~/.config/systemd/user/` by `make systemd-install` (so edits to the repo files
take effect after `systemctl --user daemon-reload`).

## Install & run

```bash
make systemd-install   # symlink units, daemon-reload, enable target + timer
make systemd-up        # start everything
make systemd-status    # unit status + last health check
make health            # run the health check right now (ad-hoc)
make systemd-down      # stop everything
make systemd-uninstall # stop + remove the units
```

`make systemd-install` also `enable`s `karaoke.target` and the timer, so the
platform comes back after a reboot/login. (For it to run without you being
logged in, enable lingering once: `loginctl enable-linger $USER`.)

## Post-processing runners

`karaoke.target` starts `karaoke-postprocess@1.service` through
`karaoke-postprocess@6.service`. Every instance consumes the same RabbitMQ queue
with `prefetch_count=1`, so RabbitMQ hands one track to each free worker; there
is no app-level coordination to maintain.

The workers share `karaoke-postprocess.slice`, currently capped at `CPUQuota=600%`
with low CPU/IO weight. That makes worker count a throughput knob without letting
background analysis starve browser playback. The worker process now also
reconnects to RabbitMQ with bounded backoff if the port-forward/broker drops, so
systemd does not have to restart it for routine AMQP blips.

Useful controls:

```bash
systemctl --user start karaoke-postprocess@{1..6}
systemctl --user stop 'karaoke-postprocess@*'
systemctl --user status karaoke-postprocess.slice
```

## Logs (journald)

Every service logs to the journal under its `SyslogIdentifier`:

```bash
journalctl --user -u karaoke-api -f                 # follow the library API
journalctl --user -u 'karaoke-postprocess@*' -f     # follow every worker
journalctl --user -u karaoke-postprocess@3 -f       # follow one worker instance
journalctl --user -u karaoke-healthcheck -n 20      # recent health reports
journalctl --user -u 'karaoke-*' --since '10 min ago'  # everything, last 10 min
```

## The health check

`scripts/healthcheck.py` verifies each moving part and prints a compact report.
**Required** checks failing make the unit exit non-zero (so
`systemctl --user is-failed karaoke-healthcheck` and the journal both flag a
degraded platform); **optional** checks only warn.

| Check | Required? | Notes |
|---|---|---|
| library-api `:8000/health` | ✅ | Core read API. |
| control-api `:8765/health` | ⛔️ opt | Needs a desktop session. |
| rabbitmq-amqp `:5672` | ✅ | Needs `karaoke-mq-forward` up. |
| rabbitmq-mgmt `:15672` | ⛔️ opt | Management UI. |
| kind pods `Running` | ✅ | `karaoke` namespace. |
| kiosk-chrome CDP `:9222` | ⛔️ opt | Unified player window. |
| sqlite-db | ✅ | Opens the DB + counts tracks. |

At boot the timer may fire before ports finish binding, so the check retries the
whole sweep a few times (env-tunable: `KARAOKE_HEALTH_RETRIES`,
`KARAOKE_HEALTH_RETRY_DELAY`) before reporting `DEGRADED`.

Sample output:

```
karaoke health: HEALTHY
  [✓] library-api    (req)  http://127.0.0.1:8000/health
  [✓] control-api    (opt)  http://127.0.0.1:8765/health
  [✓] rabbitmq-amqp  (req)  localhost:5672
  [✓] rabbitmq-mgmt  (opt)  http://127.0.0.1:15672
  [✓] kind-pods      (req)  2 pod(s) Running
  [✓] kiosk-chrome   (opt)  CDP :9222
  [✓] sqlite-db      (req)  348 tracks
```

## Configuration

Units read the same env vars as the app; override per-unit with
`systemctl --user edit <unit>` drop-ins, or globally via the unit files:

| Var | Default | Used by |
|---|---|---|
| `KARAOKE_API_PORT` | `8000` | library API |
| `KARAOKE_CTRL_PORT` | `8765` | control API |
| `RABBITMQ_HOST` | `localhost` | worker |
| `KARAOKE_COOKIES_FROM_BROWSER` | `firefox` | worker (YouTube auth) |
| `KUBE_CONTEXT` | `kind-karaoke` | port-forward, health check |
| `K8S_NAMESPACE` | `karaoke` | port-forward, health check |

## Pitfalls

- **Port already in use:** if you previously ran `make api` / `make mq-port-forward`
  in a terminal, stop those first — systemd can't bind a port another process
  holds. `ss -lntp | grep -E ':8000|:8765|:5672'` shows the culprit.
- **`karaoke-mq-forward` restarts a lot:** expected if the kind cluster or the
  `karaoke` namespace isn't up yet. Bring the cluster up first
  (`kubectl --context kind-karaoke get ns karaoke`).
- **Health check fails right after `systemd-up`:** the boot-time run can race the
  ports; the built-in retry usually clears it. Re-run `make health` to confirm.
- **Nothing runs after reboot without a login:** enable lingering once with
  `loginctl enable-linger $USER`.
