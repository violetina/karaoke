# Stale Editable Installation / Venv Debugging

When developing in git worktrees, you may encounter a situation where code changes in your files (like `tui.py`, `localcache.py`, etc.) are completely ignored by the python interpreter when running the TUI or commands, even after running `make install` or `pip install -e .`.

## Root Cause
The virtual environment (`.venv`) may contain a static copy of the package under `site-packages/karaoke` instead of a PEP-660 editable link (a `.pth` reference). This occurs if the package was previously installed statically, or if a build backend step copied the files. The static files in `site-packages` clobber any live changes in your source path (`src/karaoke`).

## Diagnostics
Run the following command to check where the python interpreter is importing the package from:
```bash
/home/tina/karaoke/.venv/bin/python -c "import karaoke; print(karaoke.__file__)"
```

- **Stale (Incorrect):** `/home/tina/karaoke/.venv/lib/python3.14/site-packages/karaoke/__init__.py` (a real folder containing python files)
- **Correct:** `/home/tina/karaoke/src/karaoke/__init__.py` (pointing to your live git repo source directory)

## Resolution
To resolve this, cleanly uninstall the package, purge the static directory manually, and reinstall in editable mode:
```bash
pip uninstall -y karaoke
rm -rf /home/tina/karaoke/.venv/lib/python3.14/site-packages/karaoke
pip install -e .
```
