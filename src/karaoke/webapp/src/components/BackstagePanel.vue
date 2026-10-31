<template>
  <div class="backstage">
    <div class="bs-note">
      Ported from the admin TUI's operations screen (<code>admin_tui.py</code>).
      Pipeline triggers it runs in-process (folder scan, vector rebuild,
      Whisper alignment, audio backfill) aren't wired here yet — use the TUI
      or CLI for those; everything below talks to the same control/library
      APIs the TUI already uses.
    </div>

    <div class="bs-grid">
      <!-- Radio -->
      <div class="bs-card">
        <div class="bs-card-header">
          <span class="bs-card-title">📻 songrec Radio Mode</span>
          <button class="bs-mini-btn" @click="fetchRadio" title="Refresh">↻</button>
        </div>
        <div class="bs-stat-row">
          <span>Status</span>
          <span class="bs-pill" :class="{ ok: radio.enabled }">{{ radio.status || (radio.enabled ? 'running' : 'stopped') }}</span>
        </div>
        <div class="bs-stat-row" v-if="radio.title">
          <span>Recognized</span><span class="bs-stat-value">{{ radio.artist }} — {{ radio.title }}</span>
        </div>
        <div class="bs-stat-row" v-if="radio.processing">
          <span>Processing</span><span class="bs-dim">{{ radio.processing }}</span>
        </div>
        <div class="bs-actions">
          <button class="bs-btn" :class="{ active: radio.enabled }" @click="toggleRadio" :disabled="radioBusy">
            {{ radioBusy ? '…' : (radio.enabled ? '⏹ Turn Radio Off' : '📻 Turn Radio On') }}
          </button>
        </div>
      </div>

      <!-- Workers -->
      <div class="bs-card">
        <div class="bs-card-header">
          <span class="bs-card-title">⚙️ Post-Processing Workers (Celery)</span>
          <button class="bs-mini-btn" @click="fetchWorkers" title="Refresh">↻</button>
        </div>
        <div v-if="workers.available" class="bs-workers">
          <div class="bs-stat-row">
            <span>Active workers</span><span class="bs-stat-value">{{ workers.workers_active }}</span>
          </div>
          <div class="bs-stat-row">
            <span>Queue depth</span><span class="bs-stat-value">{{ workers.queue_depth }}</span>
          </div>
          <div class="bs-stat-row" v-if="workers.dashboard_url">
            <span>Flower</span>
            <a :href="workers.dashboard_url" target="_blank" class="bs-link">open ↗</a>
          </div>
          <table class="bs-table" v-if="workers.worker_details?.length">
            <tbody>
              <tr v-for="w in workers.worker_details" :key="w.unit">
                <td class="bs-mono">{{ w.unit }}</td>
                <td><span class="bs-pill" :class="{ ok: w.running }">{{ w.active }}</span></td>
              </tr>
            </tbody>
          </table>
          <div class="bs-actions">
            <button class="bs-btn" @click="scaleWorkers(1)" :disabled="scaleBusy">Start Worker (+)</button>
            <button class="bs-btn warn" @click="scaleWorkers(0)" :disabled="scaleBusy">Stop Worker (-)</button>
          </div>
        </div>
        <div v-else class="bs-empty">
          Worker status unavailable{{ workers.reason ? `: ${workers.reason}` : '' }}.
        </div>
      </div>

      <!-- Clients -->
      <div class="bs-card">
        <div class="bs-card-header">
          <span class="bs-card-title">🔌 Connected Clients</span>
          <button class="bs-mini-btn" @click="fetchClients" title="Refresh">↻</button>
        </div>
        <div class="bs-stat-row">
          <span>Web TUI tabs (:{{ clients.webui_port }})</span><span class="bs-stat-value">{{ clients.webui_count ?? 0 }}</span>
        </div>
        <div class="bs-stat-row">
          <span>Playback sessions</span><span class="bs-stat-value">{{ clients.playback_count ?? 0 }}</span>
        </div>
        <table class="bs-table" v-if="clients.clients?.length">
          <tbody>
            <tr v-for="(c, i) in clients.clients" :key="i">
              <td class="bs-mono">{{ c.kind }}</td>
              <td>{{ c.client }}</td>
              <td class="bs-dim">{{ c.detail }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="bs-empty">No clients connected right now.</div>
      </div>

      <!-- Live recording -->
      <div class="bs-card">
        <div class="bs-card-header">
          <span class="bs-card-title">🎙️ Live Recording & Session Capture</span>
          <button class="bs-mini-btn" @click="fetchRecordStatus" title="Refresh">↻</button>
        </div>
        <div class="bs-actions">
          <button class="bs-btn" :class="{ active: recording.active }" @click="toggleRecording" :disabled="recordBusy">
            {{ recordBusy ? '…' : (recording.active ? '⏹ Stop Recording' : '🔴 Start Live Recording') }}
          </button>
        </div>
        <div v-if="recording.active" class="bs-stat-row">
          <span>Recording id</span><span class="bs-stat-value">{{ recording.id }}</span>
        </div>
        <div v-else class="bs-empty">Not currently recording.</div>
      </div>

      <!-- Kiosk control -->
      <div class="bs-card">
        <div class="bs-card-header">
          <span class="bs-card-title">🖥️ Kiosk Chrome Window</span>
        </div>
        <div class="bs-actions">
          <button class="bs-btn warn" @click="restartKiosk" :disabled="kioskBusy">
            {{ kioskBusy ? 'Restarting…' : '↻ Restart Kiosk' }}
          </button>
        </div>
        <div v-if="kioskMsg" class="bs-empty">{{ kioskMsg }}</div>
      </div>

      <!-- Radio sessions -->
      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">📻 Radio Sessions</span>
          <button class="bs-mini-btn" @click="fetchRadioSessions" title="Refresh">↻</button>
        </div>
        <table class="bs-table" v-if="radioSessions.length">
          <thead>
            <tr><th>ID</th><th>Started</th><th>Tracks</th><th>Status</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="s in radioSessions" :key="s.session_id">
              <td class="bs-mono">{{ s.session_id }}</td>
              <td class="bs-dim">{{ formatTime(s.started_at) }}</td>
              <td>{{ s.track_count }}</td>
              <td><span class="bs-pill" :class="{ ok: s.status === 'active' }">{{ s.status }}</span></td>
              <td>
                <button class="bs-mini-btn" @click="importRadioSession(s.session_id)"
                       :disabled="importingId === s.session_id" title="Import this session's tracks">
                  {{ importingId === s.session_id ? '…' : 'Import' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="bs-empty">No radio sessions found.</div>
      </div>

      <!-- Staging queue -->
      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">📝 Lyrics Staging Queue</span>
          <button class="bs-mini-btn" @click="fetchStagingQueue" title="Refresh">↻</button>
        </div>
        <table class="bs-table" v-if="stagingItems.length">
          <thead>
            <tr><th>Artist / Title</th><th>Source</th><th>Conf.</th><th>Synced</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="item in stagingItems" :key="item.id">
              <td>
                <div class="bs-bold">{{ item.artist }} — {{ item.title }}</div>
              </td>
              <td class="bs-dim">{{ item.source_kind }}</td>
              <td>{{ item.confidence != null ? item.confidence.toFixed(2) : '—' }}</td>
              <td>{{ item.has_synced ? '✓' : '—' }}</td>
              <td class="bs-row-actions">
                <button class="bs-mini-btn ok" @click="reviewStaged(item.id, 'approve')"
                       :disabled="reviewingId === item.id">✓</button>
                <button class="bs-mini-btn warn" @click="reviewStaged(item.id, 'reject')"
                       :disabled="reviewingId === item.id">✕</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="bs-empty">Nothing pending review.</div>
      </div>

      <!-- Event log / error log -->
      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">📋 Recent Events</span>
          <button class="bs-mini-btn" @click="fetchEvents" title="Refresh">↻</button>
        </div>
        <div class="bs-log" v-if="events.length">
          <div v-for="(e, i) in events" :key="i" class="bs-log-line">
            <span class="bs-dim">{{ formatTime(e.ts) }}</span> {{ e.task_name }}
            <span class="bs-pill" :class="{ ok: e.state === 'SUCCESS' }">{{ e.state }}</span>
          </div>
        </div>
        <div v-else class="bs-empty">No recent events.</div>
      </div>

      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">🪵 Recent Error Logs</span>
          <button class="bs-mini-btn" @click="fetchErrorLogs" title="Refresh">↻</button>
        </div>
        <div class="bs-log" v-if="errorLogs.length">
          <div v-for="(line, i) in errorLogs" :key="i" class="bs-log-line error">{{ line }}</div>
        </div>
        <div v-else class="bs-empty">No recent errors.</div>
      </div>

      <!-- Library / playlist browse (admin TUI's 'H' browse) -->
      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">📚 Browse Library / Playlists</span>
          <span class="bs-dim">right-click a track for actions</span>
        </div>
        <div class="bs-browse-controls">
          <select class="bs-select" v-model="browseMode" @change="onBrowseModeChange">
            <option value="library">🔍 Search library</option>
            <option value="playlist">📂 Browse a playlist</option>
          </select>
          <input
            v-if="browseMode === 'library'"
            class="bs-input"
            type="text"
            v-model="browseQuery"
            placeholder="artist or title…"
            @input="debouncedLibrarySearch"
          />
          <select v-else class="bs-select" v-model="browsePlaylistId" @change="fetchPlaylistTracks">
            <option value="">— choose a playlist —</option>
            <option v-for="p in playlists" :key="p.id" :value="p.id">{{ p.name || p.id }}</option>
          </select>
        </div>
        <table class="bs-table" v-if="browseTracks.length">
          <thead>
            <tr><th>Artist / Title</th><th>Genre</th><th>Synced</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="t in browseTracks"
              :key="t.track_id"
              @contextmenu.prevent="openContextMenu($event, t)"
              class="bs-browse-row"
            >
              <td class="bs-bold">{{ t.artist }} — {{ t.title }}</td>
              <td class="bs-dim">{{ t.genre || '—' }}</td>
              <td>{{ t.has_synced_lyrics ? '✓' : '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="bs-empty">
          {{ browseMode === 'library' ? 'Type to search the library.' : 'Pick a playlist to browse its tracks.' }}
        </div>
      </div>

      <!-- Live captions (TUI's live-transcription page, with model chooser) -->
      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">💬 Live Captions (Transcription)</span>
        </div>
        <div class="bs-browse-controls">
          <select class="bs-select" v-model="captionModel">
            <option value="faster-whisper-tiny">🚀 Whisper Tiny (Fast Local)</option>
            <option value="faster-whisper-base">🎯 Whisper Base (Local)</option>
            <option value="faster-whisper-small">⚡ Whisper Small (Local)</option>
            <option value="gemini-2.5-flash">✨ Gemini 2.5 Flash (API)</option>
            <option value="gemini-1.5-flash">✨ Gemini 1.5 Flash (API)</option>
            <option value="gemma-2-9b-it">💎 Gemma 2 9B (Dutch Refiner API)</option>
            <option value="gemma-3-27b-it">💎 Gemma 3 27B (Dutch Refiner API)</option>
          </select>
          <select class="bs-select" v-model="captionLanguage">
            <option value="nl">🇳🇱 Dutch (nl)</option>
            <option value="en">🇬🇧 English (en)</option>
            <option value="fr">🇫🇷 French (fr)</option>
            <option value="de">🇩🇪 German (de)</option>
            <option value="es">🇪🇸 Spanish (es)</option>
            <option value="auto">🌐 Auto Detect</option>
          </select>
          <button class="bs-btn" :class="{ active: captionsLive }" @click="toggleCaptions">
            {{ captionsLive ? '⏹ Stop' : '▶ Start Live Captions' }}
          </button>
        </div>
        <div class="bs-log bs-captions-log" v-if="captionLines.length">
          <div v-for="(c, i) in captionLines" :key="i" class="bs-log-line">{{ c }}</div>
        </div>
        <div v-else class="bs-empty">Not transcribing. Pick a model and language, then start.</div>
      </div>
    </div>

    <!-- Right-click context menu for a browsed track -->
    <div
      v-if="contextMenu.visible"
      class="bs-ctx-menu"
      :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
    >
      <div class="bs-ctx-title">{{ contextMenu.track?.artist }} — {{ contextMenu.track?.title }}</div>
      <button @click="runTrackAction('process')">⚙️ Process</button>
      <button @click="runTrackAction('reprocess')">🔁 Reprocess (force all)</button>
      <button @click="runTrackAction('whisper')">🗣️ Try Whisper</button>
      <button @click="runTrackAction('av-sync')">🎬 A-V Sync</button>
    </div>
    <div v-if="actionMsg" class="bs-toast">{{ actionMsg }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'

const workers = ref({})
const clients = ref({})
const recording = ref({ active: false, id: null })
const radioSessions = ref([])
const stagingItems = ref([])
const events = ref([])
const errorLogs = ref([])
const radio = ref({ enabled: false, status: 'stopped', artist: '', title: '', processing: '' })

const scaleBusy = ref(false)
const recordBusy = ref(false)
const kioskBusy = ref(false)
const kioskMsg = ref('')
const importingId = ref(null)
const reviewingId = ref(null)
const radioBusy = ref(false)

// -- Browse state --
const browseMode = ref('library')
const browseQuery = ref('')
const browsePlaylistId = ref('')
const browseTracks = ref([])
const playlists = ref([])
const contextMenu = ref({ visible: false, x: 0, y: 0, track: null })
const actionMsg = ref('')
let actionMsgTimer = null

// -- Live captions state --
const captionModel = ref('faster-whisper-small')
const captionLanguage = ref('nl')
const captionsLive = ref(false)
const captionLines = ref([])
let captionsSource = null

const formatTime = (ts) => {
  if (!ts) return '—'
  try {
    return new Date(ts * 1000).toLocaleString()
  } catch (e) {
    return '—'
  }
}

const fetchRadio = async () => {
  try {
    const res = await fetch('/api/radio')
    if (res.ok) radio.value = await res.json()
  } catch (e) { /* ignore */ }
}

const toggleRadio = async () => {
  if (radioBusy.value) return
  radioBusy.value = true
  try {
    const res = await fetch('/api/radio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !radio.value.enabled }),
    })
    if (res.ok) radio.value = await res.json()
  } catch (e) { /* ignore */ } finally {
    radioBusy.value = false
  }
}

const fetchWorkers = async () => {
  try {
    const res = await fetch('/api/backstage/workers')
    if (res.ok) workers.value = await res.json()
  } catch (e) { /* ignore */ }
}

const scaleWorkers = async (target) => {
  if (scaleBusy.value) return
  scaleBusy.value = true
  try {
    await fetch('/api/backstage/workers/scale', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target }),
    })
    await fetchWorkers()
  } catch (e) { /* ignore */ } finally {
    scaleBusy.value = false
  }
}

const fetchClients = async () => {
  try {
    const res = await fetch('/api/backstage/clients')
    if (res.ok) clients.value = await res.json()
  } catch (e) { /* ignore */ }
}

const fetchRecordStatus = async () => {
  try {
    const res = await fetch('/api/backstage/record/status')
    if (res.ok) {
      const data = await res.json()
      const rows = data.recording || []
      recording.value = rows.length ? { active: true, id: rows[0].recording_id ?? rows[0].id } : { active: false, id: null }
    }
  } catch (e) { /* ignore */ }
}

const toggleRecording = async () => {
  if (recordBusy.value) return
  recordBusy.value = true
  try {
    const path = recording.value.active ? '/api/backstage/record/stop' : '/api/backstage/record/start'
    const body = recording.value.active ? { recording_id: recording.value.id } : { source: 'mic', keep_audio: false }
    await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    await fetchRecordStatus()
  } catch (e) { /* ignore */ } finally {
    recordBusy.value = false
  }
}

const restartKiosk = async () => {
  if (kioskBusy.value) return
  kioskBusy.value = true
  kioskMsg.value = ''
  try {
    const res = await fetch('/api/backstage/kiosk/restart', { method: 'POST' })
    const data = await res.json()
    kioskMsg.value = data.status === 'ok' ? 'Kiosk restarted.' : (data.error || data.detail || 'Restart may have failed.')
  } catch (e) {
    kioskMsg.value = 'Restart failed.'
  } finally {
    kioskBusy.value = false
  }
}

const fetchRadioSessions = async () => {
  try {
    const res = await fetch('/api/backstage/radio/sessions?limit=10')
    if (res.ok) radioSessions.value = (await res.json()).sessions || []
  } catch (e) { /* ignore */ }
}

const importRadioSession = async (sessionId) => {
  if (importingId.value) return
  importingId.value = sessionId
  try {
    await fetch(`/api/backstage/radio/sessions/${sessionId}/import`, { method: 'POST' })
  } catch (e) { /* ignore */ } finally {
    importingId.value = null
  }
}

const fetchStagingQueue = async () => {
  try {
    const res = await fetch('/api/backstage/staging/queue?status=pending&limit=15')
    if (res.ok) stagingItems.value = (await res.json()).items || []
  } catch (e) { /* ignore */ }
}

const reviewStaged = async (itemId, action) => {
  if (reviewingId.value) return
  reviewingId.value = itemId
  try {
    await fetch(`/api/backstage/staging/queue/${itemId}/${action}`, { method: 'POST' })
    stagingItems.value = stagingItems.value.filter((i) => i.id !== itemId)
  } catch (e) { /* ignore */ } finally {
    reviewingId.value = null
  }
}

const fetchEvents = async () => {
  try {
    const res = await fetch('/api/backstage/events/recent?limit=15')
    if (res.ok) events.value = (await res.json()).events || []
  } catch (e) { /* ignore */ }
}

const fetchErrorLogs = async () => {
  try {
    const res = await fetch('/api/backstage/logs/errors?lines=15')
    if (res.ok) errorLogs.value = (await res.json()).logs || []
  } catch (e) { /* ignore */ }
}

const fetchAll = () => {
  fetchRadio()
  fetchWorkers()
  fetchClients()
  fetchRecordStatus()
  fetchRadioSessions()
  fetchStagingQueue()
  fetchEvents()
  fetchErrorLogs()
}

// -- Library / playlist browse --------------------------------------------

const onBrowseModeChange = () => {
  browseTracks.value = []
  if (browseMode.value === 'playlist') fetchPlaylists()
}

const fetchPlaylists = async () => {
  try {
    const res = await fetch('/api/playlists')
    if (res.ok) playlists.value = (await res.json()).playlists || []
  } catch (e) { /* ignore */ }
}

let librarySearchTimer = null
const debouncedLibrarySearch = () => {
  if (librarySearchTimer) clearTimeout(librarySearchTimer)
  librarySearchTimer = setTimeout(searchLibrary, 300)
}

const searchLibrary = async () => {
  if (!browseQuery.value.trim()) {
    browseTracks.value = []
    return
  }
  try {
    const res = await fetch(`/api/backstage/library/tracks?q=${encodeURIComponent(browseQuery.value)}&limit=30`)
    if (res.ok) browseTracks.value = (await res.json()).tracks || []
  } catch (e) { /* ignore */ }
}

const fetchPlaylistTracks = async () => {
  if (!browsePlaylistId.value) {
    browseTracks.value = []
    return
  }
  try {
    const res = await fetch(`/api/backstage/playlists/${encodeURIComponent(browsePlaylistId.value)}/tracks`)
    if (res.ok) browseTracks.value = (await res.json()).tracks || []
  } catch (e) { /* ignore */ }
}

// -- Right-click context menu + track actions ------------------------------

const openContextMenu = (event, track) => {
  contextMenu.value = { visible: true, x: event.clientX, y: event.clientY, track }
}

const closeContextMenu = () => {
  contextMenu.value.visible = false
}

const showActionMsg = (msg) => {
  actionMsg.value = msg
  if (actionMsgTimer) clearTimeout(actionMsgTimer)
  actionMsgTimer = setTimeout(() => { actionMsg.value = '' }, 4000)
}

const runTrackAction = async (action) => {
  const track = contextMenu.value.track
  closeContextMenu()
  if (!track) return
  const label = `${track.artist} — ${track.title}`
  try {
    let res
    if (action === 'process') {
      res = await fetch('/api/song/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_id: track.track_id }),
      })
    } else {
      res = await fetch(`/api/backstage/track/${track.track_id}/${action}`, { method: 'POST' })
    }
    const data = await res.json()
    showActionMsg(`${action} — ${label}: ${data.message || data.status || 'done'}`)
  } catch (e) {
    showActionMsg(`${action} — ${label}: failed`)
  }
}

// -- Live captions -----------------------------------------------------

const toggleCaptions = async () => {
  if (captionsLive.value) {
    if (captionsSource) {
      captionsSource.close()
      captionsSource = null
    }
    captionsLive.value = false
    return
  }
  try {
    await fetch('/api/backstage/captions/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: captionModel.value, language: captionLanguage.value }),
    })
  } catch (e) { /* ignore */ }
  captionLines.value = []
  captionsSource = new EventSource('/api/backstage/captions/stream')
  captionsSource.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data)
      const text = data.text || data.line || JSON.stringify(data)
      captionLines.value.push(text)
      if (captionLines.value.length > 100) captionLines.value.shift()
    } catch (e) {
      captionLines.value.push(ev.data)
    }
  }
  captionsSource.onerror = () => {
    captionsLive.value = false
    if (captionsSource) {
      captionsSource.close()
      captionsSource = null
    }
  }
  captionsLive.value = true
}

let pollTimer = null
onMounted(() => {
  fetchAll()
  pollTimer = setInterval(fetchAll, 10000)
  window.addEventListener('click', closeContextMenu)
})
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
  window.removeEventListener('click', closeContextMenu)
  if (captionsSource) captionsSource.close()
})
</script>

<style scoped>
.backstage {
  padding: 16px;
  max-width: 1400px;
  margin: 0 auto;
}

.bs-note {
  font-size: 0.78rem;
  color: var(--text-dim, #94a3b8);
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 14px;
}

.bs-note code {
  color: var(--accent-cyan);
  font-family: 'JetBrains Mono', monospace;
}

.bs-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 14px;
}

.bs-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}

.bs-card-wide {
  grid-column: span 2;
}

.bs-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
  margin-bottom: 10px;
}

.bs-card-title {
  font-size: 0.78rem;
  font-weight: 800;
  letter-spacing: 0.04em;
  color: var(--accent-gold);
}

.bs-stat-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.82rem;
  padding: 3px 0;
  color: #e2e8f0;
}

.bs-stat-value {
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent-cyan);
}

.bs-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.78rem;
  margin-top: 6px;
}

.bs-table th {
  text-align: left;
  color: var(--text-dim, #94a3b8);
  font-weight: 600;
  padding: 4px 6px;
  border-bottom: 1px solid var(--border);
}

.bs-table td {
  padding: 5px 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}

.bs-mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.74rem;
}

.bs-dim {
  color: var(--text-dim, #94a3b8);
}

.bs-bold {
  font-weight: 600;
}

.bs-pill {
  font-size: 0.68rem;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid var(--border);
}

.bs-pill.ok {
  background: rgba(0, 242, 254, 0.12);
  border-color: var(--accent-cyan);
  color: var(--accent-cyan);
}

.bs-actions {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

.bs-row-actions {
  display: flex;
  gap: 4px;
}

.bs-btn {
  font-size: 0.74rem;
  font-weight: 700;
  padding: 5px 10px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-primary, #fff);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--border);
}

.bs-btn:hover:not(:disabled) {
  border-color: var(--accent-cyan);
}

.bs-btn.active {
  background: var(--accent-pink);
  border-color: var(--accent-pink);
}

.bs-btn.warn:hover:not(:disabled) {
  border-color: #f87171;
  color: #f87171;
}

.bs-btn:disabled {
  opacity: 0.6;
  cursor: default;
}

.bs-mini-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-dim, #94a3b8);
  cursor: pointer;
  font-size: 0.72rem;
  padding: 2px 7px;
}

.bs-mini-btn:hover:not(:disabled) {
  color: var(--accent-cyan);
  border-color: var(--accent-cyan);
}

.bs-mini-btn.ok:hover:not(:disabled) {
  color: #4ade80;
  border-color: #4ade80;
}

.bs-mini-btn.warn:hover:not(:disabled) {
  color: #f87171;
  border-color: #f87171;
}

.bs-link {
  color: var(--accent-cyan);
  text-decoration: none;
  font-size: 0.78rem;
}

.bs-empty {
  color: var(--text-dim, #94a3b8);
  font-size: 0.78rem;
  font-style: italic;
  padding: 10px 0;
}

.bs-log {
  max-height: 220px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
}

.bs-log-line {
  padding: 2px 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  color: #cbd5e1;
}

.bs-log-line.error {
  color: #f87171;
}

@media (max-width: 900px) {
  .bs-grid { grid-template-columns: 1fr; }
  .bs-card-wide { grid-column: span 1; }
}

.bs-browse-controls {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.bs-select, .bs-input {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary, #fff);
  font-size: 0.78rem;
  padding: 5px 8px;
}

.bs-input {
  flex: 1;
  min-width: 160px;
}

.bs-browse-row {
  cursor: context-menu;
}

.bs-browse-row:hover {
  background: rgba(255, 255, 255, 0.04);
}

.bs-ctx-menu {
  position: fixed;
  z-index: 999;
  background: #1a1f2e;
  border: 1px solid var(--accent-cyan);
  border-radius: 8px;
  padding: 6px;
  min-width: 190px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}

.bs-ctx-title {
  font-size: 0.72rem;
  color: var(--text-dim, #94a3b8);
  padding: 4px 8px 6px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}

.bs-ctx-menu button {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  color: #e2e8f0;
  font-size: 0.78rem;
  padding: 6px 8px;
  border-radius: 5px;
  cursor: pointer;
}

.bs-ctx-menu button:hover {
  background: rgba(0, 242, 254, 0.12);
  color: var(--accent-cyan);
}

.bs-toast {
  position: fixed;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  background: #1a1f2e;
  border: 1px solid var(--accent-cyan);
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 0.8rem;
  color: #e2e8f0;
  z-index: 1000;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}

.bs-captions-log {
  font-size: 0.9rem;
  font-family: inherit;
}
</style>
