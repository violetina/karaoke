<template>
  <div class="queue-panel-card">
    <div class="qp-header">
      <span class="qp-title">📋 UP NEXT</span>
      <span class="qp-count" v-if="tracks.length">{{ tracks.length }} queued</span>
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
import { computed } from 'vue'

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
