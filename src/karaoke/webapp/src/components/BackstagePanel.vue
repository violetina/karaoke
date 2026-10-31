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
      <!-- Playback controls (main kiosk / YouTube Music player) -->
      <div class="bs-card">
        <div class="bs-card-header">
          <span class="bs-card-title">⏯️ Playback Controls</span>
          <button class="bs-mini-btn" @click="fetchNowPlaying" title="Refresh">↻</button>
        </div>
        <div class="bs-stat-row" v-if="nowPlaying.metadata">
          <span>Now playing</span>
          <span class="bs-stat-value">{{ nowPlaying.metadata.artist }} — {{ nowPlaying.metadata.title }}</span>
        </div>
        <div class="bs-stat-row">
          <span>Status</span>
          <span class="bs-pill" :class="{ ok: nowPlaying.status === 'Playing' }">{{ nowPlaying.status || 'unknown' }}</span>
        </div>
        <div class="bs-actions">
          <button class="bs-btn" @click="playerAction('previous')" title="Previous">⏮</button>
          <button class="bs-btn active" @click="playerAction('play-pause')" title="Play/Pause">
            {{ nowPlaying.status === 'Playing' ? '⏸' : '▶' }}
          </button>
          <button class="bs-btn" @click="playerAction('next')" title="Next">⏭</button>
          <button class="bs-btn" @click="playerSeek(-10)" title="Back 10s">« 10s</button>
          <button class="bs-btn" @click="playerSeek(10)" title="Forward 10s">10s »</button>
        </div>
      </div>

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
          <button
            v-if="browseMode === 'playlist' && browsePlaylistId"
            class="bs-btn"
            @click="bulkProcessPlaylist"
            :disabled="bulkProcessBusy"
          >
            {{ bulkProcessBusy ? 'Queuing…' : '📦 Bulk Process This Playlist' }}
          </button>
        </div>
        <div v-if="bulkProcessMsg" class="bs-empty">{{ bulkProcessMsg }}</div>
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

      <!-- Library gap-fill (wires scripts/fill_analysis_and_vector_gaps.py) -->
      <div class="bs-card bs-card-wide">
        <div class="bs-card-header">
          <span class="bs-card-title">🧩 Fill Missing Vectors / Analysis (library-wide)</span>
          <button class="bs-mini-btn" @click="fetchGapFillStatus" title="Refresh">↻</button>
        </div>
        <div class="bs-stat-row">
          <span>Status</span>
          <span class="bs-pill" :class="{ ok: gapFill.running }">{{ gapFill.running ? 'running' : 'idle' }}</span>
        </div>
        <div class="bs-actions">
          <button class="bs-btn active" @click="startGapFill" :disabled="gapFill.running || gapFillBusy">
            {{ gapFillBusy ? '…' : '▶ Start Gap-Fill' }}
          </button>
          <button class="bs-btn warn" @click="stopGapFill" :disabled="!gapFill.running || gapFillBusy">⏹ Stop</button>
        </div>
        <div class="bs-log" v-if="gapFill.logTail.length">
          <div v-for="(l, i) in gapFill.logTail" :key="i" class="bs-log-line">{{ l }}</div>
        </div>
        <div v-else class="bs-empty">
          Scans the whole library for missing Key/BPM, genre/CLAP labels and vectors, then rebuilds the
          OpenSearch index. Can take minutes to hours — runs as a background systemd unit, safe to close this page.
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
      <button @click="openLineLooper(contextMenu.track)">🎯 Line Looper (fix word sync)</button>
    </div>
    <div v-if="actionMsg" class="bs-toast">{{ actionMsg }}</div>

    <!-- Line Looper modal -->
    <div v-if="looper.open" class="bs-modal-overlay" @click.self="closeLineLooper">
      <div class="bs-modal">
        <div class="bs-modal-header">
          <span class="bs-card-title">🎯 Line Looper — {{ looper.track?.artist }} — {{ looper.track?.title }}</span>
          <button class="bs-mini-btn" @click="closeLineLooper">✕</button>
        </div>

        <div v-if="!looper.hasLocalFile" class="bs-empty">
          No locally cached {{ looper.mediaType }} file for this track yet — Process it first, then reopen.
        </div>
        <template v-else>
          <div class="bs-browse-controls">
            <select class="bs-select" v-model="looper.mediaType" @change="loadLooperTrack">
              <option value="audio">🎧 Audio</option>
              <option value="video">🎬 Video</option>
            </select>
            <select class="bs-select" v-model.number="looper.lineIndex" @change="selectLooperLine">
              <option v-for="l in looper.lines" :key="l.index" :value="l.index">
                [{{ l.start.toFixed(2) }}s] {{ l.text }}
              </option>
            </select>
          </div>

          <audio v-if="looper.mediaType === 'audio'" ref="looperMediaEl" :src="looperMediaUrl"
                 @timeupdate="onLooperTimeUpdate"></audio>
          <video v-else ref="looperMediaEl" :src="looperMediaUrl" class="bs-looper-video"
                 @timeupdate="onLooperTimeUpdate"></video>

          <div class="bs-looper-line-preview">{{ looper.words.join(' ') }}</div>

          <div class="bs-looper-stage" v-if="looper.stage === 'nudge'">
            <div class="bs-stat-row">
              <span>Loop start</span><span class="bs-stat-value">{{ looper.loopStart.toFixed(3) }}s</span>
            </div>
            <div class="bs-actions">
              <button class="bs-btn" @click="nudge(-0.5)">« -500ms</button>
              <button class="bs-btn" @click="nudge(-0.05)">‹ -50ms</button>
              <button class="bs-btn" :class="{ active: looper.looping }" @click="toggleLoop">
                {{ looper.looping ? '⏸ Pause loop' : '▶ Loop from here' }}
              </button>
              <button class="bs-btn" @click="nudge(0.05)">+50ms ›</button>
              <button class="bs-btn" @click="nudge(0.5)">+500ms »</button>
            </div>
            <div class="bs-actions">
              <button class="bs-btn active" @click="confirmStart">✓ Start is right — play line</button>
            </div>
          </div>

          <div class="bs-looper-stage" v-if="looper.stage === 'listen'">
            <div class="bs-empty">Playing the line through once. Click (or press Enter) the instant you hear the last word.</div>
            <div class="bs-actions">
              <button class="bs-btn active" @click="confirmEnd" @keyup.enter="confirmEnd" tabindex="0">
                ⏎ Last word now
              </button>
              <button class="bs-btn warn" @click="backToNudge">« Back / nudge again</button>
            </div>
          </div>

          <div class="bs-looper-stage" v-if="looper.stage === 'review'">
            <div class="bs-stat-row">
              <span>Confirmed start</span><span class="bs-stat-value">{{ looper.confirmedStart.toFixed(3) }}s</span>
            </div>
            <div class="bs-stat-row">
              <span>Confirmed last word</span><span class="bs-stat-value">{{ looper.confirmedEnd.toFixed(3) }}s</span>
            </div>
            <table class="bs-table">
              <thead><tr><th>Word</th><th>Calculated time</th></tr></thead>
              <tbody>
                <tr v-for="(w, i) in looper.words" :key="i">
                  <td>{{ w }}</td>
                  <td class="bs-mono">{{ looper.wordTimes[i]?.toFixed(3) }}s</td>
                </tr>
              </tbody>
            </table>
            <div class="bs-actions">
              <button class="bs-btn active" @click="saveLineLooper" :disabled="looper.saving">
                {{ looper.saving ? 'Saving…' : '💾 Save corrected line' }}
              </button>
              <button class="bs-btn warn" @click="backToNudge">« Redo</button>
            </div>
          </div>
        </template>
        <div v-if="looper.msg" class="bs-empty">{{ looper.msg }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

const workers = ref({})
const clients = ref({})
const recording = ref({ active: false, id: null })
const radioSessions = ref([])
const stagingItems = ref([])
const events = ref([])
const errorLogs = ref([])
const radio = ref({ enabled: false, status: 'stopped', artist: '', title: '', processing: '' })
const nowPlaying = ref({ status: '', metadata: null })

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
const bulkProcessBusy = ref(false)
const bulkProcessMsg = ref('')

// -- Library gap-fill state --
const gapFill = ref({ running: false, logTail: [] })
const gapFillBusy = ref(false)

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

const fetchNowPlaying = async () => {
  try {
    const res = await fetch('/api/players/current')
    if (res.ok) {
      const data = await res.json()
      nowPlaying.value = { status: data.status || '', metadata: data.metadata || null }
    }
  } catch (e) { /* ignore */ }
}

const playerAction = async (action) => {
  try {
    await fetch(`/api/players/${action}`, { method: 'POST' })
  } catch (e) { /* ignore */ } finally {
    setTimeout(fetchNowPlaying, 300)
  }
}

const playerSeek = async (offsetS) => {
  try {
    await fetch('/api/players/seek', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ offset_s: offsetS }),
    })
  } catch (e) { /* ignore */ } finally {
    setTimeout(fetchNowPlaying, 300)
  }
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
  fetchNowPlaying()
  fetchRadio()
  fetchWorkers()
  fetchClients()
  fetchRecordStatus()
  fetchRadioSessions()
  fetchStagingQueue()
  fetchEvents()
  fetchErrorLogs()
  fetchGapFillStatus()
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

const bulkProcessPlaylist = async () => {
  if (!browsePlaylistId.value || bulkProcessBusy.value) return
  bulkProcessBusy.value = true
  bulkProcessMsg.value = ''
  try {
    const res = await fetch(
      `/api/backstage/playlists/${encodeURIComponent(browsePlaylistId.value)}/bulk-process`,
      { method: 'POST' },
    )
    const data = await res.json()
    bulkProcessMsg.value = data.message || (data.status === 'ok' ? 'Done.' : 'Failed.')
  } catch (e) {
    bulkProcessMsg.value = 'Bulk process failed.'
  } finally {
    bulkProcessBusy.value = false
  }
}

// -- Library gap-fill -------------------------------------------------

const fetchGapFillStatus = async () => {
  try {
    const res = await fetch('/api/backstage/gap-fill/status?lines=20')
    if (res.ok) {
      const data = await res.json()
      gapFill.value = { running: !!data.running, logTail: data.log_tail || [] }
    }
  } catch (e) { /* ignore */ }
}

const startGapFill = async () => {
  if (gapFillBusy.value) return
  gapFillBusy.value = true
  try {
    const res = await fetch('/api/backstage/gap-fill/start', { method: 'POST' })
    const data = await res.json()
    if (data.status !== 'ok') showActionMsg(`Gap-fill: ${data.error || 'failed to start'}`)
    await fetchGapFillStatus()
  } catch (e) {
    showActionMsg('Gap-fill: failed to start')
  } finally {
    gapFillBusy.value = false
  }
}

const stopGapFill = async () => {
  if (gapFillBusy.value) return
  gapFillBusy.value = true
  try {
    await fetch('/api/backstage/gap-fill/stop', { method: 'POST' })
    await fetchGapFillStatus()
  } catch (e) { /* ignore */ } finally {
    gapFillBusy.value = false
  }
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

// -- Line Looper (manual loop-and-confirm word-sync correction) -----------

const looper = ref({
  open: false,
  track: null,
  mediaType: 'audio',
  hasLocalFile: false,
  lines: [],
  lineIndex: null,
  words: [],
  bpm: null,
  stage: 'nudge',   // nudge -> listen -> review
  loopStart: 0,
  clipLen: 4,
  looping: false,
  confirmedStart: 0,
  confirmedEnd: 0,
  wordTimes: [],
  saving: false,
  msg: '',
})
const looperMediaEl = ref(null)

const looperMediaUrl = computed(() => {
  if (!looper.value.track) return ''
  return `http://127.0.0.1:8765/api/line-looper/media/${looper.value.track.track_id}?media_type=${looper.value.mediaType}`
})

const closeContextMenuOnly = () => { contextMenu.value.visible = false }

const openLineLooper = (track) => {
  closeContextMenuOnly()
  if (!track) return
  looper.value = {
    open: true, track, mediaType: 'audio', hasLocalFile: false,
    lines: [], lineIndex: null, words: [], bpm: null, stage: 'nudge',
    loopStart: 0, clipLen: 4, looping: false, confirmedStart: 0, confirmedEnd: 0,
    wordTimes: [], saving: false, msg: '',
  }
  loadLooperTrack()
}

const closeLineLooper = () => {
  if (looperMediaEl.value) looperMediaEl.value.pause()
  looper.value.open = false
}

const loadLooperTrack = async () => {
  if (!looper.value.track) return
  looper.value.msg = ''
  try {
    const res = await fetch(
      `/api/backstage/line-looper/track/${looper.value.track.track_id}?media_type=${looper.value.mediaType}`
    )
    const data = await res.json()
    if (data.status !== 'ok') {
      looper.value.msg = data.message || 'No synced lyrics for this track/mode.'
      looper.value.lines = []
      return
    }
    looper.value.lines = data.lines || []
    looper.value.bpm = data.bpm
    looper.value.hasLocalFile = !!data.has_local_file
    if (looper.value.lines.length) {
      looper.value.lineIndex = looper.value.lines[0].index
      selectLooperLine()
    }
  } catch (e) {
    looper.value.msg = 'Failed to load track data.'
  }
}

const selectLooperLine = () => {
  const line = looper.value.lines.find((l) => l.index === looper.value.lineIndex)
  if (!line) return
  looper.value.words = line.words
  looper.value.loopStart = line.start
  const next = looper.value.lines.find((l) => l.index === looper.value.lineIndex + 1)
  looper.value.clipLen = next ? Math.min(8, Math.max(1.5, next.start - line.start)) : 4
  looper.value.stage = 'nudge'
  looper.value.looping = false
  if (looperMediaEl.value) looperMediaEl.value.pause()
}

const nudge = (delta) => {
  looper.value.loopStart = Math.max(0, looper.value.loopStart + delta)
  if (looper.value.looping && looperMediaEl.value) {
    looperMediaEl.value.currentTime = looper.value.loopStart
  }
}

const toggleLoop = () => {
  const el = looperMediaEl.value
  if (!el) return
  if (looper.value.looping) {
    el.pause()
    looper.value.looping = false
  } else {
    el.currentTime = looper.value.loopStart
    el.play()
    looper.value.looping = true
  }
}

const onLooperTimeUpdate = () => {
  const el = looperMediaEl.value
  if (!el) return
  if (looper.value.stage === 'nudge' && looper.value.looping) {
    if (el.currentTime >= looper.value.loopStart + looper.value.clipLen) {
      el.currentTime = looper.value.loopStart
    }
  } else if (looper.value.stage === 'listen') {
    // Safety stop well past the clip so a missed click doesn't play forever.
    if (el.currentTime >= looper.value.loopStart + looper.value.clipLen + 4) {
      el.pause()
    }
  }
}

const confirmStart = () => {
  const el = looperMediaEl.value
  if (!el) return
  el.pause()
  looper.value.looping = false
  looper.value.confirmedStart = looper.value.loopStart
  looper.value.stage = 'listen'
  el.currentTime = looper.value.loopStart
  el.play()
}

const confirmEnd = async () => {
  const el = looperMediaEl.value
  if (!el) return
  looper.value.confirmedEnd = el.currentTime
  el.pause()
  if (looper.value.confirmedEnd <= looper.value.confirmedStart) {
    looper.value.msg = 'That was before the confirmed start — try again.'
    return
  }
  looper.value.msg = ''
  try {
    const res = await fetch('/api/backstage/line-looper/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        track_id: looper.value.track.track_id,
        media_type: looper.value.mediaType,
        old_start: looper.value.lines.find((l) => l.index === looper.value.lineIndex).start,
        new_start: looper.value.confirmedStart,
        new_end: looper.value.confirmedEnd,
        words: looper.value.words,
        dry_run: true,
      }),
    })
    const data = await res.json()
    if (data.status === 'ok') {
      looper.value.wordTimes = data.word_times
      looper.value.stage = 'review'
    } else {
      looper.value.msg = data.message || 'Could not calculate word times.'
    }
  } catch (e) {
    looper.value.msg = 'Could not calculate word times.'
  }
}

const backToNudge = () => {
  if (looperMediaEl.value) looperMediaEl.value.pause()
  looper.value.looping = false
  looper.value.stage = 'nudge'
}

const saveLineLooper = async () => {
  if (looper.value.saving) return
  looper.value.saving = true
  try {
    const res = await fetch('/api/backstage/line-looper/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        track_id: looper.value.track.track_id,
        media_type: looper.value.mediaType,
        old_start: looper.value.lines.find((l) => l.index === looper.value.lineIndex).start,
        new_start: looper.value.confirmedStart,
        new_end: looper.value.confirmedEnd,
        words: looper.value.words,
        dry_run: false,
      }),
    })
    const data = await res.json()
    looper.value.msg = data.message || (data.status === 'ok' ? 'Saved.' : 'Save failed.')
    if (data.status === 'ok') {
      // Refresh so the just-corrected line's text reflects the new save.
      await loadLooperTrack()
    }
  } catch (e) {
    looper.value.msg = 'Save failed.'
  } finally {
    looper.value.saving = false
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
const onLooperKeydown = (ev) => {
  if (ev.key === 'Enter' && looper.value.open && looper.value.stage === 'listen') {
    ev.preventDefault()
    confirmEnd()
  }
}

onMounted(() => {
  fetchAll()
  pollTimer = setInterval(fetchAll, 10000)
  window.addEventListener('click', closeContextMenu)
  window.addEventListener('keydown', onLooperKeydown)
})
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
  window.removeEventListener('click', closeContextMenu)
  window.removeEventListener('keydown', onLooperKeydown)
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

.bs-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
}

.bs-modal {
  background: var(--bg-card);
  border: 1px solid var(--accent-cyan);
  border-radius: 14px;
  padding: 18px 20px;
  width: min(640px, 92vw);
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
}

.bs-modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  padding-bottom: 10px;
  margin-bottom: 12px;
}

.bs-looper-video {
  width: 100%;
  max-height: 260px;
  border-radius: 8px;
  margin-bottom: 10px;
  background: #000;
}

.bs-looper-line-preview {
  font-size: 1.05rem;
  font-weight: 700;
  color: #e2e8f0;
  padding: 8px 0;
  text-align: center;
}

.bs-looper-stage {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border);
}
</style>
