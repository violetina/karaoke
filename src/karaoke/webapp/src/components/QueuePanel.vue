<template>
  <div class="queue-panel-card">
    <div class="qp-header">
      <span class="qp-title">📋 UP NEXT</span>
      <div class="qp-actions">
        <button
          class="qp-follow qp-keepgoing"
          :class="{ active: autopilot }"
          @click="toggleAutopilot"
          :disabled="autopilotBusy"
          title="Keep going: auto-add fitting tracks as the queue nears its end"
        >
          {{ autopilotBusy ? '…' : (autopilot ? '🔁 Keep going: On' : '🔁 Keep going') }}
        </button>
        <button
          class="qp-follow"
          :class="{ active: following }"
          @click="toggleFollow"
          :disabled="followBusy"
          :title="following ? 'Stop auto-advancing the queue' : 'Play the queue and auto-advance on each track end'"
        >
          {{ followBusy ? '…' : (following ? '⏸ Following' : '▶ Follow Queue') }}
        </button>
      </div>
    </div>

    <ol class="qp-list" v-if="tracks.length">
      <li
        v-for="(t, i) in tracks"
        :key="i"
        class="qp-item"
        @click="$emit('select-artist', t.artist)"
        :title="`${t.title} — ${t.artist}`"
      >
        <span class="qp-num">{{ i + 1 }}</span>
        <span class="qp-text">
          <span class="qp-song">{{ t.title }}</span>
          <span class="qp-artist">{{ t.artist }}</span>
        </span>
      </li>
    </ol>
    <div class="qp-empty" v-else>
      <span>Queue is empty — pick a playlist or add songs from the graph.</span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  stage: {
    type: Object,
    required: true,
  },
})

defineEmits(['select-artist'])

// The stage state already resolves the next tracks (anchored on the song that
// is actually playing). Show up to 4.
const tracks = computed(() => (props.stage.upcoming_queue || []).slice(0, 4))

// --- Follow Queue: drive the local queue through the kiosk via the follower
// service (karaoke-queue-follow.service), controlled through /api/follow.
const following = ref(false)
const followBusy = ref(false)
const autopilot = ref(false)
const autopilotBusy = ref(false)
let followTimer = null

const checkFollowStatus = async () => {
  try {
    const res = await fetch('/api/follow')
    if (res.ok) {
      const data = await res.json()
      following.value = data.enabled
      autopilot.value = !!data.autopilot
    }
  } catch (e) {
    // ignore transient errors
  }
}

const toggleFollow = async () => {
  if (followBusy.value) return
  followBusy.value = true
  const target = !following.value
  try {
    const res = await fetch('/api/follow', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: target }),
    })
    if (res.ok) following.value = (await res.json()).enabled
    else await checkFollowStatus()
  } catch (e) {
    await checkFollowStatus()
  } finally {
    followBusy.value = false
  }
}

const toggleAutopilot = async () => {
  if (autopilotBusy.value) return
  autopilotBusy.value = true
  const target = !autopilot.value
  try {
    const res = await fetch('/api/queue/autopilot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: target }),
    })
    if (res.ok) autopilot.value = !!(await res.json()).autopilot
    else await checkFollowStatus()
  } catch (e) {
    await checkFollowStatus()
  } finally {
    autopilotBusy.value = false
  }
}

onMounted(() => {
  checkFollowStatus()
  followTimer = setInterval(checkFollowStatus, 5000)
})
onBeforeUnmount(() => {
  if (followTimer) clearInterval(followTimer)
})

</script>

<style scoped>
.queue-panel-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  margin-top: 14px;
}

.qp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  gap: 8px;
}

.qp-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.qp-keepgoing.active {
  background: var(--accent-gold);
  border-color: var(--accent-gold);
  color: #2a1e00;
}

.qp-title {
  font-size: 0.78rem;
  letter-spacing: 0.14em;
  color: var(--accent-gold);
  font-weight: 700;
}

.qp-count {
  font-size: 0.7rem;
  color: var(--text-muted);
}

.qp-follow {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 4px 10px;
  border-radius: 999px;
  cursor: pointer;
  color: var(--text-primary, #fff);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--border);
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.qp-follow:hover:not(:disabled) {
  border-color: var(--accent-cyan);
}

.qp-follow.active {
  background: var(--accent-cyan);
  border-color: var(--accent-cyan);
  color: #06212a;
}

.qp-follow:disabled {
  opacity: 0.6;
  cursor: default;
}

.qp-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.qp-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 9px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid transparent;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.qp-item:hover {
  background: rgba(0, 242, 254, 0.08);
  border-color: var(--accent-cyan);
}

.qp-num {
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.07);
  color: var(--text-muted);
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
}

.qp-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.qp-song {
  font-size: 0.9rem;
  color: var(--text-primary, #fff);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.qp-artist {
  font-size: 0.76rem;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.qp-empty {
  font-size: 0.8rem;
  color: var(--text-muted);
  padding: 6px 2px;
}
</style>
