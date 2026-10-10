# TUI Mood Slider, Sort Options, Genre/Feeling & Playlist Queue Controls

This document details the TUI energy mood slider, multi-attribute sorting, genre/feeling valence-arousal mapping, and playlist queue management in the `karaoke` application.

---

## 1. Interactive Mood Energy Slider (`-`/`+` keys)

- **UI Widget:** `#mood-slider` Static widget in the sidebar sitting directly under `#search-input`.
- **Display Format:** `energy ≥ 30% [███░░░░░░░] -/+`
- **Behavior:** Pressing `-` or `+` decreases or increases `self._mood_level` in 10% steps ($0.0 \dots 1.0$).
- **Filtering:** Filters both the Library table and search results in real time (`energy >= self._mood_level`).

---

## 2. Multi-Attribute Sorting (`SORT_OPTIONS`)

- **UI Widget:** `#sort-select` Select widget in the sidebar.
- **Sort Modes:**
  - `Artist / Title (A-Z)` (`artist`): Preserves SQLite `ORDER BY t.artist, t.title`.
  - `🔥 Energy (high→low)` (`energy_desc`): Sorts by computed track energy descending.
  - `🌙 Energy (low→high)` (`energy_asc`): Sorts by computed track energy ascending.
  - `🥁 BPM (fast→slow)` (`bpm_desc`): Sorts by track BPM descending.
  - `🎹 BPM (slow→fast)` (`bpm_asc`): Sorts by track BPM ascending.
  - `🎵 Key` (`key`): Sorts by detected harmonic key.

---

## 3. Genre & Valence/Arousal Feeling Glyph (`feeling_glyph`)

- **Valence/Arousal Mapping:**
  ```python
  def feeling_glyph(energy: float, brightness: float | None = None) -> str:
      b = 0.5 if brightness is None else brightness
      if energy >= 0.66:
          return "🔥" if b < 0.5 else "☀"
      if energy >= 0.4:
          return "⚡" if b >= 0.5 else "🌗"
      return "♡" if b >= 0.5 else "🌙"
  ```
- **Library Table Column (`Genre/Feel`):** Combines CLAP `track_genre` label with feeling glyph (e.g. `hip hop 🌙`, `synth-pop ⚡`, `metal 🔥`).

---

## 4. Playlist & Queue Controls

- **`a` (Enqueue Track):** Appends highlighted track from search/library to the end of `self._queue` without interrupting current playback.
- **`U` (Shuffle Upcoming):** Shuffles remaining unplayed items in `self._queue[self._queue_at + 1:]`.
- **`C` (Clear Queue):** Clears the active queue and hides `#queue`.
- **`o` (Toggle Auto-Advance):** Toggles `play-once` mode (`ON`: queue auto-advances, `OFF`: site radio takes over).
