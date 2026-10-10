# TUI wildcard queue and reactive mood visuals

## Wildcard queue behavior

The TUI uses `*` on startup to populate the playable queue. Treat this as browse-all mode: apply mood/genre filters first, then shuffle the resulting rows with a fresh random source. Do not let the database's `ORDER BY artist, title` leak into the user-visible wildcard queue. Keep the normal sort selector available for non-wildcard searches; the default sort should be mood/energy rather than key when the user wants a vibe-first browse.

## Mirrored ASCII duet

The existing cartwheel animation reads more like dancing than static stick poses. Build the duet by rendering two copies of the existing frame sequence: mirror the right copy, move the left dancer rightward and the right dancer leftward, hold them together for a short musical phrase, then send them outward again. Use a phrase state machine (approach, together, retreat, apart), not a one-frame proximity toggle.

Keep the renderer edge-safe: clamp both figure bounding boxes to the panel width and allow slight overlap on narrow panels rather than clipping. Preserve a constant row count so the UI does not jump when a dancer hops. Gate hopping at `bpm >= 100`; slower tracks should remain grounded.

Use stored analysis fields as stable motion proxies: `energy`/loudness for bass-weighted motion on the left and spectral `brightness` for high-tone/quick motion on the mirrored right. Pass both from `KaraokeTui._render_visuals`. Genre can modify frame cadence, while tender/love sentiment can trigger the together phase. Test narrow widths, below-100-BPM grounding, phrase interaction, and unequal bass/brightness inputs.
