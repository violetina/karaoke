<template>
  <div class="mood-widget-card" :style="{ '--mood-color': moodColor, '--mood-glow': moodGlow }">
    <div class="mood-widget-header">
      <span class="mood-tag">✨ LIVE MOOD &amp; CHORDS</span>
      <div class="mood-badge" :style="{ borderColor: moodColor, color: moodColor }">
        <span class="pulse-dot" :style="{ backgroundColor: moodColor }"></span>
        {{ (stage.mood || 'neutral').toUpperCase() }}
      </div>
    </div>

    <div class="mood-widget-body">
      <div class="mood-visual-column">
      <div class="mood-metrics">
        <span class="mini-tag" v-if="stage.key">Key: {{ stage.key }}</span>
        <span class="mini-tag" v-if="stage.bpm">BPM: {{ Math.round(stage.bpm) }}</span>
        <span class="mini-tag energy" v-if="stage.energy != null">⚡ {{ Math.round(stage.energy * 100) }}%</span>
      </div>
        <!-- Same canvas animation and in-square chord overlay as /mood. -->
        <iframe class="mood-animation" src="/mood/embedded" title="Live animated mood and chords" scrolling="no"></iframe>
        <div class="mood-history">
          <div class="mood-history-label">MOOD OVER TIME</div>
          <template v-if="moodTimeline">
            <div class="mood-history-strip" aria-label="Song mood timeline">
              <span v-for="(segment, i) in moodTimeline.mood_segments" :key="i"
                :style="{ flexGrow: Math.max(0, segment[1] - segment[0]), background: segment[3] ? `repeating-linear-gradient(135deg, ${moodColors[segment[2]] || '#94a3b8'} 0 5px, ${moodColors[segment[3]] || '#94a3b8'} 5px 10px)` : (moodColors[segment[2]] || '#94a3b8') }"
                :title="`${segment[0]}–${segment[1]}s: ${segment[4] || segment[2]}${segment[3] ? ' / ' + segment[3] : ''}`"></span>
            </div>
            <div class="mood-history-summary">{{ moodTimeline.mood_arc || moodTimeline.mood_feeling || moodTimeline.dominant_mood }} · {{ moodTimeline.mood_swings }} {{ moodTimeline.mood_swings === 1 ? 'swing' : 'swings' }}<template v-if="moodTimeline.mood_borderline"> · {{ Math.round(moodTimeline.mood_borderline * 100) }}% borderline</template></div>
          </template>
          <span v-else class="mood-history-summary">{{ moodTimelineStatus }}</span>
        </div>
      </div>

      <div class="track-summary">
        <div class="track-heading">
          <div>
            <div class="track-title">{{ stage.title || 'Karaoke Standby' }}</div>
            <button class="track-artist" @click="$emit('select-artist', stage.artist)">{{ stage.artist || 'Waiting for song…' }}</button>
            <div v-if="stage.album" class="track-album">{{ stage.album }}</div>
          </div>
        </div>
        <div class="library-context" aria-live="polite">
          <template v-if="library">
            <span>{{ library.play_count == null ? 'Not in library' : `${library.play_count} plays` }}</span>
            <span v-if="library.playlists.length">In: {{ library.playlists.map(p => p.name).join(' · ') }}</span>
            <span v-else>Not in a saved playlist</span>
          </template>
          <span v-else>{{ libraryError ? 'Library details unavailable' : 'Loading library details…' }}</span>
        </div>
        <slot />
      </div>

    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  stage: {
    type: Object,
    required: true,
  },
})

defineEmits(['select-artist'])
const library = ref(null)
const libraryError = ref(false)
const moodTimeline = ref(null)
const moodTimelineStatus = ref('Loading timeline…')
const moodColors = { happy: '#facc15', tender: '#22c55e', sad: '#3b82f6', angry: '#ef4444', neutral: '#94a3b8' }
watch(() => JSON.stringify([props.stage.artist, props.stage.title]), async (identity, _, onCleanup) => {
  const [artist, title] = JSON.parse(identity)
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  library.value = null
  libraryError.value = false
  moodTimeline.value = null
  moodTimelineStatus.value = 'Loading timeline…'
  try {
    const params = new URLSearchParams({ artist: artist || '', title: title || '' })
    const res = await fetch(`/api/song/library-context?${params}`, { signal: controller.signal })
    if (!res.ok) throw new Error('Library lookup failed')
    const data = await res.json()
    if (controller.signal.aborted) return
    library.value = data
    if (data.track_id == null) {
      moodTimelineStatus.value = 'No stored mood timeline'
      return
    }
    try {
      const timelineResponse = await fetch(`/api/song/mood-timeline?track_id=${encodeURIComponent(data.track_id)}`, { signal: controller.signal })
      if (!timelineResponse.ok) throw new Error('Timeline unavailable')
      const timeline = await timelineResponse.json()
      if (!controller.signal.aborted) {
        moodTimeline.value = timeline
        moodTimelineStatus.value = 'No stored mood timeline'
      }
    } catch (e) {
      if (!controller.signal.aborted) moodTimelineStatus.value = 'Mood timeline unavailable'
    }
  } catch (e) {
    if (!controller.signal.aborted) {
      libraryError.value = true
      moodTimelineStatus.value = 'Mood timeline unavailable'
    }
  }
}, { immediate: true })

const moodColor = computed(() => {
  const m = (props.stage.mood || '').toLowerCase()
  if (m === 'happy') return '#ffbe0b'
  if (m === 'tender') return '#10b981'
  if (m === 'sad') return '#3b82f6'
  if (m === 'angry') return '#ef4444'
  if (m === 'party' || m === 'hype') return '#ff007f'
  return '#00f2fe'
})

const moodGlow = computed(() => {
  const c = moodColor.value
  return `${c}40`
})
</script>

<style scoped>
.mood-history { display: flex; flex-direction: column; gap: 6px; }
.mood-history-label { font-size: 0.65rem; font-weight: 800; color: var(--text-dim); letter-spacing: 0.06em; }
.mood-history-summary { font-size: 0.78rem; color: var(--text-muted); overflow-wrap: anywhere; }
.mood-history-strip { display: flex; height: 12px; border-radius: 4px; overflow: hidden; background: #222; }
.mood-history-strip span { flex-basis: 0; min-width: 0; }

.mood-visual-column {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: min(280px, 100%);
  flex-shrink: 0;
}
.mood-visual-column .mood-metrics { justify-content: center; gap: 12px; }

.mood-animation {
  width: min(280px, 100%);
  height: auto;
  aspect-ratio: 1;
  border: 1px solid var(--border-bright);
  border-radius: 12px;
  flex-shrink: 0;
  background: #000;
}

.mood-widget-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  position: relative;
  overflow: hidden;
  box-shadow: inset 0 0 20px var(--mood-glow);
  flex-shrink: 0;
}

.mood-widget-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.mood-tag {
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  color: var(--text-dim);
}

.mood-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid;
  background: rgba(0, 0, 0, 0.4);
}

.pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  animation: pulse-dot 1.2s infinite;
}

@keyframes pulse-dot {
  0% { transform: scale(0.9); opacity: 0.7; }
  50% { transform: scale(1.3); opacity: 1; }
  100% { transform: scale(0.9); opacity: 0.7; }
}

.mood-widget-body {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.track-summary { flex: 2 1 300px; min-width: 0; display: flex; flex-direction: column; gap: 10px; }
.track-heading { display: flex; align-items: center; gap: 12px; }
.track-art { width: 64px; height: 64px; object-fit: cover; border-radius: 8px; }
.track-title { color: #fff; font-size: 1.2rem; font-weight: 800; overflow-wrap: anywhere; }
.track-artist { background: none; border: 0; padding: 0; color: var(--accent-cyan); cursor: pointer; text-align: left; font: inherit; }
.track-album { color: var(--text-muted); font-size: 0.8rem; }
.library-context { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 8px; color: var(--text-muted); font-size: 0.75rem; }
.mood-metrics { flex-wrap: wrap; }

.mood-avatar-wrap {
  width: 140px;
  height: 140px;
  border-radius: 8px;
  overflow: hidden;
  background: #000;
  border: 2px solid var(--border-bright);
  position: relative;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.pixel-art-avatar {
  width: 100%;
  height: 100%;
  image-rendering: pixelated;
  object-fit: cover;
}

.avatar-ring {
  position: absolute;
  inset: 0;
  box-shadow: inset 0 0 10px var(--mood-color);
  pointer-events: none;
}

.chords-panel {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 0;
}

.chords-label {
  font-size: 0.65rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  color: var(--text-dim);
}

.chords-display {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.current-chord {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.chord-main {
  font-size: 2.4rem;
  font-weight: 900;
  font-family: 'JetBrains Mono', monospace;
  color: #fff;
  letter-spacing: -0.02em;
}

.chord-roman {
  font-size: 0.8rem;
  font-family: 'JetBrains Mono', monospace;
  color: var(--accent-cyan);
}

.next-chord-wrap {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-family: 'JetBrains Mono', monospace;
}

.arrow {
  color: var(--accent-gold);
  font-size: 0.7rem;
}

.next-chord-name {
  color: #fff;
  font-weight: 700;
}

.next-chord-timer {
  color: var(--text-dim);
  font-size: 0.68rem;
}

.mood-metrics {
  display: flex;
  gap: 6px;
  margin-top: 2px;
}

.mini-tag {
  font-size: 0.65rem;
  font-family: 'JetBrains Mono', monospace;
  color: var(--text-muted);
}

.mini-tag.energy {
  color: var(--accent-pink);
}

.eq-visualizer {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 76px;
  padding: 0 4px;
}

.v-bar {
  width: 3px;
  height: 100%;
  border-radius: 2px;
  animation: bar-dance 0.8s infinite alternate ease-in-out;
  opacity: 0.85;
}

@keyframes bar-dance {
  0% { transform: scaleY(0.15); }
  50% { transform: scaleY(0.9); }
  100% { transform: scaleY(0.3); }
}
</style>
