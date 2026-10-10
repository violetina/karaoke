# Textual TUI vs. CLI Countdown Parity & Git Worktree Conflicts

## 1. Textual TUI vs. CLI Countdown Feature Parity

When implementing live lyric timing features or countdowns (like instrumental gap cues or lyric line-end/rest-gap countdowns), keep feature parity between the CLI rendering loop and the Textual TUI view.

### The Problem
- The CLI rendering loop (e.g. `_build_frame` / `Panel` subtitles) natively calculates the time remaining until the next upcoming lyric line (`next in X.Xs`) and displays it as a live-updating panel subtitle.
- The TUI lyrics view (`_render_body`) only extracts the text segments. If the Static `#lyrics` widget in the TUI is simply updated with the text body, the live countdown is completely lost to TUI users.

### The Solution
- Textual `Static` (and most other `Widget` subclasses) support dynamic `border_subtitle` and `border_title` properties when a border is enabled in CSS.
- Update the border subtitle of the lyrics widget dynamically during the rendering tick inside the TUI controller:

```python
    def _render_synced(self, elapsed: float) -> None:
        tl = self._timeline
        active = tl.active_index(elapsed)
        mood = mood_of(tl.lines[active][1]) if active >= 0 else "neutral"
        body = Text()
        _render_body(body, tl, elapsed, mood=mood)
        
        lyrics_widget = self.query_one("#lyrics", Static)
        lyrics_widget.update(body)
        
        # Sync the "next in X.Xs" countdown to TUI lyric panel subtitle
        nxt = tl.next_time(elapsed)
        lyrics_widget.border_subtitle = f"next in {nxt - elapsed:0.1f}s" if nxt else "(end)"
```

- **Dynamic Cleanup:** Ensure you explicitly reset `lyrics_widget.border_subtitle = None` whenever player is inactive, when there are no synced lyrics, or when a track transitions, to prevent stale countdown states from sticking.


## 2. Git Worktree Rebase & Reconciliation Hurdles

Because the project leverages Git worktrees to isolate feature branches, managing branch alignment on local `main` can sometimes trigger index conflicts due to untracked files.

### The Problem
- Sibling worktrees or prior feature branches can leave untracked/uncommitted files in the directory.
- When attempting a standard pull/rebase on a stale branch (e.g. your local `main` being behind `origin/main`), Git will abort with:
  `error: The following untracked working tree files would be overwritten by checkout... Please move or remove them before you switch branches.`
- Often, these untracked files are identical to or stale copies of files already committed to the remote branch (`origin/main`).

### The Solution
To safely align your local branch without losing progress or corrupting files:

1. **Preserve Untracked Changes Dynamically:**
   Stash all untracked files into Git's temporary storage to clear the working tree:
   ```bash
   git stash push -u -m "pre-origin-main cleanup untracked files"
   ```
2. **Hard Reset to Remote Branch:**
   Safely move the local branch pointer and index to match the current remote branch:
   ```bash
   git reset --hard origin/main
   ```
3. **Verify Targets:**
   Verify targets exist in the updated repository state (e.g., `make help | grep tui`).
4. **Selective Stash Recovery:**
   If there are actual custom uncommitted planning or docs files that you want to bring back, check them out selectively from the stash instead of applying the whole stash (which would re-introduce duplicate/stale files):
   ```bash
   git checkout stash@{0} -- docs/browser-cookies.md docs/search-howto.md
   ```
