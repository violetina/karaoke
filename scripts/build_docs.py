#!/usr/bin/env python3
"""scripts/build_docs.py - Port the karaoke docs/ tree into the Quartz wiki.

This is the MkDocs replacement. The doc *source of truth* stays in this repo
under ``docs/`` (version-controlled alongside the code). This script renders
that source into the Quartz content directory, doing the three things MkDocs
plugins used to do:

1. **Auto docstrings** — expands every ``::: karaoke.<module>`` directive in
   ``docs/api.md`` into the module's extracted docstrings + public
   class/function signatures (replaces ``mkdocstrings``). The directive list
   itself is kept complete by ``scripts/doc_sync.py`` (run ``make docs-sync``),
   so adding a new module and running the sync keeps the API reference honest.
2. **Admonitions** — converts MkDocs ``!!!`` / ``???`` blocks into Obsidian /
   Quartz callouts (``> [!type]``).
3. **Snippets** — inlines ``--8<-- "file"`` includes.

Assets (svg, png, mermaid, dot) are copied verbatim; Mermaid fences render
natively in Quartz.

Usage:
    python scripts/build_docs.py              # port docs/ -> $WIKI_WEB/content
    python scripts/build_docs.py --api-only   # only regenerate api.md
    WIKI_WEB=/path/to/wiki-web python scripts/build_docs.py

The Quartz site location defaults to ``~/wiki-web`` and can be overridden with
the ``WIKI_WEB`` environment variable. The site is a separate git repo; after
porting, build it with ``make wiki`` (or ``cd $WIKI_WEB && npx quartz build``).
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DOCS = REPO_ROOT / "docs"
SRC_PKG = REPO_ROOT / "src"
SUBDOC = REPO_ROOT / "src" / "karaoke" / "docs" / "index.md"

WIKI_WEB = Path(os.environ.get("WIKI_WEB", Path.home() / "wiki-web")).expanduser()
DST_CONTENT = WIKI_WEB / "content"

ADMON_RE = re.compile(
    r'^(\s*)(!!!|\?\?\?\+?)\s+([a-zA-Z0-9_-]+)(?:\s+"([^"]*)")?\s*$'
)
API_DIRECTIVE_RE = re.compile(r"^:::\s+([a-zA-Z0-9_.]+)\s*$")
SNIPPET_RE = re.compile(r'^--8<--\s+"([^"]+)"\s*$', re.M)


def convert_admonitions(text: str) -> str:
    """Convert MkDocs !!!/??? admonition blocks to Quartz callouts."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = ADMON_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, marker, ad_type, title = m.groups()
        fold = "-" if marker == "???" else ("+" if marker == "???+" else "")
        out.append(f"{indent}> [!{ad_type}]{fold}" + (f" {title}" if title else ""))
        i += 1
        body_prefix = indent + "    "
        while i < len(lines):
            line = lines[i]
            if line.strip() == "":
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt.startswith(body_prefix) or nxt.startswith(indent + "\t"):
                    out.append(f"{indent}>")
                    i += 1
                    continue
                break
            if line.startswith(body_prefix):
                out.append(f"{indent}> {line[len(body_prefix):]}")
                i += 1
            elif line.startswith(indent + "\t"):
                out.append(f"{indent}> {line[len(indent) + 1:]}")
                i += 1
            else:
                break
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _first_line(doc: str | None) -> str:
    return doc.strip().splitlines()[0] if doc and doc.strip() else ""


def render_module_api(mod_name: str) -> str:
    """Extract a module's docstrings + public signatures (replaces mkdocstrings)."""
    rel = mod_name.replace(".", "/")
    py_path = SRC_PKG / (rel + ".py")
    if not py_path.exists():
        py_path = SRC_PKG / rel / "__init__.py"
    if not py_path.exists():
        return f"*(Module `{mod_name}` source not found)*\n"
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return f"*(Could not parse `{mod_name}`)*\n"

    parts: list[str] = []
    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        parts.append(mod_doc.strip() + "\n")

    defs: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            doc = _first_line(ast.get_docstring(node))
            defs.append(f"- `class {node.name}`" + (f" — {doc}" if doc else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            args = [a.arg for a in node.args.args]
            prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            doc = _first_line(ast.get_docstring(node))
            defs.append(f"- `{prefix}{node.name}({', '.join(args)})`" + (f" — {doc}" if doc else ""))
    if defs:
        parts.append("\n".join(defs) + "\n")
    return "\n".join(parts)


def expand_api_directives(text: str) -> str:
    """Replace ``::: karaoke.module`` directives with extracted docstrings."""
    out: list[str] = []
    for line in text.splitlines():
        m = API_DIRECTIVE_RE.match(line)
        if m:
            out.append(render_module_api(m.group(1)))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def inline_snippets(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        target = REPO_ROOT / m.group(1).strip()
        if target.exists():
            return target.read_text(encoding="utf-8", errors="ignore")
        return m.group(0)
    return SNIPPET_RE.sub(repl, text)


def render_markdown(rel_posix: str, text: str) -> str:
    text = inline_snippets(text)
    if rel_posix == "api.md":
        text = expand_api_directives(text)
    return convert_admonitions(text)


def build(api_only: bool = False) -> None:
    if not DST_CONTENT.parent.exists():
        raise SystemExit(
            f"Quartz site not found at {WIKI_WEB}. Set WIKI_WEB or clone the site first."
        )

    if api_only:
        src = SRC_DOCS / "api.md"
        dst = DST_CONTENT / "api.md"
        dst.write_text(render_markdown("api.md", src.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"Regenerated {dst} from {src}")
        return

    if DST_CONTENT.exists():
        shutil.rmtree(DST_CONTENT)
    DST_CONTENT.mkdir(parents=True, exist_ok=True)

    n_md = n_asset = 0
    for src in sorted(SRC_DOCS.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(SRC_DOCS)
        if rel.parts and rel.parts[0] == "stylesheets":
            continue
        dst = DST_CONTENT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".md":
            dst.write_text(render_markdown(rel.as_posix(), src.read_text(encoding="utf-8", errors="ignore")), encoding="utf-8")
            n_md += 1
        else:
            shutil.copy2(src, dst)
            n_asset += 1

    if SUBDOC.exists():
        (DST_CONTENT / "tools-karaoke.md").write_text(
            convert_admonitions(SUBDOC.read_text(encoding="utf-8", errors="ignore")),
            encoding="utf-8",
        )
        n_md += 1

    print(f"Ported {n_md} markdown files and {n_asset} assets into {DST_CONTENT}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-only", action="store_true", help="only regenerate api.md")
    args = ap.parse_args()
    build(api_only=args.api_only)


if __name__ == "__main__":
    main()
