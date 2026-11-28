# Choosing the AI DJ model (benchmark)

The dashboard AI DJ booth (`POST /api/dj/chat`, :8900) answers questions about the
song on stage: key and BPM, mood, chords, the band, and what to play next. It runs on a
local Ollama model. `scripts/bench_dj_models.py` measures which model does this best on
this machine. It goes through the real DJ path (retrieved facts, system prompt, SSE
stream) and scores every answer against the library, not by eye.

## Recommended model

**`qwen2.5:3b`** is the default since 1.12.0. On one song per smart playlist (58 songs,
5 questions each) it answered 100% of the questions correctly, with a median of 2.6 s for
a full answer and 2.2 GiB of GPU memory.

| model | facts correct | first word | full answer | GPU memory |
|---|---|---|---|---|
| **qwen2.5:3b** | **100%** | 1.8 s | **2.6 s** | 2.2 GiB |
| qwen3:0.6b | 100% | 3.0 s | 3.4 s | 1.3 GiB |
| llama3.2:3b | 98% (suggest 92%) | 1.7 s | 3.0 s | 2.9 GiB |

A 12-song screen of lesser-known models (same run, `qwen2.5:3b` as baseline) found
nothing better:

| model | facts correct | full answer (vs qwen2.5:3b 5.9 s in that run) |
|---|---|---|
| granite4:micro-h, granite3.3:2b | 100% | ~11 s |
| phi4-mini:3.8b | 99% | 8.8 s |
| gemma3:4b | 99% | 17.7 s |
| gemma3:1b | 76% (mood 42%) | 3.4 s |

Not yet screened: `ministral-3:3b`, `qwen3:4b-instruct`, `hermes3:3b`, `smollm2:1.7b`,
`llama3.2:1b`. Avoid "thinking" models (`qwen3` base tags, `deepseek-r1`,
`lfm2.5-thinking`) for a live DJ: they spend 10-70 s before the first visible word, and the 8B ones take about a minute per answer.

### Setting the default

```bash
# persistent (survives restarts)
mkdir -p ~/.config/systemd/user/karaoke-dashboard.service.d
printf '[Service]\nEnvironment="KARAOKE_DJ_MODEL=qwen2.5:3b"\n' \
  > ~/.config/systemd/user/karaoke-dashboard.service.d/dj-model.conf
systemctl --user daemon-reload && systemctl --user restart karaoke-dashboard.service

# runtime only (the dashboard's model dropdown does the same)
curl -X POST http://127.0.0.1:8900/api/dj/models -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:3b"}'
```

## Running the benchmark

```bash
make bench-dj MODELS="qwen2.5:3b llama3.2:3b"              # one song (TRACK=74 by default)
make bench-dj-smart MODELS="qwen2.5:3b gemma3:4b" MAX=12  # quick screen: 12 songs spread over smart playlists
make bench-dj-smart MODELS="qwen2.5:3b gemma3:4b"         # full: one analysed song per smart playlist
.venv/bin/python scripts/bench_dj_models.py --rescore <run>.json   # re-score a saved run, no model calls
```

`--smart-playlists [REGEX]` picks, per smart playlist, a track that has key, BPM, a mood
timeline and chords (preferring an artist with a member list); each track and artist is
used once. Reports land in `~/.local/share/karaoke/bench/dj-models-<stamp>-<label>.{md,json}`
and are saved after each model, so an interrupted run keeps its results.

Each model is unloaded before the next, so the cold-load time is separate. During every
answer the script samples CPU %, load, RAM, GPU busy % and GPU memory (VRAM + GTT).

### What is scored

| question | correct when the answer contains |
|---|---|
| key_bpm | the key (enharmonics and the Camelot code accepted) and the BPM within ±3 ("162 BPM", "BPM is a steady 162", or a bare number in a short answer) |
| mood | the dominant feeling or quadrant from `track_moods` |
| chords | 2 of the 6 most-used chords from `track_chords` |
| artist | 2 band members from the artist cache (1 for a single-member act) |
| suggest | exactly one `[QUEUE: Artist - Title]` that is a real library track other than the playing one |

A question with no ground truth in the library (no member list, no chords) scores `n/a`
and is left out of the totals; it is not counted as wrong.

## What the benchmark found in the DJ itself

When several models fail on the **same songs**, the cause is the tooling, not the models.
The benchmark found four such bugs, all fixed in 1.12.0:

- The "current track analysis" fact selected a column that doesn't exist, so it failed
  silently and the model got no key/BPM/chords.
- The uploader-name repair turned "The Slits - Typical Girls" into the band "Girls".
- When the artist had no exact entry in the artist cache, `dj_chat_endpoint` blanked the
  playing artist, so for duos and "feat."/"aka" credits the model was told nothing was
  playing.
- Fuzzy OpenSearch searches on the question text ("after", "song") put unrelated artists
  next to the real recommendations, and small models mixed them into invented picks.

The prompt now leads with the verified facts, skips the fuzzy searches for analysis and
suggestion questions, and lists the recommendations as ready-to-copy
`[QUEUE: Artist - Title]` lines (current song already excluded). With these changes
`qwen3:0.6b` went from 1/5 to 5/5 on the reference song.

## Hardware notes (Framework 13, Ryzen AI 300, Radeon 890M)

- **Power profile:** `performance` boosts the GPU to 2.7 GHz, reaches ~100 °C and
  throttles to ~1.4 GHz within seconds. It is up to 45% slower than `balanced` for
  sustained use. `power-saver` is only 2-4% slower than `balanced` and runs ~12 °C
  cooler.
- **GPU memory:** the models run 100% on the GPU from GTT (shared system RAM, ~15 GiB,
  `ttm.pages_limit`), not from the 512 MiB BIOS VRAM carve-out. Raising GTT to 24 GiB did
  not change tokens/s, because the APU is memory-bandwidth bound. Use `amdgpu_top` to see GTT;
  COSMIC Monitor only shows the carve-out.
- After hours of load the laptop heat-soaks and absolute times can double. Compare speeds
  within one run, and always include the current default as the baseline.

See also [Configure AI providers](howto-configure-ai-providers.md) and
[Karaoke DJ & MCP](mcp-server.md).
