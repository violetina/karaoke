from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = ROOT / "docs_confluence"

def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(DOCS, OUT)
    print(f"[ok] copied {DOCS} -> {OUT}")
    print("[todo] add snippet expansion here")

if __name__ == "__main__":
    main()

