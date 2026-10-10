# Textual Serve Web UI Integration

When exposing a Textual TUI to the web, relying on the `textual serve` CLI binary can be brittle if it's not properly linked in the virtual environment's `bin/` directory or if the module lacks a direct `__main__.py` entrypoint.

Instead of debugging PATH issues or missing CLI wrappers, the most robust way to serve the app is to write a custom runner script that invokes the `Server` class programmatically.

## Implementation

1. Add `textual-serve` to the project dependencies.
2. Create a runner script (e.g., `scripts/web_serve.py`):

```python
import argparse
from textual_serve.server import Server

def main():
    parser = argparse.ArgumentParser(description="Serve the Textual app via web browser")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--command", type=str, default="karaoke-tui", help="Command to run the TUI")
    args = parser.parse_args()

    server = Server(args.command, port=args.port, host="0.0.0.0")
    print(f"Serving {args.command} on http://0.0.0.0:{args.port} ...")
    server.serve()

if __name__ == "__main__":
    main()
```

3. Wire it into the project's `Makefile`:

```makefile
web: ## Serve the TUI in a web browser using textual-serve
	$(PYTHON) scripts/web_serve.py
```