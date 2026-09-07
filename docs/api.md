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

## `GET /api/workers/status`

Celery worker, queue and Flower dashboard status. Allows monitoring the
post-processing pipeline without screen-scraping the TUI.

```json
{
  "orchestrator": "celery",
  "available": true,
  "dashboard_url": "http://127.0.0.1:5555",
  "workers_active": 1,
  "queue_depth": 0,
  "queue_details": {
    "name": "karaoke-postprocess-celery",
    "messages": 0,
    "messages_ready": 0,
    "messages_unacknowledged": 0,
    "consumers": 1
  },
  "worker_details": [
    {
      "unit": "karaoke-celery-worker.service",
      "active": "active",
      "running": true
    }
  ]
}
```

The `orchestrator` field indicates the active backend (`celery` or `legacy`).
`available: false` means RabbitMQ is unreachable or no workers are running; a
`reason` field is also present on failure. CPU and memory metrics are no
longer exposed via this API directly.

### `POST /api/workers/scale`

Scales the Celery worker pool (starts/stops `karaoke-celery-worker.service`).

Request body:

```json
{
  "target": 1
}
```

`target` (integer): Desired number of active workers. `0` stops the worker;
`1` (or higher) starts it. Returns `200 OK` on success.
