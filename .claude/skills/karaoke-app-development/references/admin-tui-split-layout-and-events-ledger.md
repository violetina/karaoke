# Admin TUI Split Layout, Log Streaming, and Host-Cluster Event Ledger

Key architectural patterns and Textual TUI design lessons learned from building the backend Operations TUI (`make admin`, `karaoke.admin_tui`) and connecting it to the Celery/Argo Events ledger.

## 1. Split Layout vs. Single Vertical Stack for Operations Dashboards
- **Problem**: When a dashboard has 5+ control panels (workers, clients, pipeline actions, inputs, logs), stacking them in a single vertical column (`layout: vertical; height: 1fr; overflow-y: auto;`) pushes the lower panels 40–60 lines down. On standard terminal heights (24–40 lines), the log is pushed off-screen or crushed into a tiny 4-line sliver.
- **Pattern**: Use a **2-column horizontal split layout**:
  - **Left column (55% width, `overflow-y: auto`)**: Operations, scaling buttons, connected clients, pipeline triggers, and text inputs.
  - **Right column (45% width, full height)**: Dedicated, isolated `System Logs & Diagnostics` container with its own border.
  - Both sides remain simultaneously visible; the log pane takes the entire vertical height of the terminal.

## 2. Textual Log Streaming: `RichLog` vs `OptionList`
- **Pitfall**: Using `OptionList` as a log viewer. Each line of a multi-line traceback becomes an interactive selectable row with highlight bars. Calling `clear_options()` every polling tick causes heavy visual flicker, resets the scrollbar, and breaks text wrapping.
- **Solution**: Use `from textual.widgets import RichLog`:
  ```python
  RichLog(id="error-log", highlight=True, markup=True, wrap=True)
  ```
  - Only redraw/write when log content actually changes (`if self._last_logs != current_logs:`).
  - Apply markup highlighting for log severities: `[bold red]` for `ERROR`/`CRITICAL`, `[bold yellow]` for `Traceback`/`Exception`, `[dim]` for standard timestamps.
  - Supports automatic word-wrapping and smooth mouse/keyboard scrolling out of the box.

## 3. Keyboard Focus Traps and Easy-Close Controls
- **Problem**: In Textual, a bare `q` binding cannot quit while any `Input` widget has focus — typing `q` simply inserts the letter into the text field.
- **Pattern**:
  1. Add high-priority quit combos that bypass widget focus:
     ```python
     Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
     Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
     ```
  2. Implement an `escape` handler that drops input focus first, then quits if unfocused:
     ```python
     Binding("escape", "handle_escape", "Unfocus / Quit", show=False, priority=True)

     def action_handle_escape(self) -> None:
         focused = self.focused
         if focused is not None and isinstance(focused, Input):
             self.set_focus(None)
             return
         self.exit()
     ```
  3. Include an on-screen `Button("Quit (q)", id="btn-quit", variant="error")` so mouse and browser Web-UI users can exit in one click.

## 4. Host-Cluster Event Ledger (Argo Events -> OpenSearch -> Host TUI)
- **Problem**: In-cluster Kubernetes pods (kind) cannot reach the host control API because it binds loopback (`127.0.0.1:8765`) on the host machine. Pushing events directly from a cluster pod to host fails (`unreachable`).
- **Solution**: Use **OpenSearch (`localhost:9200`) as a shared event ledger**:
  1. Celery worker emits `task.succeeded` to RabbitMQ topic exchange `celeryev`.
  2. Argo Events `EventSource/amqp-celery-eventsource` mirrors events into `karaoke-celery-events`.
  3. Argo Events `Sensor/celery-postprocess-sensor` executes an HTTP trigger `POST http://opensearch-cluster-master.default.svc.cluster.local:9200/karaoke-events/_doc/`.
  4. Host control API exposes `GET /api/events/recent?since_ts=...`, backed by `karaoke.events.recent_events()`.
  5. Host TUIs poll `/api/events/recent` every 2.5s and refresh track info/lyrics **in-place** via `call_from_thread`, avoiding full browser reloads (`textual-serve` reloads kill in-progress audio/sample tasks via `on_unmount`).
