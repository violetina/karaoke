<template>
  <div class="song-list-card">
    <div class="list-header">
      <div class="header-left">
        <span class="list-title">📚 TRACKS IN LIBRARY</span>
        <span class="artist-sub">{{ artistName || 'None Selected' }}</span>
      </div>
      <span class="count-badge" v-if="filteredSongs.length">
        {{ filteredSongs.length }} {{ filteredSongs.length === 1 ? 'song' : 'songs' }}
      </span>
    </div>

    <!-- Quick filter input for songs -->
    <div class="song-filter-box" v-if="songs.length > 5">
      <input
        v-model="searchFilter"
        type="text"
        placeholder="Filter songs by title..."
        class="filter-input"
      />
    </div>

    <div v-if="loading" class="list-loading">⏳ Searching library tracks…</div>

    <div v-else-if="songs.length === 0" class="list-empty">
      No library tracks found for "{{ artistName }}".
    </div>

    <div v-else class="song-scroll">
      <div
        v-for="song in filteredSongs"
        :key="song.track_id"
        class="song-row"
        :class="{ active: song.title === currentTitle }"
      >
        <button class="song-play-btn" @click="$emit('play-song', song)" title="Play this track">
          ▶
        </button>

        <div class="song-info" @click="$emit('play-song', song)">
          <div class="song-title-line">
            <span class="song-title">{{ song.title }}</span>
          </div>
          <div class="song-artist-line">
            <span class="song-artist">{{ song.artist }}</span>
            <span v-if="song.album" class="song-album">· {{ song.album }}</span>
            <span v-if="song.play_count" class="song-plays">· {{ song.play_count }}×</span>
          </div>
        </div>

        <div class="song-badges">
          <span v-if="song.camelot" class="mini-pill key" title="Camelot Key">{{ song.camelot }}</span>
          <span v-if="song.bpm" class="mini-pill bpm" title="BPM">{{ Math.round(song.bpm) }}</span>
          <span v-if="song.energy !== null" class="mini-pill energy" title="Energy">⚡{{ Math.round(song.energy * 100) }}%</span>
          <button class="graph-btn" @click.stop="$emit('focus-song', song)" title="Center similarity graph on this track">🕸️</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  artistName: { type: String, default: '' },
  songs: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  currentTitle: { type: String, default: '' },
})

defineEmits(['play-song', 'focus-song'])

const searchFilter = ref('')

const filteredSongs = computed(() => {
  if (!searchFilter.value.trim()) return props.songs
  const q = searchFilter.value.toLowerCase().trim()
  return props.songs.filter(
    (s) => (s.title && s.title.toLowerCase().includes(q)) || (s.album && s.album.toLowerCase().includes(q))
  )
})
</script>

<style scoped>
.song-list-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  min-height: 280px;
  max-height: 380px;
  flex-shrink: 0;
  overflow: hidden;
}

.list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
  margin-bottom: 8px;
}

.header-left {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.list-title {
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: var(--accent-gold);
}

.artist-sub {
  font-size: 0.85rem;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.count-badge {
  font-size: 0.7rem;
  color: var(--accent-cyan);
  font-family: 'JetBrains Mono', monospace;
  background: rgba(0, 242, 254, 0.1);
  padding: 2px 7px;
  border-radius: 4px;
  border: 1px solid rgba(0, 242, 254, 0.3);
  flex-shrink: 0;
}

.song-filter-box {
  margin-bottom: 8px;
}

.filter-input {
  width: 100%;
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 4px 8px;
  color: #fff;
  font-size: 0.75rem;
  outline: none;
  font-family: inherit;
}

.filter-input:focus {
  border-color: var(--accent-cyan);
}

.list-loading,
.list-empty {
  color: var(--text-dim);
  font-size: 0.8rem;
  text-align: center;
  padding: 30px 10px;
  font-style: italic;
}

.song-scroll {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1;
  padding-right: 4px;
}

.song-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid transparent;
  transition: all 0.15s ease;
}

.song-row:hover {
  background: rgba(255, 255, 255, 0.06);
  border-color: var(--border-bright);
}

.song-row.active {
  background: rgba(0, 242, 254, 0.12);
  border-color: rgba(0, 242, 254, 0.4);
}

.song-play-btn {
  background: rgba(0, 242, 254, 0.15);
  border: 1px solid var(--accent-cyan);
  color: var(--accent-cyan);
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

.song-play-btn:hover {
  transform: scale(1.15);
  background: var(--accent-cyan);
  color: #07090e;
}

.song-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  cursor: pointer;
}

.song-title-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.song-title {
  font-size: 0.85rem;
  font-weight: 700;
  color: #f8fafc;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.song-artist-line {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.72rem;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.song-artist {
  color: var(--accent-cyan);
  font-weight: 600;
}

.song-album {
  color: var(--text-dim);
}

.song-plays {
  color: var(--text-dim);
}

.song-badges {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.mini-pill {
  font-size: 0.62rem;
  font-family: 'JetBrains Mono', monospace;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.06);
  color: #cbd5e1;
}

.mini-pill.key {
  color: var(--accent-cyan);
  border: 1px solid rgba(0, 242, 254, 0.3);
}

.mini-pill.energy {
  color: var(--accent-pink);
}

.graph-btn {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 0.8rem;
  opacity: 0.6;
  padding: 2px;
  transition: opacity 0.15s ease;
}

.graph-btn:hover {
  opacity: 1;
}
</style>
