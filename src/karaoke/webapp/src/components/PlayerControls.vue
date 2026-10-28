<template>
  <div class="player-controls-card">
    <div class="pc-header">
      <span class="pc-title">🎛️ PLAYER CONTROLS</span>
      <button
        class="kiosk-chip"
        :class="{ active: kioskActive }"
        @click="toggleKiosk"
        title="Toggle kiosk Chrome player window"
      >
        🖥️ {{ kioskActive ? 'Kiosk On' : 'Kiosk Off' }}
      </button>
    </div>

    <!-- Now playing mini -->
    <div class="now-playing" v-if="stage.title">
      <div class="np-heading">
        <img v-if="stage.art_url && !artworkFailed" :src="artworkUrl" alt="Album artwork" class="np-artwork" @error="artworkFailed = true" />
        <div v-else class="np-artwork np-artwork-placeholder" aria-label="No album artwork">♪</div>
        <div class="np-title">{{ stage.title }}</div>
      </div>
      <div class="np-artist">{{ stage.artist }}</div>
      <div class="np-progress">
        <div class="np-bar">
          <div class="np-fill" :style="{ width: progressPct + '%' }"></div>
        </div>
        <span class="np-time">{{ fmtTime(stage.position_s) }} / {{ fmtTime(stage.duration) }}</span>
      </div>
    </div>
    <div class="now-playing idle" v-else>
      <span>Nothing playing — search below or pick a playlist.</span>
    </div>

    <!-- Transport -->
    <div class="transport-row">
      <button class="t-btn" @click="seek(-10)" title="Back 10s">« 10s</button>
      <button class="t-btn" @click="playerPrev" title="Previous track">⏮</button>
      <button
        class="t-btn play-knob"
        :class="{ playing: stage.status === 'Playing' }"
        @click="playerPlayPause"
        title="Play / Pause"
      >
        {{ stage.status === 'Playing' ? '⏸' : '▶' }}
      </button>
      <button class="t-btn" @click="playerNext" title="Next track">⏭</button>
      <button class="t-btn" @click="seek(10)" title="Forward 10s">10s »</button>
    </div>

    <!-- Quick play search -->
    <div class="quick-play">
      <input
        v-model="quickQuery"
        type="text"
        placeholder="Play a song or artist…"
        @keyup.enter="handleQuickPlay"
      />
      <button class="qp-btn" @click="handleQuickPlay" :disabled="!quickQuery.trim()">
        ▶ Play
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  stage: { type: Object, required: true },
})

const emit = defineEmits(['play-track'])

const artworkFailed = ref(false)
const artworkUrl = computed(() => '/api/players/artwork?' + new URLSearchParams({
  track: `${props.stage.artist || ''}:${props.stage.title || ''}:${props.stage.art_url || ''}`,
}))
watch(artworkUrl, () => { artworkFailed.value = false })
const quickQuery = ref('')
const kioskActive = ref(false)
let kioskTimer = null

const progressPct = computed(() => {
  const pos = props.stage.position_s
  const dur = props.stage.duration
  if (!pos || !dur || dur <= 0) return 0
  return Math.min(100, (pos / dur) * 100)
})

const fmtTime = (s) => {
  if (s == null || isNaN(s)) return '0:00'
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

const post = async (path, body) => {
  try {
    await fetch(path, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    // ignore
  }
}

const playerPlayPause = () => post('/api/players/play-pause')
const playerNext = () => post('/api/players/next')
const playerPrev = () => post('/api/players/previous')
const seek = (offset) => post('/api/players/seek', { offset_s: offset })

const handleQuickPlay = () => {
  const q = quickQuery.value.trim()
  if (!q) return
  emit('play-track', q)
  quickQuery.value = ''
}

const checkKioskStatus = async () => {
  try {
    const res = await fetch('/api/players/kiosk/status')
    if (res.ok) kioskActive.value = (await res.json()).active
  } catch (e) {
    // ignore
  }
}

const toggleKiosk = async () => {
  try {
    const res = await fetch('/api/players/kiosk/toggle', { method: 'POST' })
    if (res.ok) kioskActive.value = (await res.json()).active
  } catch (e) {
    // ignore
  }
}

onMounted(() => {
  checkKioskStatus()
  kioskTimer = setInterval(checkKioskStatus, 5000)
})

onBeforeUnmount(() => {
  if (kioskTimer) clearInterval(kioskTimer)
})
</script>

<style scoped>
.player-controls-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex-shrink: 0;
}

.pc-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.pc-title {
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: var(--accent-gold);
}

.kiosk-chip {
  font-size: 0.68rem;
  padding: 3px 9px;
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s ease;
}

.kiosk-chip.active {
  color: #07090e;
  background: var(--accent-cyan);
  border-color: var(--accent-cyan);
  font-weight: 700;
}

.now-playing {
  background: rgba(0, 0, 0, 0.3);
  border-radius: 8px;
  padding: 10px 12px;
}

.now-playing.idle {
  color: var(--text-dim);
  font-size: 0.8rem;
  font-style: italic;
  text-align: center;
}

.np-heading { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.np-heading .np-title { white-space: normal; overflow-wrap: anywhere; min-width: 0; }
.np-artwork { width: 80px; height: 80px; object-fit: cover; border-radius: 8px; flex-shrink: 0; }
.np-artwork-placeholder { display: grid; place-items: center; background: rgba(255, 255, 255, 0.05); color: var(--text-muted); font-size: 2rem; }

.np-title {
  font-size: 0.95rem;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.np-artist {
  font-size: 0.8rem;
  color: var(--accent-cyan);
  margin-bottom: 8px;
}

.np-progress {
  display: flex;
  align-items: center;
  gap: 8px;
}

.np-bar {
  flex: 1;
  height: 5px;
  background: rgba(255, 255, 255, 0.1);
  border-radius: 3px;
  overflow: hidden;
}

.np-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent-cyan), var(--accent-pink));
  border-radius: 3px;
  transition: width 0.3s linear;
}

.np-time {
  font-size: 0.68rem;
  font-family: 'JetBrains Mono', monospace;
  color: var(--text-muted);
  flex-shrink: 0;
}

.transport-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.t-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: #e2e8f0;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 0.85rem;
  cursor: pointer;
  transition: all 0.15s ease;
  min-width: 42px;
}

.t-btn:hover {
  background: rgba(255, 255, 255, 0.12);
  border-color: var(--border-bright);
}

.play-knob {
  background: linear-gradient(135deg, var(--accent-cyan), #2563eb);
  border: none;
  color: #fff;
  font-size: 1.1rem;
  width: 54px;
  height: 46px;
  border-radius: 50%;
  box-shadow: 0 0 16px rgba(0, 242, 254, 0.4);
}

.play-knob.playing {
  background: linear-gradient(135deg, var(--accent-pink), #be123c);
  box-shadow: 0 0 16px rgba(255, 0, 127, 0.4);
}

.quick-play {
  display: flex;
  gap: 8px;
}

.quick-play input {
  flex: 1;
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 8px 10px;
  color: #fff;
  font-size: 0.8rem;
  outline: none;
}

.quick-play input:focus {
  border-color: var(--accent-cyan);
}

.qp-btn {
  background: var(--accent-cyan);
  color: #07090e;
  border: none;
  border-radius: 7px;
  padding: 0 14px;
  font-weight: 700;
  font-size: 0.8rem;
  cursor: pointer;
}

.qp-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
