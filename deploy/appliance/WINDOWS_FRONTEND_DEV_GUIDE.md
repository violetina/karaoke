# Windows 10 Frontend Developer Guide: Karaoke Backend Appliance

This guide provides everything needed to run the complete, pre-seeded Karaoke backend on **Windows 10** without touching Linux packages, Python virtualenvs, or database configs.

---

## 1. Quick Setup on Windows 10 (1-Click)

1. Ensure **WSL2** is enabled on Windows 10:
   * Open PowerShell as Administrator and run:
     ```powershell
     wsl --install --no-distribution
     ```
   * (If already enabled, you can skip this step).
2. Copy `karaoke-backend-wsl2.tar.gz` and the batch scripts (`install_on_windows.bat`, `start_backend.bat`, `stop_backend.bat`) into any folder (e.g. `Downloads` or `Desktop`).
3. Double-click **`install_on_windows.bat`**.
   * It imports the appliance into `C:\WSL\KaraokeBackend` and launches the services automatically.

---

## 2. Running Services & Endpoints

Because WSL2 shares the Windows localhost network stack, all services are directly available on your Windows machine:

| Service | Port | Endpoint URL | Description |
| :--- | :---: | :--- | :--- |
| **Karaoke REST API** | `8000` | `http://localhost:8000` | Full library API |
| **Interactive Swagger Docs** | `8000` | `http://localhost:8000/docs` | Test and explore all endpoints interactively |
| **Similarity Graph UI** | `8888` | `http://localhost:8888/graph` | Cytoscape.js interactive music theory graph |
| **AI DJ MCP Server** | `8888` | `http://localhost:8888/sse` | MCP SSE protocol for AI agent integration |
| **PostgreSQL Database** | `5432` | `localhost:5432` (user/pass/db: `karaoke`) | **18,370 tracks**, lyrics, chords, sync offsets |
| **OpenSearch Vector Engine** | `9200` | `http://localhost:9200` | **347,604 documents** & vector embeddings |

---

## 3. Frontend Architecture: Replacing MPRIS

On Linux, MPRIS was used because a terminal TUI had to spy on external desktop apps via D-Bus.

For your frontend (React, Vue, Svelte, Electron, or Next.js):
* **The Frontend IS the Player**:
  * Embed the **YouTube IFrame Player API** or HTML5 Video element in your UI.
  * Your UI already knows the current playback time (`player.getCurrentTime()`), duration, and whether it's playing or paused.
* **Syncing with the Backend**:
  * When a song starts playing, send a quick POST request:
    ```json
    POST http://localhost:8000/api/player/now_playing
    {
      "track_id": 18627,
      "artist": "Red Hot Chili Peppers",
      "title": "Aeroplane"
    }
    ```
  * Query the lyrics and timing offsets:
    ```javascript
    const res = await fetch(`http://localhost:8000/api/tracks/${trackId}`);
    const track = await res.json();
    // track.synced_lyrics contains enhanced word/line timestamps!
    ```

---

## 4. Running in Kiosk Mode on Windows 10

When you want to display the Karaoke player on a TV or dedicated screen:

### Option A: Using Google Chrome or Microsoft Edge
Launch your frontend URL in kiosk mode:
```cmd
# Using Microsoft Edge (pre-installed on every Windows 10 PC):
msedge.exe --kiosk "http://localhost:3000" --edge-kiosk-type=fullscreen

# Or using Google Chrome:
chrome.exe --kiosk --app="http://localhost:3000"
```
Press `Alt + F4` to exit kiosk mode.

### Option B: Using Electron / Tauri
If bundling as a Windows desktop application:
```javascript
mainWindow.setKiosk(true); // Fullscreen with no borders or address bars
```

---

## 5. Daily Operations

* **To start the backend**: Double-click `start_backend.bat`.
* **To stop the backend**: Double-click `stop_backend.bat`.
* **To inspect backend files from Windows Explorer**:
  Press `Win + R` and enter:
  ```text
  \\wsl$\KaraokeBackend\opt\karaoke
  ```
