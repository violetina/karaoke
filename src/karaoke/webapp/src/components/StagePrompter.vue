<template>
  <div class="stage-prompter-card">
    <div class="stage-header">
      <div class="stage-title-wrap">
        <span class="live-indicator" :class="{ playing: stage.status === 'Playing' }"></span>
        <span class="header-label">STAGE PROMPTER &amp; LIVE MOOD</span>
      </div>
      <div class="actions">
        <button
          class="badge-link kiosk-toggle"
          :class="{ active: kioskActive }"
          @click="toggleKiosk"
          title="Toggle kiosk Chrome player window"
        >
          🖥️ Kiosk Chrome: {{ kioskActive ? 'Active' : 'Closed (Click to Launch)' }}
        </button>
        <a :href="tvUrl" target="_blank" class="badge-link">📺 TV Fullscreen</a>
        <a :href="moodUrl" target="_blank" class="badge-link">✨ Mood Visualizer</a>
      </div>
    </div>

    <!-- Combined now-playing, mood and harmony display -->
    <LiveMoodWidget :stage="stage" @select-artist="$emit('select-artist', $event)">
      <slot />
    </LiveMoodWidget>

    <!-- Live Lyrics Prompter View (centered fixed window, /tv-style) -->
    <div class="lyrics-box">
      <div class="lyrics-header">
        <span>KARAOKE PROMPTER</span>
        <div class="lyrics-timing">
          <span v-if="nextLineCountdown" class="next-line-countdown">
            Next line in {{ nextLineCountdown }}
          </span>
          <span v-if="stage.active_line_index >= 0" class="line-counter">
            Line {{ stage.active_line_index + 1 }} / {{ stage.lines.length }}
          </span>
        </div>
      </div>
      <div class="lyrics-stage">
        <div v-if="stage.lines && stage.lines.length > 0" class="lyrics-window">
          <div
            v-for="item in lyricWindow"
            :key="item.idx"
            class="lyric-line"
            :class="item.cls"
          >
            <!-- Active line: word-by-word highlight like the TUI -->
            <template v-if="item.idx === stage.active_line_index && lineWords(item.line).length">
              <span
                v-for="(w, wi) in lineWords(item.line)"
                :key="wi"
                class="lyric-word"
                :class="{ sung: wi < activeWordIndex, singing: wi === activeWordIndex }"
              >{{ w }} </span>
            </template>
            <template v-else>
              {{ item.line.text || item.line }}
            </template>
          </div>
        </div>
        <div v-else class="lyrics-empty">
          <span>{{ stage.status === 'Playing' ? 'Instrumental or sync loading...' : 'No track active. Use Player Controls or ask the AI DJ to start!' }}</span>
        </div>
      </div>
    </div>

    <!-- Upcoming Queue Bar -->
    <div class="queue-strip" v-if="stage.upcoming_queue && stage.upcoming_queue.length > 0">
      <span class="queue-tag">UP NEXT:</span>
      <div class="queue-chips">
        <div
          v-for="(q, i) in stage.upcoming_queue.slice(0, 4)"
          :key="i"
          class="queue-chip"
          @click="$emit('select-artist', q.artist)"
        >
          <span class="q-title">{{ q.title }}</span>
          <span class="q-artist">— {{ q.artist }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import LiveMoodWidget from './LiveMoodWidget.vue'

const props = defineProps({
  stage: {
    type: Object,
    required: true,
  },
})

const emit = defineEmits(['select-artist', 'play-track'])

const tvUrl = 'http://127.0.0.1:8765/tv'
const moodUrl = 'http://127.0.0.1:8765/mood'

const kioskActive = ref(false)

const nextLineCountdown = computed(() => {
  if (props.stage.next_line_in == null) return ''
  const seconds = Number(props.stage.next_line_in)
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  return `${seconds.toFixed(1)}s`
})

// --- Lyric word highlighting ---------------------------------------------
// Split an active line into words for per-word highlighting.
const lineWords = (line) => {
  const text = line?.text || (typeof line === 'string' ? line : '')
  if (!text) return []
  return text.trim().split(/\s+/)
}

// Which word is currently being sung, from enhanced-LRC word onset times.
const activeWordIndex = computed(() => {
  const idx = props.stage.active_line_index
  if (idx < 0 || !props.stage.lines || !props.stage.lines[idx]) return -1
  const line = props.stage.lines[idx]
  const words = line.words || []
  const pos = props.stage.position_s
  if (pos == null) return -1

  // Enhanced LRC: words[] holds each word's onset time — exact highlight.
  if (words.length) {
    let w = -1
    for (let i = 0; i < words.length; i++) {
      if (pos >= words[i]) w = i
      else break
    }
    return w
  }

  // Line-level sync only: estimate by elapsed fraction within the line,
  // using the next line's start time as the end boundary when none is given.
  const start = line.time
  let end = line.end
  if (end == null && props.stage.lines[idx + 1]) {
    end = props.stage.lines[idx + 1].time
  }
  const wc = lineWords(line).length
  if (start != null && end != null && end > start && wc > 0) {
    const frac = Math.min(1, Math.max(0, (pos - start) / (end - start)))
    return Math.min(wc - 1, Math.floor(frac * wc))
  }
  return -1
})

// Fixed centered window around the active line (/tv-style): the active line
// always sits in the middle slot, so it never drifts to the top/bottom.
const WINDOW_BEFORE = 2
const WINDOW_AFTER = 4
const lyricWindow = computed(() => {
  const lines = props.stage.lines || []
  if (!lines.length) return []
  let active = props.stage.active_line_index
  // Before the first synced line, preview from the top.
  if (active < 0) active = 0
  const start = Math.max(0, active - WINDOW_BEFORE)
  const end = Math.min(lines.length, active + WINDOW_AFTER + 1)
  const out = []
  for (let idx = start; idx < end; idx++) {
    let cls = 'upcoming'
    if (props.stage.active_line_index < 0) cls = idx === 0 ? 'next' : 'upcoming'
    else if (idx === props.stage.active_line_index) cls = 'active'
    else if (idx < props.stage.active_line_index) cls = idx === props.stage.active_line_index - 1 ? 'prev' : 'past'
    else cls = idx === props.stage.active_line_index + 1 ? 'next' : 'upcoming'
    out.push({ idx, line: lines[idx], cls })
  }
  return out
})

const moodGlowColor = computed(() => {
  const m = (props.stage.mood || '').toLowerCase()
  if (m.includes('happy') || m.includes('energetic')) return 'rgba(255, 190, 11, 0.3)'
  if (m.includes('sad') || m.includes('melancholic')) return 'rgba(72, 202, 228, 0.3)'
  if (m.includes('party') || m.includes('hype')) return 'rgba(255, 0, 127, 0.3)'
  if (m.includes('chill') || m.includes('romantic')) return 'rgba(157, 78, 221, 0.3)'
  return 'rgba(0, 242, 254, 0.2)'
})

const checkKioskStatus = async () => {
  try {
    const res = await fetch('/api/players/kiosk/status')
    if (res.ok) {
      const data = await res.json()
      kioskActive.value = data.active
    }
  } catch (e) {
    // ignore
  }
}

const toggleKiosk = async () => {
  try {
    const res = await fetch('/api/players/kiosk/toggle', { method: 'POST' })
    if (res.ok) {
      const data = await res.json()
      kioskActive.value = data.active
    }
  } catch (e) {
    // ignore
  }
}

onMounted(() => {
  checkKioskStatus()
  setInterval(checkKioskStatus, 5000)
})
</script>

<style scoped>
.stage-prompter-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  position: relative;
  flex-shrink: 0;
  overflow: visible;
}

.stage-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.stage-title-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}

.live-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #64748b;
}

.live-indicator.playing {
  background: #10b981;
  box-shadow: 0 0 10px #10b981;
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0% { transform: scale(0.95); opacity: 0.8; }
  50% { transform: scale(1.25); opacity: 1; }
  100% { transform: scale(0.95); opacity: 0.8; }
}

.header-label {
  font-size: 0.75rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  color: var(--text-muted);
}

.actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.badge-link {
  font-size: 0.75rem;
  padding: 4px 10px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  color: var(--text-muted);
  text-decoration: none;
  cursor: pointer;
  transition: all 0.2s ease;
}

.badge-link:hover {
  background: rgba(0, 242, 254, 0.15);
  color: #fff;
  border-color: var(--accent-cyan);
}

.kiosk-toggle.active {
  border-color: #10b981;
  color: #10b981;
  background: rgba(16, 185, 129, 0.1);
}

.track-hero {
  display: flex;
  gap: 16px;
  padding: 12px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid rgba(255, 255, 255, 0.04);
  box-shadow: inset 0 0 25px var(--mood-glow);
  align-items: center;
}

.art-cover {
  width: 80px;
  height: 80px;
  border-radius: 8px;
  overflow: hidden;
  flex-shrink: 0;
  border: 1px solid var(--border-bright);
}

.art-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.art-cover.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #181f30;
  font-size: 2rem;
}

.track-details {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.song-title {
  font-size: 1.25rem;
  font-weight: 800;
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.song-artist {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--accent-cyan);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: fit-content;
}

.inspect-hint {
  font-size: 0.7rem;
  color: var(--text-dim);
  opacity: 0.8;
}

.song-artist:hover .inspect-hint {
  color: var(--accent-gold);
}

.song-album {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.badges-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.metric-pill {
  font-size: 0.75rem;
  font-family: 'JetBrains Mono', monospace;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--border);
  color: #cbd5e1;
}

.key-pill {
  border-color: rgba(0, 242, 254, 0.4);
  color: var(--accent-cyan);
}

.mood-pill {
  color: var(--accent-gold);
}

.energy-pill {
  color: var(--accent-pink);
}

/* Transport Controls Row */
.transport-controls-row {
  display: flex;
  align-items: center;
  gap: 14px;
  background: rgba(0, 0, 0, 0.45);
  padding: 10px 14px;
  border-radius: 8px;
  border: 1px solid var(--border-bright);
}

.playback-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
}

.transport-btn {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid var(--border);
  color: #fff;
  font-size: 0.95rem;
  padding: 8px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 700;
  transition: all 0.2s ease;
}

.transport-btn:hover {
  background: rgba(255, 255, 255, 0.2);
  border-color: #fff;
}

.play-knob {
  background: linear-gradient(135deg, var(--accent-cyan), #0077b6);
  color: #07090e;
  font-size: 1rem;
  font-weight: 900;
  padding: 8px 20px;
  box-shadow: 0 0 15px rgba(0, 242, 254, 0.4);
}

.play-knob.playing {
  background: linear-gradient(135deg, var(--accent-pink), #c1121f);
  color: #fff;
  box-shadow: 0 0 15px rgba(255, 0, 127, 0.4);
}

.play-knob:hover {
  transform: scale(1.04);
}

.quick-play-form {
  display: flex;
  flex: 1;
  gap: 8px;
}

.quick-play-form input {
  flex: 1;
  background: rgba(0, 0, 0, 0.4);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  color: #fff;
  font-size: 0.85rem;
  outline: none;
  font-family: inherit;
}

.quick-play-form input:focus {
  border-color: var(--accent-cyan);
}

.play-submit-btn {
  background: rgba(0, 242, 254, 0.2);
  border: 1px solid var(--accent-cyan);
  color: var(--accent-cyan);
  font-weight: 700;
  font-size: 0.8rem;
  padding: 0 14px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}

.play-submit-btn:hover:not(:disabled) {
  background: var(--accent-cyan);
  color: #07090e;
}

.play-submit-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.lyrics-box {
  background: #05070a;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 8px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  height: 470px;
}

.lyrics-header {
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.05em;
  color: var(--text-dim);
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
  flex-shrink: 0;
}

.lyrics-timing {
  display: flex;
  align-items: center;
  gap: 14px;
}

.next-line-countdown {
  color: var(--accent-cyan);
  font-variant-numeric: tabular-nums;
  text-shadow: 0 0 8px rgba(0, 242, 254, 0.35);
}

/* The stage centers the fixed window vertically — active line is always mid. */
.lyrics-stage {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  overflow: hidden;
}

.lyrics-window {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  text-align: center;
  width: 100%;
}

.lyric-line {
  font-size: 1.25rem;
  transition: all 0.3s cubic-bezier(0.2, 0, 0, 1);
  line-height: 1.4;
  width: 100%;
}

.lyric-line.past {
  color: rgba(255, 255, 255, 0.22);
  font-size: 1.0rem;
}

.lyric-line.prev {
  color: rgba(255, 255, 255, 0.4);
  font-size: 1.1rem;
}

.lyric-line.active {
  color: #fff;
  font-weight: 800;
  font-size: 2.0rem;
  color: var(--accent-cyan);
  text-shadow: 0 0 16px rgba(0, 242, 254, 0.55);
  transform: scale(1.03);
}

.lyric-line.next {
  color: rgba(255, 255, 255, 0.78);
  font-size: 1.2rem;
}

.lyric-line.upcoming {
  color: rgba(255, 255, 255, 0.55);
  font-size: 1.1rem;
}

/* Per-word highlight (TUI-style) on the active line. */
.lyric-word {
  transition: color 0.12s ease, text-shadow 0.12s ease;
  color: rgba(255, 255, 255, 0.55);
  margin-right: 0.3em;
}

.lyric-word.sung {
  color: #fff;
}

.lyric-word.singing {
  color: var(--accent-gold);
  text-shadow: 0 0 18px rgba(255, 190, 11, 0.8);
}

.lyrics-empty {
  font-size: 0.85rem;
  color: var(--text-dim);
  text-align: center;
  padding: 24px 0;
  font-style: italic;
}

.queue-strip {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
  overflow-x: auto;
}

.queue-tag {
  font-size: 0.7rem;
  font-weight: 800;
  color: var(--text-dim);
  flex-shrink: 0;
}

.queue-chips {
  display: flex;
  gap: 6px;
}

.queue-chip {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  white-space: nowrap;
  cursor: pointer;
  transition: all 0.2s ease;
}

.queue-chip:hover {
  background: rgba(255, 255, 255, 0.1);
  border-color: var(--accent-cyan);
}

.q-title {
  color: #f1f5f9;
  font-weight: 600;
}

.q-artist {
  color: var(--text-muted);
  margin-left: 4px;
}
</style>
