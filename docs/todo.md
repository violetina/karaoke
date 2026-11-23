# Karaoke TODO & Roadmap

Planned features, in-progress work, and accuracy improvements for the Karaoke
platform. Grouped by state so the next thing to pick up is obvious. Checked
items are kept for a release or two for context, then rolled into
**Shipped milestones** at the bottom.

> [!tip] Keeping this current
> This page is the doc source of truth (`docs/todo.md`). It renders into the
> Quartz wiki via `make wiki`. When you finish something, move it from
> **In progress** → **Recently shipped**, and prune **Recently shipped** into
> **Shipped milestones** each release.

## 🔨 In progress

- [ ] **AI DJ live-queue control (MCP)** — `player_control`, `get_live_queue`,
  `queue_next_in_player`, `remove_from_dj_playlist`, `reorder_dj_playlist` MCP
  tools writing through `active_queue_state`; `collection.enqueue_tracks` and
  saved-playlist move/remove in `localcache`. *(uncommitted on `main`)*
- [ ] **Stage-view v2 timeline** — chord/mood/lyric timeline integration in
  `stage_view.py`. *(uncommitted on `main`)*

## 🎯 Planned — next up

- [ ] **Beat-driven visuals** — the TUI left column reserves `#beat-art` (cover
  art today). Sample video frames on the beat, or synthesize art from the
  line's sentiment. See [[tui]].
- [ ] **Automated OpenSearch backfill cron** — background worker that syncs new
  PostgreSQL tracks and lyric lines into the OpenSearch indices automatically,
  instead of manual `make vector-index` / reconcile runs.
- [ ] **Whisper GPU acceleration tuning** — optimize the local Whisper
  transcription worker for faster offline-fallback lyric timing.

## 💡 Backlog / ideas

- [ ] **Mood-still in the stage/web view** — surface the new `GET /api/mood-still`
  renders live on the dashboard, advancing with `elapsed`. See [[control-api]].
- [ ] **Per-word playhead everywhere** — extend real Enhanced-LRC word timing
  beyond the TUI into the web prompter.

## ✅ Recently shipped

- [x] **Mood-still API (1.10.0)** — `GET /api/mood-still` renders a feature-driven
  1920×1080 PNG from the live track's key/BPM/energy/mood/chord, advancing with
  `elapsed`. New `aggressive` / `bluesy` moods. See [[control-api]].
- [x] **Multi-provider AI DJ chat (1.9.0)** — Claude / GPT / Gemini / Ollama via
  `karaoke.ai_providers`. See [[howto-configure-ai-providers]].
- [x] **Release years + decade playlists (1.8.0)** — per-album MusicBrainz year
  lookup, `Decade:` smart lists.
- [x] **TUI word-level playhead** — word highlight tracks real Enhanced-LRC
  timings, interpolating where only line timings exist.
- [x] **Phase 1–3 lyric timing** — line-duration capping, Learn Mode authoring
  (`e` / `SPACE`), and real per-word timings from YouTube `json3`
  (`json3_to_enhanced_lrc`).

## 🏛️ Shipped milestones

- OpenSearch hybrid BM25 + kNN vector search.
- YouTube caption probing/extraction (`json3`, `srv3`, `vtt`, `ttml`, `srt`).
- LRCLIB exact + fuzzy fallback matching with title cleaning (`clean_title`).
- FastAPI control API and CLI client integration.
