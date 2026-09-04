# Karaoke TODO & Roadmap

This page tracks planned features, ongoing enhancements, and accuracy improvements for the Karaoke platform.

## Active & Upcoming Tasks

- [x] **Phase 1: Line Duration Capping** — Prevent stale line highlighting during long instrumental gaps.
- [x] **Phase 2: Learn Mode** — Keyboard shortcuts (`e` for line-end, `SPACE` for word tapping) to author Enhanced LRC.
- [x] **Phase 3: Real Word Timings** — Extract and backfill per-word timings from YouTube `json3` caption payloads (`json3_to_enhanced_lrc`).
- [x] **TUI Word-Level Playhead Rendering** — word highlight tracks real
  Enhanced-LRC timings, interpolating where only line timings exist.
- [ ] **Beat-driven visuals** — the left column reserves space (`#beat-art`,
  currently the cover art). Sample video frames on beat, or generate art from
  the line's sentiment. See `docs/tui.md`.
- [ ] **Old: TUI Word-Level Playhead Rendering** — Render real word-level Enhanced LRC timestamps in the Textual TUI player.
- [ ] **Automated OpenSearch Backfill Cron** — Background worker to sync new SQLite tracks and lyric lines into OpenSearch indices automatically.
- [ ] **Whisper GPU Acceleration Tuning** — Optimize local Whisper transcription worker configuration for faster offline fallback.

---

## Completed Milestones

- OpenSearch hybrid BM25 + kNN vector search integration.
- YouTube caption probing and extraction (`json3`, `srv3`, `vtt`, `ttml`, `srt`).
- LRCLIB exact and fuzzy fallback matching with title-cleaning (`clean_title`).
- FastAPI control API and CLI client integration.
