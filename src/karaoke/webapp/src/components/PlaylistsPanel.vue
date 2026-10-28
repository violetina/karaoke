<template>
  <div class="playlists-card">
    <div class="pl-header">
      <span class="pl-title">🎶 PLAYLISTS</span>
      <span class="pl-count" v-if="playlists.length">{{ playlists.length }}</span>
    </div>

    <div v-if="loading" class="pl-loading">⏳ Loading playlists…</div>
    <div v-else-if="playlists.length === 0" class="pl-empty">No playlists found.</div>

    <div v-else class="pl-scroll">
      <div
        v-for="pl in playlists"
        :key="pl.playlist_id"
        class="pl-row"
        :class="{ loading: playingId === pl.playlist_id }"
      >
        <button class="pl-play-btn" @click="playPlaylist(pl)" :disabled="playingId === pl.playlist_id" title="Play this playlist">
          {{ playingId === pl.playlist_id ? '…' : '▶' }}
        </button>
        <div class="pl-info" @click="playPlaylist(pl)">
          <span class="pl-name">{{ pl.name }}</span>
          <span class="pl-tracks">{{ pl.track_count }} tracks</span>
        </div>
        <button class="pl-graph-btn" @click.stop="$emit('explore-playlist', pl)" title="Explore in graph">🕸️</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'

defineEmits(['explore-playlist'])

const playlists = ref([])
const loading = ref(true)
const playingId = ref(null)

const fetchPlaylists = async () => {
  loading.value = true
  try {
    const res = await fetch('/api/playlists')
    if (res.ok) {
      const data = await res.json()
      playlists.value = data.playlists || []
    }
  } catch (e) {
    playlists.value = []
  } finally {
    loading.value = false
  }
}

const playPlaylist = async (pl) => {
  if (playingId.value) return
  playingId.value = pl.playlist_id
  try {
    await fetch('/api/playlists/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ playlist_id: pl.playlist_id }),
    })
  } catch (e) {
    // ignore
  } finally {
    setTimeout(() => {
      playingId.value = null
    }, 1500)
  }
}

onMounted(fetchPlaylists)
</script>

<style scoped>
.playlists-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  min-height: 220px;
  max-height: 340px;
  flex-shrink: 0;
  overflow: hidden;
}

.pl-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
  margin-bottom: 8px;
}

.pl-title {
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: var(--accent-gold);
}

.pl-count {
  font-size: 0.7rem;
  color: var(--accent-cyan);
  font-family: 'JetBrains Mono', monospace;
  background: rgba(0, 242, 254, 0.1);
  padding: 2px 7px;
  border-radius: 4px;
  border: 1px solid rgba(0, 242, 254, 0.3);
}

.pl-loading,
.pl-empty {
  color: var(--text-dim);
  font-size: 0.8rem;
  text-align: center;
  padding: 24px 10px;
  font-style: italic;
}

.pl-scroll {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1;
  padding-right: 4px;
}

.pl-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid transparent;
  transition: all 0.15s ease;
}

.pl-row:hover {
  background: rgba(255, 255, 255, 0.06);
  border-color: var(--border-bright);
}

.pl-row.loading {
  opacity: 0.6;
}

.pl-play-btn {
  background: rgba(255, 0, 127, 0.15);
  border: 1px solid var(--accent-pink);
  color: var(--accent-pink);
  font-size: 0.7rem;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
  transition: transform 0.15s ease;
}

.pl-play-btn:hover:not(:disabled) {
  transform: scale(1.15);
  background: var(--accent-pink);
  color: #fff;
}

.pl-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  cursor: pointer;
}

.pl-name {
  font-size: 0.82rem;
  font-weight: 600;
  color: #f1f5f9;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pl-tracks {
  font-size: 0.68rem;
  color: var(--text-dim);
}

.pl-graph-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 0.8rem;
  opacity: 0.6;
  padding: 2px;
  flex-shrink: 0;
}

.pl-graph-btn:hover {
  opacity: 1;
}
</style>
