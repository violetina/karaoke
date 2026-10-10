# Merge Authority & Version Bumping (karaoke personal repo)

Conventions confirmed with the user for the `violetina/karaoke` repo.

## This repo is the user's PERSONAL project — merge directly
When the user says "merge this" / "see if we can merge these changes":
1. Confirm CI is green (`gh pr checks <n> --watch`).
2. Squash-merge it yourself: `gh pr merge <n> --squash --delete-branch`.
3. Sync local: `git checkout main && git pull origin main`.

Do NOT leave karaoke PRs open for review. This is the **OPPOSITE** of the
meemoo / k8s-templator platform repo, where the standing rule (see memory) is
that infra/doc changes ship as PRs left OPEN for the dev team. Keep the two
repos' merge policies separate — never apply meemoo's "leave open" rule to
karaoke, or karaoke's "merge directly" rule to meemoo. When unsure which repo
you're in, check the remote (`git remote -v`).

## "dump / bump the version" = big-change landmark
When the user asks to bump the version (usually phrased as a big/unified change),
bump it in BOTH files or they drift apart:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/karaoke/__init__.py` → `__version__ = "X.Y.Z"`

Use a semver MINOR jump for a feature landmark. Example from the "unified
playing" release: `0.1.1` → `0.3.0` (deliberately skipping `0.2.0` so the
package version supersedes the internal `API_VERSION` / `CTRL_API_VERSION`
constants already sitting at `0.2.0` in `src/karaoke/api.py` and
`src/karaoke/ctrl_api.py`). Run `make test` to confirm 323 tests still pass,
then ship the bump as its own tiny follow-up PR AFTER the feature PR merges.
