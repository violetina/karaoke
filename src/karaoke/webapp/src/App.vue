<template>
  <div class="app-layout">
    <!-- Top Navigation & System Health Header -->
    <header class="app-header">
      <div class="brand">
        <span class="brand-logo">🎤</span>
        <div class="brand-text">
          <span class="brand-title">KARAOKE STAGE &amp; AI DJ</span>
          <span class="brand-subtitle">Unified Control Platform</span>
        </div>
      </div>

      <!-- Live Service Health Indicators -->
      <div class="health-badges">
        <span class="health-pill" :class="{ online: health.ctrl_api_8765 }" title="Host Stage & Prompter (:8765)">
          <span class="dot"></span> :8765 Stage
        </span>
        <span class="health-pill" :class="{ online: health.mcp_api_8888 }" title="Similarity & MCP (:8888)">
          <span class="dot"></span> :8888 Graph
        </span>
        <span class="health-pill" :class="{ online: health.opensearch_9200 }" title="OpenSearch Cache (:9200)">
          <span class="dot"></span> :9200 OpenSearch
        </span>
        <span class="health-pill" :class="{ online: health.ollama_11434 }" title="Ollama AI DJ (:11434)">
          <span class="dot"></span> :11434 Ollama
        </span>
      </div>

      <!-- View Selector -->
      <nav class="nav-tabs">
        <button
          class="tab-btn"
          :class="{ active: currentTab === 'unified' }"
          @click="currentTab = 'unified'"
        >
          ★ Unified Hub
        </button>
        <button
          class="tab-btn"
          :class="{ active: currentTab === 'tv' }"
          @click="currentTab = 'tv'"
        >
          📺 Live Prompter (:8765/tv)
        </button>
        <button
          class="tab-btn"
          :class="{ active: currentTab === 'mood' }"
          @click="currentTab = 'mood'"
        >
          ✨ Mood Reactive (:8765/mood)
        </button>
        <button
          class="tab-btn"
          :class="{ active: currentTab === 'graph' }"
          @click="currentTab = 'graph'"
        >
          🌌 Full Constellation (:8888)
        </button>
      </nav>
    </header>

    <!-- Main Content Area -->
    <main class="app-main">
      <!-- Unified View -->
      <div v-show="currentTab === 'unified'" class="unified-grid">
        <div class="grid-left-col">
          <!-- Stage & Live Prompter Widget -->
          <StagePrompter
            :stage="stageState"
            @select-artist="handleSelectArtist"
            @play-track="handlePlayTrack"
          >
            <AiDjChat
              class="embedded"
              :current-artist="stageState.artist"
              :current-title="stageState.title"
              @focus-artist="handleSelectArtist"
              @queue-track="handleQueueTrack"
            />
          </StagePrompter>

          <!-- Interactive Relations Graph (Cytoscape) -->
          <RelationsGraph
            :selected-artist="selectedArtist"
            :selected-artist-id="selectedArtistId"
            :current-query="currentSongQuery"
            @select-artist="handleSelectArtist"
            @play-song="handlePlaySong"
            @inspect-song="handleInspectSong"
          />
        </div>

        <div class="grid-right-col">
          <!-- Player controls (transport + quick play) -->
          <PlayerControls
            :stage="stageState"
            @play-track="handlePlayTrack"
          />

          <!-- Artist Wikidata Knowledge Info-Box -->
          <ArtistInfoBox
            :artist="artistInfo"
            :loading="artistLoading"
            @select-artist="handleSelectArtist"
            @play-track="handlePlayTrack"
          />

          <!-- Library songs for the selected artist -->
          <SongList
            :artist-name="selectedArtist"
            :songs="artistSongs"
            :loading="songsLoading"
            :current-title="stageState.title"
            @play-song="handlePlaySong"
            @focus-song="handleFocusSong"
          />

          <!-- Playlists (last block) -->
          <PlaylistsPanel @explore-playlist="handleExplorePlaylist" />
        </div>
      </div>

      <!-- Embedded Dedicated Fullscreen Views -->
      <div v-if="currentTab === 'tv'" class="iframe-view">
        <iframe src="http://127.0.0.1:8765/tv" frameborder="0"></iframe>
      </div>
      <div v-if="currentTab === 'mood'" class="iframe-view">
        <iframe src="http://127.0.0.1:8765/mood" frameborder="0"></iframe>
      </div>
      <div v-if="currentTab === 'graph'" class="iframe-view">
        <iframe src="http://127.0.0.1:8888/graph" frameborder="0"></iframe>
      </div>
    </main>


  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import StagePrompter from './components/StagePrompter.vue'
import PlayerControls from './components/PlayerControls.vue'
import RelationsGraph from './components/RelationsGraph.vue'
import ArtistInfoBox from './components/ArtistInfoBox.vue'
import SongList from './components/SongList.vue'
import PlaylistsPanel from './components/PlaylistsPanel.vue'
import AiDjChat from './components/AiDjChat.vue'

const currentTab = ref('unified')

const health = ref({
  ctrl_api_8765: false,
  mcp_api_8888: false,
  opensearch_9200: false,
  ollama_11434: false,
})

const stageState = ref({
  artist: '',
  title: '',
  album: '',
  art_url: '',
  status: 'Stopped',
  bpm: 120,
  key: '8B',
  energy: 0.5,
  mood: 'neutral',
  active_line_index: -1,
  lines: [],
  upcoming_queue: [],
})

const selectedArtist = ref('Joy Division')
const selectedArtistId = ref('')
const artistInfo = ref({})
const artistLoading = ref(false)

const artistSongs = ref([])
const songsLoading = ref(false)

// Query used to seed the similarity graph on the currently playing track.
const currentSongQuery = computed(() => {
  const a = stageState.value.artist
  const t = stageState.value.title
  if (a && t) return `${a} - ${t}`
  return a || t || ''
})

let pollTimer = null

const fetchHealth = async () => {
  try {
    const res = await fetch('/api/health')
    if (res.ok) {
      health.value = await res.json()
    }
  } catch (e) {
    // offline
  }
}

const fetchStageState = async () => {
  try {
    const res = await fetch('/api/stage/state')
    if (res.ok) {
      const data = await res.json()
      stageState.value = data
    }
  } catch (e) {
    // retry on next tick
  }
}

const fetchArtistSongs = async (artistName, request = artistRequest) => {
  if (!artistName) return
  songsLoading.value = true
  try {
    const res = await fetch(`/api/artist_songs/${encodeURIComponent(artistName)}`)
    if (res.ok) {
      const data = await res.json()
      if (request === artistRequest) artistSongs.value = data.songs || []
    }
  } catch (e) {
    if (request === artistRequest) artistSongs.value = []
  } finally {
    if (request === artistRequest) songsLoading.value = false
  }
}

let artistRequest = 0
const handleSelectArtist = async (selection, playingSong = null) => {
  if (!selection) return
  const name = typeof selection === 'string' ? selection : selection.name
  const identifier = typeof selection === 'string' ? selection : (selection.wikidata || selection.mbid || name)
  const request = ++artistRequest
  selectedArtist.value = name || identifier
  selectedArtistId.value = typeof selection === 'object' ? (selection.wikidata || selection.mbid || '') : ''
  artistLoading.value = true
  artistInfo.value = {}
  artistSongs.value = []
  try {
    const res = playingSong
      ? await fetch('/api/artist/now-playing', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ artist: playingSong.artist, title: playingSong.title || '' }),
        })
      : await fetch(`/api/artist/${encodeURIComponent(identifier)}`)
    if (!res.ok) throw new Error('Artist lookup failed')
    const data = await res.json()
    const artist = playingSong ? data.artist : data
    if (request !== artistRequest) return
    artistInfo.value = artist
    selectedArtist.value = artist.name || name
    fetchArtistSongs(selectedArtist.value)
  } catch (e) {
    if (request === artistRequest) artistInfo.value = { name, description: 'Artist details unavailable' }
  } finally {
    if (request === artistRequest) artistLoading.value = false
  }
}

// Both SSE and polling update stageState. Only song identity changes refresh
// artist context; a manual graph inspection survives all same-song frames.
watch(() => JSON.stringify([stageState.value.artist, stageState.value.title]), () => {
  if (stageState.value.artist && stageState.value.title) {
    handleSelectArtist(stageState.value.artist, { ...stageState.value })
  }
})

const handlePlayTrack = async (queryOrArtist) => {
  if (!queryOrArtist) return
  try {
    await fetch('/api/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist: queryOrArtist, title: '' }),
    })
    setTimeout(fetchStageState, 1200)
  } catch (e) {
    console.error('Play failed:', e)
  }
}

const handlePlaySong = async (song) => {
  if (!song) return
  try {
    await fetch('/api/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist: song.artist || '', title: song.title || '' }),
    })
    setTimeout(fetchStageState, 1200)
  } catch (e) {
    console.error('Play song failed:', e)
  }
}

const handleFocusSong = (song) => {
  // Inspect the song's artist; the graph in similarity mode follows the current query.
  if (song?.artist) handleSelectArtist(song.artist)
}

const handleInspectSong = (song) => {
  // Left-click on a graph node: load artist/song context WITHOUT playing anything.
  if (song?.artist) handleSelectArtist(song.artist)
}

const handleExplorePlaylist = (pl) => {
  // Open the playlist in the graph explorer (new tab) seeded by its id.
  if (pl?.playlist_id) {
    window.open(`http://127.0.0.1:8888/graph?playlist_id=${encodeURIComponent(pl.playlist_id)}`, '_blank')
  }
}

const handleQueueTrack = async (trackQuery) => {
  if (!trackQuery) return
  try {
    const response = await fetch('/api/song/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: trackQuery }),
    })
    const result = await response.json()
    if (!response.ok || !['ok', 'queued'].includes(result.status)) {
      window.alert(result.message || result.error || 'Could not queue this track; playback was left unchanged.')
    }
  } catch (error) {
    window.alert('Queue request failed; playback was left unchanged.')
  }
}

// Live stage state over SSE (10Hz) for smooth word highlighting + scrolling.
let stageSource = null
const startStageStream = () => {
  try {
    stageSource = new EventSource('/api/stage/stream')
    stageSource.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data)
        if (data && (data.artist !== undefined || data.status !== undefined)) {
          stageState.value = data
        }
      } catch (e) {
        // ignore malformed frame
      }
    }
    stageSource.onerror = () => {
      // Drop and let the periodic poll keep things alive; reconnect shortly.
      if (stageSource) {
        stageSource.close()
        stageSource = null
      }
      setTimeout(startStageStream, 3000)
    }
  } catch (e) {
    // SSE unavailable — fall back to polling only.
  }
}

onMounted(() => {
  fetchHealth()
  fetchStageState()
  startStageStream()

  // Lightweight poll: health + a stage-state backstop if SSE drops.
  pollTimer = setInterval(() => {
    fetchHealth()
    if (!stageSource) fetchStageState()
  }, 2000)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (stageSource) stageSource.close()
})
</script>

<style scoped>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  background: var(--bg);
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 24px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  z-index: 10;
  flex-shrink: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand-logo {
  font-size: 1.6rem;
}

.brand-text {
  display: flex;
  flex-direction: column;
}

.brand-title {
  font-size: 1.05rem;
  font-weight: 900;
  letter-spacing: 0.05em;
  background: linear-gradient(135deg, #fff, #94a3b8);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.brand-subtitle {
  font-size: 0.68rem;
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent-cyan);
}

.health-badges {
  display: flex;
  gap: 8px;
}

.health-pill {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.7rem;
  padding: 3px 8px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  color: #64748b;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.health-pill .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #ef4444;
}

.health-pill.online {
  color: #e2e8f0;
  border-color: rgba(16, 185, 129, 0.4);
}

.health-pill.online .dot {
  background: #10b981;
  box-shadow: 0 0 6px #10b981;
}

.nav-tabs {
  display: flex;
  gap: 6px;
}

.tab-btn {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.tab-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.tab-btn.active {
  background: rgba(0, 242, 254, 0.15);
  border-color: var(--accent-cyan);
  color: #fff;
  box-shadow: 0 0 10px rgba(0, 242, 254, 0.2);
}

.app-main {
  flex: 1;
  overflow: hidden;
  position: relative;
  display: flex;
  flex-direction: column;
}

.unified-grid {
  display: grid;
  grid-template-columns: 2.1fr 0.9fr;
  gap: 16px;
  padding: 16px 20px;
  height: 100%;
  overflow-y: auto;
}

.grid-left-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 0;
  min-width: 0;
  overflow-y: auto;
}

.grid-right-col {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 0;
  overflow-y: auto;
}

.iframe-view {
  width: 100%;
  height: 100%;
  flex: 1;
}

.iframe-view iframe {
  width: 100%;
  height: 100%;
  border: none;
}

@media (max-width: 1024px) {
  .unified-grid {
    grid-template-columns: 1fr;
  }
}
</style>
