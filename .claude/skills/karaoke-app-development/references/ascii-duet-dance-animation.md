# ASCII Duet Dance Animation

Use this pattern when extending the karaoke TUI's beat-synchronised terminal visuals.

## Choreography

- Keep the existing cartwheel/figure frames when they already read as dancing; add a mirrored second figure instead of replacing the motion with static poses.
- Use a phrase state machine rather than independent random movement. A reliable 32-beat phrase is:
  - beats 0–8: left figure travels right and right figure travels left
  - beats 8–16: both remain together for two 4/4 bars
  - beats 16–24: both retreat
  - beats 24–32: dance apart
- Use a smoothstep interpolation for distance changes so the meeting is intentional and not a sprite snap.
- Mirror ASCII safely with a helper that swaps `/` and `\\`, then reverses the string. Preserve fixed frame height so the surrounding Textual layout does not jump.

## Musical inputs

- Drive phase from `beats = elapsed / (60 / bpm)` so the visual remains deterministic despite timer jitter.
- Treat missing/zero BPM as a grounded fallback tempo.
- Disable vertical hops below 100 BPM; slow tracks should sway/roll without jumping.
- Use the aggregate lyric sentiment (`happy`, `sad`, `angry`, `tender`) and track energy as behavioral inputs. Tender/love material can trigger the together phase and a heart marker; energy can widen the apart distance.
- Let genre alter cadence or pose vocabulary while retaining the same interaction choreography. Examples: angular rock/metal/punk, quicker electronic/dance, groove-oriented hip-hop/soul.

## Integration and tests

- Keep pure frame generation in `karaoke.visuals`; have `KaraokeTui._render_visuals` pass BPM, elapsed time, dominant mood, genre, and energy.
- Test deterministic output, distance/meeting state, genre variants, and the no-hop rule below 100 BPM.
- Preserve the old single-figure renderer if existing tests or other callers use it, but render the duet in the live TUI when the new behavior is requested.
- Full verification is `PYTHONPATH=src .venv/bin/python -m pytest` (or the project’s equivalent); do not rely on visual claims without exercising the renderer/tests.
