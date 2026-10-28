// Exercise the real chat script against fixture SSE, without touching playback.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../src/karaoke/webapp/package.json', import.meta.url))
const { ref, computed, nextTick } = require('vue')
const source = readFileSync(new URL('../src/karaoke/webapp/src/components/AiDjChat.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .* from 'vue'\n/m, '')
for (const action of ['queue_suggestion', 'queue_track']) {
  const emitted = []
  const frames = [
    { token: 'This song has a dreamy vibe. [QUEUE: Example Artist - Example Track]' },
    { action, track: 'Example Artist - Example Track' },
  ].map(data => `data: ${JSON.stringify(data)}\n\n`).join('')
  let read = false
  const context = vm.createContext({
    ref, computed, nextTick, TextDecoder,
    defineProps: () => ({}),
    defineEmits: () => (...args) => emitted.push(args),
    fetch: async () => ({ body: { getReader: () => ({ read: async () => {
      if (read) return { done: true }
      read = true
      return { done: false, value: new TextEncoder().encode(frames) }
    } }) } }),
  })
  vm.runInContext(script + '\nglobalThis.api = { inputQuery, handleSend, messages };', context)
  context.api.inputQuery.value = "What's the vibe?"
  await context.api.handleSend()
  assert.equal(emitted.length, 0, `${action} must not emit playback events`)
  assert.equal(context.api.messages.value.at(-1).queueSuggestions[0], 'Example Artist - Example Track')
}
const app = readFileSync(new URL('../src/karaoke/webapp/src/App.vue', import.meta.url), 'utf8')
const queueHandler = app.split('const handleQueueTrack = async')[1].split('// Live stage state')[0]
assert.ok(queueHandler.includes("'/api/song/queue'"))
assert.ok(!queueHandler.includes('handlePlayTrack'))
console.log('PASS: vibe replies and legacy queue frames cannot auto-queue/play; explicit queue uses queue-only endpoint')
