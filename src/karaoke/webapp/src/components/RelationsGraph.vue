<template>
  <div class="graph-container">
    <div class="graph-toolbar">
      <div class="mode-toggles">
        <button
          class="mode-btn"
          :class="{ active: mode === 'relations' }"
          @click="setMode('relations')"
        >
          🕸️ Band &amp; Projects
        </button>
        <button
          class="mode-btn"
          :class="{ active: mode === 'similarity' }"
          @click="setMode('similarity')"
        >
          🌌 Song Similarity
        </button>
      </div>

      <div class="graph-status">
        <span v-if="loading" class="spinner">⏳</span>
        <span class="status-text">{{ statusText }}</span>
      </div>

      <div class="tool-actions">
        <label class="follow-toggle" title="Re-center the graph when the playing song changes">
          <input type="checkbox" v-model="followSong" />
          Follow song
        </label>
        <button class="icon-btn" @click="fitGraph" title="Fit to Screen">⛶</button>
        <button class="icon-btn" @click="loadGraph" title="Reload">🔄</button>
      </div>
    </div>

    <!-- Similarity filter bar (strategy + genre/mood/camelot/bpm) -->
    <div class="filter-bar" v-if="mode === 'similarity'">
      <div class="filter-group">
        <span class="filter-label">Link by:</span>
        <button
          v-for="s in strategies"
          :key="s.key"
          class="strat-chip"
          :class="{ active: strategy === s.key }"
          @click="strategy = s.key; loadGraph()"
        >
          {{ s.label }}
        </button>
      </div>
      <div class="filter-group">
        <select v-model="genre" @change="loadGraph" class="filter-select">
          <option value="">All genres</option>
          <option v-for="g in genreOptions" :key="g" :value="g">{{ g }}</option>
        </select>
        <select v-model="mood" @change="loadGraph" class="filter-select">
          <option value="">All moods</option>
          <option v-for="m in moodOptions" :key="m" :value="m">{{ m }}</option>
        </select>
        <input
          v-model.number="bpmMin"
          type="number"
          placeholder="BPM min"
          class="filter-num"
          @change="loadGraph"
        />
        <input
          v-model.number="bpmMax"
          type="number"
          placeholder="BPM max"
          class="filter-num"
          @change="loadGraph"
        />
      </div>
    </div>

    <!-- Cytoscape Canvas Mount -->
    <div ref="cyMount" class="cy-canvas" @contextmenu.prevent></div>

    <!-- Right-click context menu for song nodes -->
    <div
      v-if="ctxMenu.visible"
      class="node-ctx-menu"
      :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
    >
      <div class="ctx-title">{{ ctxMenu.label }}</div>
      <button class="ctx-item" @click="ctxPlayNow">▶ Play now</button>
      <button class="ctx-item" @click="ctxQueue">➕ Queue next</button>
      <button class="ctx-item" @click="ctxDjAdd">⭐ Add to AI DJ playlist</button>
      <button class="ctx-item process" @click="ctxProcess">⚙️ Fully process (vectors · mood · chords)</button>
    </div>

    <!-- Transient toast for action feedback -->
    <div v-if="toast" class="graph-toast" :class="toast.kind">{{ toast.msg }}</div>


    <!-- Graph Legend Overlay -->
    <div class="graph-legend">
      <template v-if="mode === 'relations'">
        <span class="legend-item"><span class="legend-dot dot-center"></span> Artist</span>
        <span class="legend-item"><span class="legend-dot dot-band"></span> Band</span>
        <span class="legend-item"><span class="legend-dot dot-member"></span> Member</span>
        <span class="legend-item"><span class="legend-dot dot-side"></span> Side-Project</span>
      </template>
      <template v-else>
        <span class="legend-item"><span class="legend-dot dot-seed"></span> Now playing</span>
        <span class="legend-item">Node color = Camelot key / mood</span>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch, onBeforeUnmount } from 'vue'
import cytoscape from 'cytoscape'
import fcose from 'cytoscape-fcose'

cytoscape.use(fcose)

const props = defineProps({
  selectedArtist: { type: String, default: '' },
  selectedArtistId: { type: String, default: '' },
  currentTrackId: { type: [String, Number], default: '' },
  currentQuery: { type: String, default: '' },
})

const emit = defineEmits(['select-artist', 'play-song', 'inspect-song'])

const cyMount = ref(null)
let cy = null

const mode = ref('relations')
const loading = ref(false)
const statusText = ref('Ready')
const followSong = ref(true)

// Similarity filters
const strategy = ref('structural')
const genre = ref('')
const mood = ref('')
const bpmMin = ref(null)
const bpmMax = ref(null)

const strategies = [
  { key: 'structural', label: 'Chords' },
  { key: 'acoustic', label: 'Sound' },
  { key: 'context', label: 'Lyrics' },
  { key: 'mood', label: 'Mood' },
  { key: 'harmonic', label: 'Key/BPM' },
]

const genreOptions = ['rock', 'pop', 'jazz', 'electronic', 'hip hop', 'soul', 'folk', 'classical', 'metal', 'funk', 'blues', 'reggae']
const moodOptions = ['happy', 'tender', 'sad', 'angry', 'neutral']

const setMode = (m) => {
  mode.value = m
  loadGraph()
}

const fitGraph = () => {
  if (cy) cy.fit(undefined, 30)
}

const loadGraph = async () => {
  loading.value = true
  if (mode.value === 'relations') {
    const artist = props.selectedArtistId || props.selectedArtist || 'Joy Division'
    statusText.value = `Loading relations for ${artist}...`
    try {
      const res = await fetch(`/api/artist_relations/${encodeURIComponent(artist)}`)
      const data = await res.json()
      renderCytoscape(data.nodes || [], data.edges || [])
      statusText.value = `${data.nodes?.length || 0} entities linked to ${artist}`
    } catch (e) {
      statusText.value = 'Failed to load relations'
    } finally {
      loading.value = false
    }
  } else {
    statusText.value = 'Loading similarity constellation...'
    try {
      const params = new URLSearchParams({ limit: '6', hops: '2', strategy: strategy.value })
      if (props.currentTrackId) params.set('track_id', String(props.currentTrackId))
      else if (props.currentQuery) params.set('query', props.currentQuery)
      if (genre.value) params.set('genre', genre.value)
      if (mood.value) params.set('mood', mood.value)
      if (bpmMin.value) params.set('bpm_min', String(bpmMin.value))
      if (bpmMax.value) params.set('bpm_max', String(bpmMax.value))

      const res = await fetch(`/api/graph?${params.toString()}`)
      const data = await res.json()
      const cyNodes = (data.nodes || []).map((n) => ({
        data: {
          id: String(n.id || n.track_id),
          track_id: n.track_id,
          label: n.artist ? `${n.artist} — ${n.title}` : (n.label || 'Song'),
          artist: n.artist,
          title: n.title,
          color: (mode.value === 'similarity' && strategy.value === 'mood' && n.color) ? n.color : (n.key_color || n.color || '#00f2fe'),
          size: n.is_seed ? 70 : 42,
          type: n.is_seed ? 'seed' : 'track',
          genre: n.genre || '',
          mood: n.dominant_mood || '',
          bpm: n.bpm,
          camelot: n.camelot || '',
        },
      }))
      const cyEdges = (data.edges || []).map((e, idx) => ({
        data: {
          id: `edge_${idx}`,
          source: String(e.source),
          target: String(e.target),
          label: e.reason || (e.similarity ? `${Math.round(e.similarity * 100)}%` : ''),
          color: 'rgba(255, 255, 255, 0.18)',
        },
      }))
      renderCytoscape(cyNodes, cyEdges)
      const stats = data.stats || {}
      statusText.value = `${cyNodes.length} tracks · ${stats.strategy || strategy.value}${stats.seed_ids ? ' · seeded' : ''}`
    } catch (e) {
      statusText.value = 'Failed to load similarity graph'
    } finally {
      loading.value = false
    }
  }
}

const renderCytoscape = (nodes, edges) => {
  if (!cyMount.value) return
  if (cy) {
    cy.destroy()
  }

  cy = cytoscape({
    container: cyMount.value,
    elements: [...nodes, ...edges],
    style: [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          'label': 'data(label)',
          'color': '#f8fafc',
          'font-family': 'Outfit, sans-serif',
          'font-size': '11px',
          'font-weight': '600',
          'text-valign': 'bottom',
          'text-margin-y': 5,
          'text-max-width': '120px',
          'text-wrap': 'ellipsis',
          'width': 'data(size)',
          'height': 'data(size)',
          'border-width': 2,
          'border-color': 'rgba(255, 255, 255, 0.4)',
          'text-outline-color': '#07090e',
          'text-outline-width': 3,
        },
      },
      {
        selector: 'node[type = "center"], node[type = "seed"]',
        style: {
          'border-color': '#00f2fe',
          'border-width': 4,
          'font-size': '14px',
          'font-weight': '800',
        },
      },
      {
        selector: 'node[type = "band"]',
        style: { 'shape': 'round-rectangle', 'border-color': '#ff007f' },
      },
      {
        selector: 'node[type = "side_project"]',
        style: { 'shape': 'diamond', 'border-color': '#9d4edd' },
      },
      {
        selector: 'edge',
        style: {
          'width': 2,
          'line-color': 'data(color)',
          'target-arrow-color': 'data(color)',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'label': 'data(label)',
          'font-size': '9px',
          'color': '#94a3b8',
          'text-outline-color': '#07090e',
          'text-outline-width': 2,
        },
      },
      {
        selector: ':selected',
        style: { 'border-color': '#ffbe0b', 'border-width': 4, 'line-color': '#ffbe0b' },
      },
    ],
    layout: {
      name: 'fcose',
      quality: 'default',
      animate: true,
      animationDuration: 600,
      randomize: true,
      nodeRepulsion: 5000,
      idealEdgeLength: 90,
      fit: true,
      padding: 30,
    },
  })

  cy.on('tap', 'node', (evt) => {
    const data = evt.target.data()
    hideCtxMenu()
    // Left-click = INSPECT ONLY. Never autoplay / never disturb current playback.
    if (data.type === 'track' || data.type === 'seed') {
      if (data.artist) emit('select-artist', data.artist)
      emit('inspect-song', { track_id: data.track_id, artist: data.artist, title: data.title })
    } else {
      const artistName = data.artist || data.label
      if (artistName) emit('select-artist', { name: artistName, wikidata: data.wikidata, mbid: data.mbid })
    }
  })

  // Right-click (or long-press) on a song node → context menu of actions.
  cy.on('cxttap', 'node', (evt) => {
    const data = evt.target.data()
    if (data.type !== 'track' && data.type !== 'seed') {
      hideCtxMenu()
      return
    }
    const re = evt.renderedPosition || { x: 0, y: 0 }
    ctxMenu.value = {
      visible: true,
      x: Math.min(re.x, (cyMount.value?.clientWidth || 400) - 240),
      y: re.y,
      label: data.artist ? `${data.artist} — ${data.title || ''}` : (data.title || 'Song'),
      song: { track_id: data.track_id, artist: data.artist, title: data.title },
    }
  })

  // Dismiss menu on background tap / pan / zoom.
  cy.on('tap', (evt) => {
    if (evt.target === cy) hideCtxMenu()
  })
  cy.on('pan zoom', hideCtxMenu)
}

// --- Context menu + song actions ----------------------------------------
const ctxMenu = ref({ visible: false, x: 0, y: 0, label: '', song: null })
const toast = ref(null)
let toastTimer = null

const hideCtxMenu = () => {
  ctxMenu.value = { ...ctxMenu.value, visible: false }
}

const showToast = (msg, kind = 'ok') => {
  toast.value = { msg, kind }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toast.value = null
  }, 3200)
}

const postSongAction = async (path, song) => {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        track_id: song.track_id ?? null,
        artist: song.artist || '',
        title: song.title || '',
      }),
    })
    return await res.json()
  } catch (e) {
    return { status: 'error', error: String(e) }
  }
}

const ctxPlayNow = () => {
  const song = ctxMenu.value.song
  hideCtxMenu()
  if (song) {
    emit('play-song', song)
    showToast(`▶ Playing ${song.title || song.artist}`)
  }
}

const ctxQueue = async () => {
  const song = ctxMenu.value.song
  hideCtxMenu()
  if (!song) return
  showToast('➕ Queueing…')
  const r = await postSongAction('/api/song/queue', song)
  if (r.status === 'queued') showToast(`➕ Queued ${song.title || song.artist}`)
  else showToast(r.message || r.error || 'Could not queue this song', 'warn')
}

const ctxDjAdd = async () => {
  const song = ctxMenu.value.song
  hideCtxMenu()
  if (!song) return
  showToast('⭐ Adding to DJ list…')
  const r = await postSongAction('/api/song/dj_add', song)
  if (r.error) showToast(r.error, 'warn')
  else showToast(`⭐ Added to AI DJ playlist`)
}

const ctxProcess = async () => {
  const song = ctxMenu.value.song
  hideCtxMenu()
  if (!song) return
  showToast('⚙️ Queuing full processing…')
  const r = await postSongAction('/api/song/process', song)
  if (r.status === 'queued') showToast('⚙️ Full processing queued (vectors · mood · chords)')
  else if (r.status === 'noop') showToast('Already fully processed', 'warn')
  else showToast(r.message || r.error || 'Could not process', 'warn')
}

// Follow artist selection (relations mode)
watch(() => props.selectedArtistId || props.selectedArtist, (newVal) => {
  if (newVal && mode.value === 'relations') loadGraph()
})

// Follow currently playing song (similarity mode)
watch(() => props.currentTrackId, () => {
  if (followSong.value && mode.value === 'similarity') loadGraph()
})
watch(() => props.currentQuery, () => {
  if (followSong.value && mode.value === 'similarity' && !props.currentTrackId) loadGraph()
})

onMounted(() => {
  loadGraph()
})

onBeforeUnmount(() => {
  if (toastTimer) clearTimeout(toastTimer)
  if (cy) cy.destroy()
})
</script>

<style scoped>
.graph-container {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
  height: 100%;
  min-height: 420px;
}

.graph-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  background: rgba(0, 0, 0, 0.4);
  border-bottom: 1px solid var(--border);
  z-index: 2;
  gap: 10px;
}

.mode-toggles {
  display: flex;
  gap: 6px;
}

.mode-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 600;
  padding: 6px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
  white-space: nowrap;
}

.mode-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.mode-btn.active {
  background: rgba(0, 242, 254, 0.15);
  border-color: var(--accent-cyan);
  color: #fff;
  box-shadow: 0 0 10px rgba(0, 242, 254, 0.2);
}

.graph-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.72rem;
  color: var(--text-dim);
  flex: 1;
  justify-content: center;
}

.tool-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}

.follow-toggle {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.72rem;
  color: var(--text-muted);
  cursor: pointer;
  white-space: nowrap;
}

.icon-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.8rem;
  padding: 4px 9px;
  border-radius: 4px;
  cursor: pointer;
}

.icon-btn:hover {
  background: rgba(255, 255, 255, 0.15);
  color: #fff;
}

/* Filter bar */
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  background: rgba(0, 0, 0, 0.3);
  border-bottom: 1px solid var(--border);
  z-index: 2;
}

.filter-group {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.filter-label {
  font-size: 0.7rem;
  font-weight: 700;
  color: var(--text-dim);
}

.strat-chip {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.72rem;
  padding: 3px 9px;
  border-radius: 5px;
  cursor: pointer;
}

.strat-chip.active {
  background: rgba(157, 78, 221, 0.2);
  border-color: var(--accent-purple);
  color: #fff;
}

.filter-select,
.filter-num {
  background: rgba(0, 0, 0, 0.4);
  border: 1px solid var(--border);
  color: #e2e8f0;
  font-size: 0.72rem;
  padding: 4px 7px;
  border-radius: 5px;
  outline: none;
}

.filter-num {
  width: 72px;
}

.filter-select:focus,
.filter-num:focus {
  border-color: var(--accent-cyan);
}

.cy-canvas {
  flex: 1;
  width: 100%;
  height: 100%;
  min-height: 340px;
}

/* Right-click node context menu */
.node-ctx-menu {
  position: absolute;
  z-index: 20;
  min-width: 228px;
  background: rgba(10, 13, 20, 0.97);
  border: 1px solid var(--border-bright, rgba(255, 255, 255, 0.2));
  border-radius: 10px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.6);
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  backdrop-filter: blur(6px);
}

.ctx-title {
  font-size: 0.7rem;
  font-weight: 700;
  color: var(--accent-cyan);
  padding: 6px 8px 7px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ctx-item {
  background: none;
  border: none;
  color: #e2e8f0;
  text-align: left;
  font-size: 0.8rem;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s ease;
}

.ctx-item:hover {
  background: rgba(0, 242, 254, 0.14);
  color: #fff;
}

.ctx-item.process {
  margin-top: 3px;
  border-top: 1px solid var(--border);
  color: var(--accent-gold, #ffbe0b);
  font-size: 0.76rem;
}

/* Action feedback toast */
.graph-toast {
  position: absolute;
  bottom: 14px;
  right: 14px;
  z-index: 20;
  background: rgba(10, 13, 20, 0.96);
  border: 1px solid var(--accent-cyan);
  color: #fff;
  font-size: 0.78rem;
  padding: 8px 14px;
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.5);
  max-width: 70%;
}

.graph-toast.warn {
  border-color: var(--accent-gold, #ffbe0b);
}

.graph-legend {
  position: absolute;
  bottom: 12px;
  left: 14px;
  display: flex;
  gap: 12px;
  background: rgba(7, 9, 14, 0.85);
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  pointer-events: none;
  font-size: 0.68rem;
  color: var(--text-muted);
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.dot-center { background: #00f2fe; }
.dot-seed { background: #00f2fe; box-shadow: 0 0 6px #00f2fe; }
.dot-band { background: #ff007f; border-radius: 2px; }
.dot-member { background: #ffbe0b; }
.dot-side { background: #9d4edd; }
</style>
