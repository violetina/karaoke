# TUI Beat-Synced ASCII Animation & Bulk Audio Analysis

## 🤸 On-the-Beat ASCII Animations (Textual TUI)
To render highly responsive animations synchronized with playback position, map the track's BPM and playhead position (`elapsed`) to the current animation frame:

1. **Calculate Beat Progress:** Convert the current track's BPM to seconds-per-beat (`beat_duration = 60.0 / bpm`). The relative progress of the current beat is `progress = (elapsed % beat_duration) / beat_duration`, returning a value smoothly scaling from `0.0` to `1.0`.
2. **Smooth Frame Mapping:** Divide the 0..1 interval evenly across your animation sequence:
   ```python
   # 9-frame cartwheel sequence matching the standard cartwheel ASCII art
   _CARTWHEEL_FRAMES = [
       ["   o   ", "  /|\\  ", "  / \\  "],
       [" \\ o / ", "   |   ", "  / \\  "],
       ["  _ o  ", "   /\\  ", "  | \\  "],
       ["   __\\ ", " ___\\o ", " /)  | "],
       ["  __|  ", "  \\o   ", "  ( \\  "],
       ["  \\ /  ", "   |   ", "  /o\\  "],
       ["  |__  ", "   o/  ", "  / )  "],
       ["  o _  ", "  /\\   ", "  / |  "],
       ["   o   ", "  /|\\  ", "  / \\  "]
   ]
   frame_idx = int(progress * len(_CARTWHEEL_FRAMES)) % len(_CARTWHEEL_FRAMES)
   frame_lines = "\n".join(_CARTWHEEL_FRAMES[frame_idx])
   ```
3. **Dynamic Visual Sidebar:** Push these animated frames into the visuals Static widget at 10fps. Since the TUI has full CSS capabilities, keep `#ascii-visual` at `height: 1fr` so it grows dynamically to accommodate the 3-line animation on larger terminals without overlapping surrounding panels.

---

## 📐 Textual Layout Swapping & Subtitle Countdowns
- **Lyrics Priority:** Position the scrolling lyrics panel (`#lyrics`) directly under the title panel (`#now-playing`) and above the browser DataTable (`#library`) inside the main layout vertical container.
- **Lyrics Border Subtitle Countdown:** Use Textual's `.border_subtitle` property on the Static `#lyrics` widget to render a live-updating lyric countdown (e.g., `border_subtitle = f"next in {nxt - elapsed:0.1f}s"`).
- **Stale Countdowns Cleanup:** Ensure this border subtitle is explicitly set to `None` when the player is idle, on track change, or during a total lyrics-miss to prevent stale countdown numbers from remaining on the panel's border.

---

## 🧪 Bulk Audio Analysis & Database Integration
When you need to analyze all cached YouTube audio files and register their musical metrics in the database:

1. **Resolve Database track_id:** Walk your local cache folder (e.g. `~/.local/share/karaoke/youtube/`), parse the filename stem as the YouTube video ID (`vid_id`), and match it to a `sources` table URL record (`https://www.youtube.com/watch?v={vid_id}`) to resolve the canonical `track_id`.
2. **Filter already analyzed:** Skip tracks that already have rows inside the `track_analysis` table to avoid redundant CPU overhead.
3. **Analyze and store:** Execute `analyze_audio` via `librosa`/`essentia` to get metrics (detected key, confidence, agreement, BPM, energy, brightness), then call `track_analysis.save_detected()` to record the values in SQLite.
