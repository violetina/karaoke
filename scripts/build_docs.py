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
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DOCS = REPO_ROOT / "docs"
SRC_PKG = REPO_ROOT / "src"
SUBDOC = REPO_ROOT / "src" / "karaoke" / "docs" / "index.md"


def repo_version() -> str:
    """Read the package version straight from pyproject.toml (no deps)."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else "unknown"


def repo_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def version_banner() -> str:
    """The one place that stamps 'which release do these docs describe'.

    Regenerated on every port, so the wiki can never show docs for a version
    other than the one currently checked out -- there is no hand-maintained
    version number to forget to bump.
    """
    return (
        f"> [!info] Docs for karaoke **v{repo_version()}** (`{repo_commit()}`)\n"
        f"> Generated from this exact checkout by `make wiki` / `scripts/build_docs.py`.\n"
        f"> For what changed between versions, see the "
        f"[Releases overview](releases/index.md) (CHANGELOG-based, always current).\n\n"
    )

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


def format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a real signature: type hints, defaults, *args/**kwargs, return type."""
    a = node.args
    parts: list[str] = []
    posonly = list(getattr(a, "posonlyargs", []))
    pos = posonly + list(a.args)
    n_default = len(pos) - len(a.defaults)

    def fmt(arg: ast.arg, default: ast.expr | None = None) -> str:
        s = arg.arg
        if arg.annotation is not None:
            s += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            s += f" = {ast.unparse(default)}"
        return s

    for i, arg in enumerate(pos):
        default = a.defaults[i - n_default] if i >= n_default else None
        parts.append(fmt(arg, default))
        if posonly and i == len(posonly) - 1:
            parts.append("/")
    if a.vararg:
        parts.append("*" + fmt(a.vararg))
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(fmt(arg, default))
    if a.kwarg:
        parts.append("**" + fmt(a.kwarg))
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"({', '.join(parts)}){ret}"


def render_module_api(mod_name: str) -> str:
    """Extract full docstrings + real signatures from source (replaces mkdocstrings)."""
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

    def render_func(node: ast.FunctionDef | ast.AsyncFunctionDef, level: str) -> None:
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        try:
            sig = format_signature(node)
        except Exception:
            sig = "(...)"
        parts.append(f"{level} `{prefix}{node.name}{sig}`\n")
        doc = ast.get_docstring(node)
        if doc:
            parts.append(doc.strip() + "\n")

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            parts.append(f"### class `{node.name}`\n")
            cls_doc = ast.get_docstring(node)
            if cls_doc:
                parts.append(cls_doc.strip() + "\n")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("_"):
                    render_func(sub, "####")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            render_func(node, "###")
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
    text = convert_admonitions(text)
    if rel_posix == "index.md":
        # Insert the version banner right after the H1 frontmatter line so it
        # reads naturally as the first thing on the homepage.
        lines = text.splitlines(keepends=True)
        if lines and lines[0].startswith("# "):
            text = lines[0] + "\n" + version_banner() + "".join(lines[1:])
        else:
            text = version_banner() + text
    return text


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
