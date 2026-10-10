# Documenting the karaoke project (MkDocs)

Docs live in `docs/` and are built with MkDocs (`make docs` → `site/`,
`make docs-live` to serve). Config is `mkdocs.yml` (plus `mkdocs-confluence.yml`
for the Confluence export and `src/karaoke/mkdocs.yml` for the mkdocstrings API
tree, included via the monorepo plugin).

## Established structure / conventions
- **One page per topic**, kebab-case filenames (`flows.md`, `tui-visuals.md`).
- **Per-mode pages** live under `docs/modes/` with an `index.md` overview table;
  the TUI modes are scan, spotify, browse, radio, record, sample.
- **Flow schemas** go in `docs/flows.md` as mermaid `flowchart`/`sequenceDiagram`
  fences. Trace the real call chain from source before drawing
  (e.g. `detect_active → preferred_player → classify`), and name the functions.
- Prose style matches the existing docs: explain *why* a design choice was made
  (the reasons are already in module docstrings — mine them), not just what.

## Wiring a new page in (REQUIRED, two places)
1. Add it to the `nav:` tree in `mkdocs.yml`. Nested sections are fine
   (`Modes:` with child pages). Sub-directory pages use the path
   (`modes/scan.md`).
2. Add a one-line entry to the **Documentation map** in `docs/index.md`.

## mermaid
Rendered via the `mermaid2` plugin with a custom superfences fence — just use
```` ```mermaid ```` fences. `make docs` logs how many diagrams it found per
page, a quick sanity check they were detected.

## Verify with `make docs` and read the output
`make docs` exits 0 even with problems, so grep the output:
```bash
make docs 2>&1 | grep -iE "does not contain|error|warn|built in"
```
- **Broken-anchor pitfall:** an intra-doc link to a `#fragment` that doesn't
  match a real heading slug produces
  `contains a link '...#x', but the doc '...' does not contain an anchor '#x'`.
  Heading slugs are auto-generated (lowercased, punctuation stripped, spaces →
  hyphens) and hand-guessed anchors — especially for headings with numbers,
  `/`, or parentheses like `#d-local-audio-analysis-karaoke-analyze--scripts...`
  — usually don't match. Fix by copying the exact slug from the target page or,
  when unsure, link the page with no fragment. Re-run until the only remaining
  line is `Documentation built in …`.
- The `extra_javascript is now DEPRECATED` mermaid notice is pre-existing and
  harmless; ignore it.

## mermaid pitfall: parentheses (and other shape chars) in node labels
A single bad diagram fails silently: `make docs`/`mkdocs serve` build fine and
the other diagrams on the page render, but the broken one is left on the page
as its raw ```` ```mermaid ```` source text (client-side Mermaid parse error).
Cause: unquoted special chars inside a `[...]`/`{...}` node label. A `(` makes
the flowchart parser expect a new node-shape start (`PS` token), e.g.
`F[retry with clean_title\ndrop '(Remastered)' etc.]` → `Parse error ... got 'PS'`.
Fix: wrap any label containing `()`, `[]`, `{}`, `:`, `;`, `#`, or quotes in
double quotes: `F["retry with clean_title\ndrop '(Remastered)' etc."]`.
Diagnose the exact failing diagram/line in the browser console via
`await window.mermaid.parse(src)` — it names the line and expected tokens.
`make docs` does NOT catch this (Mermaid runs in the browser, not at build).
