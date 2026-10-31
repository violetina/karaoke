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
    </div>
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

const scaleBusy = ref(false)
const recordBusy = ref(false)
const kioskBusy = ref(false)
const kioskMsg = ref('')
const importingId = ref(null)
const reviewingId = ref(null)

const formatTime = (ts) => {
  if (!ts) return '—'
  try {
    return new Date(ts * 1000).toLocaleString()
  } catch (e) {
    return '—'
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
  fetchWorkers()
  fetchClients()
  fetchRecordStatus()
  fetchRadioSessions()
  fetchStagingQueue()
  fetchEvents()
  fetchErrorLogs()
}

let pollTimer = null
onMounted(() => {
  fetchAll()
  pollTimer = setInterval(fetchAll, 10000)
})
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
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
</style>
