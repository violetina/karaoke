# Terminal duet animation: responsive rendering

Use this when extending the karaoke TUI's ASCII dance/visual panel.

## Design pattern

- Keep the existing beat-driven figure readable; add interaction as choreography rather than replacing it with unrelated poses.
- For mirrored dancers, define a fixed phrase in beats (for example: 8 beats approach, 8 together, 8 retreat, 8 apart).
- Use stored audio-analysis values as stable visual drivers: `energy` is a useful bass/weight proxy and `brightness` (spectral centroid) is a useful high-tone/quickness proxy. Give each dancer an independent frame phase so they do not look mechanically identical.
- Preserve the slow-song rule: do not add vertical hops below 100 BPM; continue horizontal/pose motion while grounded.
- Sentiment and genre should modulate the interaction: tender/love can trigger a temporary together phase; genre can change cadence or pose family.

## Critical layout rule

The renderer must use the actual `#ascii-visual` content width. Never emit a hard-coded wider frame into a narrower Textual `Static`; Textual wraps it and makes one figure appear at an edge while the other appears on the opposite edge.

Before composing two figures:

1. Compute the maximum pose width.
2. If the panel cannot hold two complete poses plus a centre gap, render one compact complete dancer (or another explicit narrow fallback).
3. For the normal case, assign ownership boundaries: left dancer may draw only at columns `< width // 2`, right dancer only at columns `>= width // 2`.
4. Drop colliding characters instead of overwriting/merging them. Clamp both pose boxes to `[0, width)`.
5. Keep a constant number of output rows so hopping does not make the surrounding TUI jump.

## Verification

- Add visual tests for: below-100 BPM grounded output, together phase, independent bass/treble phases, narrow fallback, and every output line staying within the requested width.
- Run the targeted visual/TUI tests and then the full `.venv/bin/python -m pytest` suite.
