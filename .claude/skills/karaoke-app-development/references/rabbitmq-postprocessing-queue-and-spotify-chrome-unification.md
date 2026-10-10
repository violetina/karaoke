# RabbitMQ Post-Processing Queue & Spotify/Chrome Player Unification

This reference guide details the design, configuration, and implementation of the asynchronous post-processing queue (RabbitMQ) and the unified Chrome web player playback pipeline.

## 1. Asynchronous Post-Processing Queue (RabbitMQ)

Some derived metadata and assets (audio key/tempo analysis, word-level Enhanced LRC upgrades) are too slow to compile inline while a user is actively starting or playing a song. These are offloaded to an asynchronous work queue.

### Architecture

```
[ TUI / scan mode ] --(enqueue_if_needed)--> [ RabbitMQ (kind cluster) ]
                                                        |
                                             (Basic.Deliver / Ack)
                                                        v
                                        [ postprocess_worker (Host-Side) ]
                                              - download audio
                                              - analyze key/BPM
                                              - upgrade timings
                                              - write back to SQLite
```

- **Broker (In-Cluster):** A standard `rabbitmq:3-management` broker is deployed in the `karaoke` namespace of the `kind-karaoke` cluster. It is configured without persistent volumes; if the broker resets, the queue is re-filled from SQLite with `make postprocess-enqueue-all`.
- **Worker (Host-Side):** The consumer (`karaoke-postprocess-worker`) runs on the **host machine** instead of inside the cluster. It requires access to the host's audio downloads cache (`~/.local/share/karaoke/youtube/`), the SQLite database, and the heavy audio analysis dependencies (essentia, librosa, faster-whisper) which are excluded from the slim cluster API image.
- **TUI Enqueue Gating:** When a track is loaded, `postprocess_queue.enqueue_if_needed()` checks if the track has missing key/BPM row or line-level-only lyrics (`needs_postprocessing(track_id)`). It only publishes a message when work is actually needed.
- **Silent Fallback:** Publishing runs in a background thread (`run_worker(..., thread=True)`). If the RabbitMQ broker is down or unreachable, it fails silently as a no-op, preventing any app hangs or crashes.

---

## 2. Unifying Playback in the Chrome CDP Player

To prevent window clutter and make playback easy to control, all audio sources (YouTube, YouTube Music, and Spotify) are unified to run inside the same browser window.

### Pausing Active Playback
Before launching any new media URL, `open_song_url` queries `playerctl --list-all` and commands all active media players to `pause` so that newly opened tracks do not overlap audio.

### Spotify Web Player Fall-Through
Instead of calling `playerctl open` (which forces launching the native Spotify desktop app on the host), any Spotify track URI (`spotify:track:{id}`) is automatically converted on-the-fly to its corresponding HTTPS Web Player URL:
```python
if url.startswith("spotify:track:"):
    track_id = url.split(":")[-1]
    url = f"https://open.spotify.com/track/{track_id}"
```
The track is then allowed to fall through directly to the Chrome player block (`try_chrome_cdp_navigate` or `xdg-open`), which opens it as a tab in your dedicated browser window. You can then control it using standard browser MPRIS controls!
