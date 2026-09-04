# API reference

This reference is generated from Python docstrings with `mkdocstrings`.

## CLI

::: karaoke.cli

## Configuration

::: karaoke.config

## Song identification

::: karaoke.identify

## Lyrics and LRC parsing

::: karaoke.lyrics

## Timeline and terminal player

::: karaoke.player

## Search

::: karaoke.search

## Local cache and stats

::: karaoke.localcache

## Scanner and document building

::: karaoke.scanner

## OpenSearch client and index mapping

::: karaoke.osclient

## Embeddings

::: karaoke.embed

## Audio tags

::: karaoke.tags

## Spotify client

::: karaoke.spotify_client

## Spotify import

::: karaoke.spotify_import

## Whisper transcription

::: karaoke.whisper_sync

## Mood tinting

::: karaoke.sentiment

## Beat flash

::: karaoke.beats

## `GET /api/workers`

Post-processing worker and queue statistics, so the pipeline can be monitored
without screen-scraping the TUI.

```json
{
  "available": true,
  "queue":   {"name": "karaoke-postprocess", "ready": 0, "unacked": 0,
              "queued": 0, "consumers": 12, "deliver_rate": 0.0,
              "publish_rate": 0.0, "busy": false},
  "workers": {"count": 12, "running": true, "pids": [509910, "..."],
              "cpu_percent": 0.0, "rss_mb": 674.6}
}
```

CPU and memory are **summed across every worker** — they scale horizontally
(`systemctl --user start karaoke-postprocess@{1..12}`), so one worker's usage
would understate the fleet.

Best-effort by design: if RabbitMQ is unreachable, or the workers run on
another host (in a container `/proc` shows none of them), the response is
`200` with `available: false` and a `reason`. A broker outage cannot take the
library endpoints down with it.
