# Git worktree merge verification

When merged karaoke work seems missing from `main`, first distinguish `origin/main` from the local `/home/tina/karaoke` worktree.

Reliable checks:

```bash
git fetch origin --prune
git show origin/main:Makefile | grep -nE '^(tui|browse|install-audio|analyze|AUDIO_|\.PHONY)'
git -C /home/tina/karaoke status --short --branch
git -C /home/tina/karaoke rev-list --left-right --count main...origin/main
git branch --contains <commit> --all
```

Interpretation:
- `origin/main:<path>` shows what is actually merged remotely.
- `/home/tina/karaoke` can be stale, dirty, or diverged even when the remote branch already contains the PR.
- If local `main` is dirty/diverged, do not recommend a blind pull/reset unless the user confirms the local changes are disposable.
