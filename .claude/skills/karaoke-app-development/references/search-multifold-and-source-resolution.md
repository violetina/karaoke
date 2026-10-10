# Search Tokenization & Dynamic Source Resolution Design Patterns

This reference document records key architectural patterns and debugging lessons learned for the `karaoke` platform's library search, browser CDP navigation, and background worker task flows.

## 1. Multi-Word & Cross-Field Library Search Fallbacks
Standard SQL or substring-based queries are highly strict. A query like `"Houses Holy"` fails against `"Houses of the Holy"` because of the missing `"of the"`. Similarly, `"Led Zeppelin Houses Holy"` fails because no single field (`artist` or `title`) contains all the words.

### Design Pattern:
1. **Contiguous Phrase Match (Primary):** Try the original substring match first to preserve exact-match priority and weights.
2. **Tokenized Word Coverage (Field-Level Fallback):**
   - Split the normalized query into words.
   - Filter out small common stop words (e.g., `of`, `the`, `a`, `in`, `and`).
   - Calculate how many query words exist in the field as whole words (using regex word boundaries `\b`).
   - Score the field by scaling down the base weight by the word coverage ratio:
     $$\text{Score} = \text{WORD} \times \frac{\text{Matched Words}}{\text{Meaningful Words}} \times \text{Multiplier}$$
3. **Cross-Field Tokenized Coverage (Full-Track Fallback):**
   - If individual fields yield `0.0` score, combine all track metadata into a single string (`f"{artist} {title} {album}"`).
   - Calculate word coverage across this combined string.
   - If a match is found, attribute the match to any contributing fields that matched individual words, and return a scaled, cross-field score.

---

## 2. Dynamic YouTube Source Resolution & Auto-Healing
When a post-processing or transcription task is triggered on a track that only has a Spotify source or is entirely missing from the SQLite database, failing immediately with a "Track not found" error degrades the TUI experience.

### Design Pattern:
* **Task-Level Auto-Creation:** If the background orchestrator (`resolve_track_id` task) receives a payload with no matching SQLite record, it searches YouTube for `"{artist} - {title}"`, creates the track, registers the found video as a `youtube` source, and proceeds.
* **Source Healing Fallback:** If the track exists but has no playable/downloadable source URL, the background task searches YouTube, inserts the found link into the SQLite `sources` table, and continues syncing.
* **Smart Enqueue Gating:** In the TUI, do not block enqueuing of Spotify-only tracks if they have plain lyrics needing alignment. Bypass the downloadable-source check so Celery can run, automatically resolve a YouTube mirror, download the audio, align the timings, and save the synced LRC back to SQLite.

---

## 3. Chrome CDP Navigation & Special Characters
When driving Google Chrome or Chromium kiosk windows via the Chrome DevTools Protocol (CDP) `Page.navigate` fallback evaluating `Runtime.evaluate`, search URLs and titles containing single quotes (e.g. `'` in `Aunt Cynthia's Cabin`) will break the enclosing JavaScript string literal:
```javascript
window.location.href = 'https://music.youtube.com/search?q=Aunt+Cynthia's+Cabin'; // SYNTAX ERROR!
```
This syntax error silently crashes the CDP evaluation channel, causing the app to fall back to launching standard OS browser tabs and breaking the kiosk flow.

### Fix:
Always escape single quotes in parameters before formatting them into JavaScript evaluation blocks:
```python
escaped_url = url.replace("'", "\\'")
reply_js = _cdp_send(
    "Runtime.evaluate",
    {"expression": f"window.onbeforeunload = null; window.location.href = '{escaped_url}';"}
)
```

---

## 4. Synonym-Backed BM25 Keyword Search & Vocab Expansion
To solve vocabulary mismatch issues in full-text keyword search (e.g., searching for "gloom" but lyrics saying "sad"), we employ a **synonym expansion analyzer** in OpenSearch:
* **The Synonym Analyzer (`synonym_analyzer`):** Uses standard tokenization, lowercase, and a custom synonym filter.
* **Thematic Synonym Buckets:** Configured across six specific emotional, genre, and acoustic dimensions:
  1. *Rock/Metal:* `rock, stoner rock, psychedelic rock, punk rock, post-punk, grunge, hard rock, heavy metal, metal`
  2. *Sad/Melancholy:* `sad, depressed, melancholy, blue, sorrow, gloom, tearful, crying, weeping, lonely, dark, tender`
  3. *Happy/Energetic:* `happy, glad, joyful, upbeat, cheerful, bright, high energy, energetic`
  4. *Chill/Calm:* `chill, mellow, relax, relaxing, calm, slow, down, ambient`
  5. *Hip-Hop/Soul:* `hip hop, rap, trap, r and b, rnb, soul, funk`
  6. *Electronic/Dance:* `techno, electronic, electronica, electro, synth, synthesizer, beats, drum and bass, dnb, dance, house, rave, club, party, EDM`
* **Schema Mapping:** Applied to `title`, `artist`, `album`, and `plain_lyrics` in the primary index, as well as line-level and note-level text fields. This automatically expands keyword queries at search time for richer BM25 matches.

---

## 5. Host Service & Kiosk Browser Lifecycle Management via systemd & Admin TUI
Transitioning local GUI tools (like Kiosk Chrome) and processing pools from raw, background shell spawns to formal `systemd --user` units brings robust lifecycle management and centralized control:

### Design Pattern:
* **Decoupled Playback UI:** Spawning the Chrome kiosk playback window is completely detached from client launch commands (such as `make tui`). Instead, it is managed as a systemd user unit `karaoke-kiosk.service` bound to the core `karaoke.target` umbrella.
* **Admin TUI as Unified Operator Panel:** The Admin TUI (`make admin`) monitors background workers and connection channels periodically over HTTP. If the API is unreachable, it falls back to direct, non-blocking `systemctl --user is-active` shell-outs.
* **Robust Service Restart/Management:** Control triggers (such as `K` to restart Kiosk Chrome) send requests to `ctrl_api` which wraps a dual-mode restart mechanism:
  1. It first triggers `systemctl --user restart karaoke-kiosk.service` so systemd manages the lifecycle cleanly.
  2. If the systemd unit is uninstalled/unreachable, it falls back to finding and killing active Chrome CDP processes (`port :9222`) before launching a fresh, isolated background browser instance.
* **Code Reloading:** Python editable installs running under systemd do not auto-reload on python file edits. Running code changes require explicitly calling `systemctl --user restart` on the respective services.
