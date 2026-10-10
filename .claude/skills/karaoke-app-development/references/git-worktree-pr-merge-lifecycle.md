# Git worktree PR merge lifecycle & cleanup

Companion to `git-worktree-merge-verification.md`. Covers the merge/cleanup step
of the karaoke worktree flow, where `main` is permanently checked out in the
primary worktree `/home/tina/karaoke` and each feature lives in a sibling
worktree (`/home/tina/karaoke-<feature>`).

## `gh pr merge --delete-branch` fails: "'main' is already used by worktree"

`gh pr merge --squash --delete-branch` errors with:

```
fatal: 'main' is already used by worktree at /home/tina/karaoke
```

The GitHub squash-merge **still succeeds** — only the local branch-delete step
fails, because `gh` tries to switch the current worktree to `main` and can't
(it's checked out in the primary worktree). Do NOT re-run the merge. Confirm and
clean up manually:

```bash
gh pr view <N>                                  # expect state: MERGED
cd /home/tina/karaoke && git pull origin main --ff-only
git worktree remove /home/tina/karaoke-<feature>
git branch -D feat/<feature>
make test                                       # merged result on main is green
```

`git worktree remove` also refuses if the worktree has a dirty tree — commit,
stash, or discard first.

## "Feature X isn't in main but I thought we merged it"

Frequent root cause: an earlier PR merged only a **snapshot** of the branch, and
the feature commits landed on that same branch *after* the PR merged, so the
branch was never re-merged. (Seen with `feat/tui`: PR #23 merged the 11:44
snapshot; the cartwheel/layout commits at 17:28–17:30 stayed on the branch.)

Diagnose by comparing commit dates against the merged PR's timestamp:

```bash
git log origin/main..feat/<branch> --oneline    # commits still not on main
git log -n5 --format='%h %cd %s'                 # dates vs. the merged PR time
git branch -a --contains <sha>                   # which branches hold the feature
gh pr list --state all --limit 20                # find the snapshot PR + merge time
```

Fix = rebase-and-new-PR, NOT re-merging the old PR:

```bash
cd /home/tina/karaoke-<feature>
git fetch origin && git rebase origin/main       # skips already-upstream commits
make test
git push --force-with-lease
gh pr create ...
```

## PR body gotcha

`gh pr create --body "...$var..."` inside a double-quoted heredoc/string: any
`$word` (e.g. `$FastAPI`, backticked `src/karaoke/api.py` paths) is shell-expanded
to empty, silently blanking those tokens in the rendered PR body. Use single
quotes, escape `$`, or a `--body-file`.
